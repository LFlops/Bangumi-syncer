"""LLM 匹配增强调度器。

继承 BaseScheduler，同构 bangumi_replay_scheduler，按 ``[sync] llm_match_cron``
（默认 ``*/1 * * * *``）定时轮询 agent_runs 处理 match 任务。

每轮流程（recover 与 pending 共享并发限额并发消费）：
1. **清理**：终态且 ended_at 超保留期 → 先删 agent_steps 再删 agent_runs（级联）+ 日志
2. **恢复扫描**：``processing`` 且 started_at 超时的遗留 run → 刷新 started_at
   → sync_record 缺失则 mark_failed；否则构造 bgm 后交由场景层公开入口
   ``llm_assist.continue_run`` 完成 replay 重建与续跑（本调度器不触碰场景内部符号）
3. **正常处理**：逐条原子拾取（由 llm_assist.run 内部 atomic_claim 负责）→
    调 ``llm_assist.run`` → 异常捕获累加 attempts（≥3 标 failed）

recover 与 pending 统一入列后经 ``asyncio.Semaphore(llm_match_concurrency)`` 并发消费，
单条 run 异常在包装层隔离记录，不影响同批其他 run。sync_record_id 尚未回填
（入队前移的毫秒级窗口）的 run 本轮跳过，等回填后下一轮处理。

去重落在落任务入口（sync_service 的 _handle_match_failure），本调度器只处理已存在任务。
重入防护：本进程正在处理的 run 记入模块级 ``_active_run_ids``，恢复扫描与正常处理
均跳过，避免「进程活着但处理慢」被误判为崩溃遗留而双跑；恢复开始时以统一时间戳
抢占刷新 started_at。跨进程死进程恢复仍由 list_stale_processing 超时判定兜底。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import ValidationError

from app.core.accounts import get_active_bangumi_config
from app.core.config import config_manager
from app.core.database import get_database_manager
from app.core.logging import logger
from app.models.agent import AgentRunRecord
from app.services.base.scheduler import BaseScheduler
from app.services.matching import llm_assist as llm_assist_module
from app.services.notification_service import get_notification_service
from app.utils.bangumi_api import BangumiApi

# ---------------------------------------------------------------------------
# 进程级 active run 防护
#
# 定时任务仅凭 started_at 超时无法区分「进程已死（应恢复）」与「进程活着但处理慢
# （不应恢复）」，多实例/重入下会双跑同一 run。本进程内已取得执行权的 run 记入
# ``_active_run_ids``，恢复扫描与正常处理均据此跳过。
# ---------------------------------------------------------------------------

_active_run_ids: set[str] = set()


def _try_acquire_run(run_id: str) -> bool:
    """尝试取得 run 在本进程的执行权；已持有返回 False。

    原子性依据：CPython 事件循环单线程调度协程，本函数为**不含 await 的同步
    check-and-set**，执行期间不会发生协程切换，因此 "in + add" 两步对其它协程
    而言是原子的。无需 asyncio.Lock（后者还带来跨 event loop 绑定风险）。
    """
    if run_id in _active_run_ids:
        return False
    _active_run_ids.add(run_id)
    return True


def _release_run(run_id: str) -> None:
    """释放 run 的本进程执行权（幂等）。"""
    _active_run_ids.discard(run_id)


def _clear_active_runs() -> None:
    """清空 active 集合（测试隔离用，避免模块级状态跨测试污染）。"""
    _active_run_ids.clear()


def _cfg_bool(value: Any) -> bool:
    """宽松布尔解析：实际 bool 或字符串 true/1/yes/on（任意大小写）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _cfg_int(value: Any, fallback: int) -> int:
    """宽松整数解析，失败回退 fallback。"""
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _parse_run_rows(raw_rows: list, *, source: str) -> list[AgentRunRecord]:
    """把 repo 返回的 dict 行转换为 ``AgentRunRecord``。

    单行构造失败（缺必填列 / 类型不符）时记 error 并跳过该行，不中断整轮调度；
    返回可安全进行属性访问的模型列表。
    """
    records: list[AgentRunRecord] = []
    for raw in raw_rows:
        try:
            records.append(AgentRunRecord.model_validate(raw))
        except ValidationError as e:
            logger.error(
                f"🤖 {source} 行解析为 AgentRunRecord 失败，已跳过: {raw!r}: {e}"
            )
    return records


class LlmMatchScheduler(BaseScheduler):
    """LLM 匹配增强调度器（APScheduler 周期任务）"""

    JOB_ID = "llm_match_pending"
    DEFAULT_CRON = "*/1 * * * *"  # 每 60s
    DRIVER_NAME = "LlmMatch"

    # 每轮最多处理 N 条 pending，防堆积
    BATCH_SIZE = 5

    # ------------------------------------------------------------------
    # 抽象方法实现
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        """启用条件：[sync] llm_match_assist=true 且 LLM api_key 非空。

        - 开关关 → False（不启动）
        - 开关开但 LLM 配置缺失 → False + 日志"LLM 配置缺失，匹配增强已禁用"
        """
        enabled = _cfg_bool(
            config_manager.get("sync", "llm_match_assist", fallback=False)
        )
        if not enabled:
            return False
        llm_cfg = config_manager.get_llm_config()
        if not (llm_cfg.get("api_key") or "").strip():
            logger.info("LLM 配置缺失，匹配增强已禁用")
            return False
        return True

    def _get_driver_config(self) -> dict:
        """返回含 sync_interval（cron）的配置。"""
        return {
            "sync_interval": config_manager.get(
                "sync", "llm_match_cron", fallback=self.DEFAULT_CRON
            )
        }

    async def _run_sync_job(self) -> None:
        """单轮调度：清理 → 恢复扫描 → pending 共享信号量并发消费。

        recover 任务先入列（保证崩溃遗留优先被接管），与 pending 任务共用同一
        并发限额；单条 run 的异常在消费包装层被隔离并记录，不影响同批其他 run。
        """
        if not self._is_enabled():
            return

        dbm = get_database_manager()
        repo = dbm.agent_runs

        # 1. 滑动窗口轮转清理（终态超窗 + 活性过期死行，单条 DELETE）
        retention_days = _cfg_int(
            config_manager.get("sync", "llm_match_retention_days", fallback=30), 30
        )
        try:
            deleted = repo.cleanup_expired(retention_days=retention_days)
        except Exception as e:
            logger.error(f"🤖 清理过期 agent_run 失败: {e}")
            deleted = 0
        if deleted and deleted > 0:
            logger.info(f"🤖 清理过期 agent_run {deleted} 条")

        # 2. 恢复扫描（本进程已在处理（活着但慢）的 run 不得被重复捞起）
        recovery_timeout = _cfg_int(
            config_manager.get("sync", "llm_match_recovery_timeout_s", fallback=120),
            120,
        )
        try:
            stale_raw = repo.list_stale_processing(timeout_seconds=recovery_timeout)
        except Exception as e:
            logger.error(f"🤖 恢复扫描失败: {e}")
            stale_raw = []
        stale = [
            run
            for run in _parse_run_rows(stale_raw, source="恢复扫描")
            if run.run_id not in _active_run_ids
        ]

        # 3. 正常处理（limit 防堆积；切片双保险，避免上层 mock 返回超量）
        try:
            pending_raw = repo.list_pending(limit=self.BATCH_SIZE)[: self.BATCH_SIZE]
        except Exception as e:
            logger.error(f"🤖 列出 pending agent_run 失败: {e}")
            pending_raw = []
        pending = _parse_run_rows(pending_raw, source="pending")

        # 4. 并发消费：recover 先入列，与 pending 共享并发限额（信号量结构化并发）
        limit = max(
            1,
            _cfg_int(
                config_manager.get("sync", "llm_match_concurrency", fallback=3), 3
            ),
        )
        sem = asyncio.Semaphore(limit)
        failures = 0

        async def _consume(fn, run: AgentRunRecord) -> None:
            """单条消费包装：受共享信号量约束，异常隔离不影响同批其他 run。"""
            nonlocal failures
            async with sem:
                try:
                    await fn(run)
                except Exception as e:
                    failures += 1
                    logger.error(f"🤖 处理 run {run.run_id} 异常: {e}")

        tasks = [(self._recover_run, run) for run in stale]
        tasks += [(self._process_run, run) for run in pending]
        await asyncio.gather(*(_consume(fn, run) for fn, run in tasks))
        if failures:
            logger.error(f"🤖 本轮并发消费有 {failures} 条 run 处理异常")

    # ------------------------------------------------------------------
    # 恢复扫描
    # ------------------------------------------------------------------

    async def _recover_run(self, run: AgentRunRecord) -> None:
        """恢复单条崩溃遗留的 processing run。

        0. sync_record_id 尚未回填（T6 前移窗口）→ 跳过本轮，不占用执行权
        1. 取得本进程执行权（防并发恢复双跑；未取得直接跳过）
        2. 以**统一时间戳**刷新 started_at（防下一轮重复恢复）
        3. sync_record 缺失 → mark_failed(error)
        4. 否则构造 bgm → 调用场景层单一公开入口 ``llm_assist.continue_run``
           （replay / 补执行 / 续跑 / 落库与失败分流均在场景层内部完成）

        本方法只负责调度与兜底日志，不触碰场景层私有符号。
        """
        run_id = run.run_id
        # sync_record_id 由 persist 后回填，存在毫秒级窗口；未回填时跳过本轮
        # （不 mark_failed，等回填后下一轮再处理），且必须在 acquire 之前判断，
        # 避免占用（并随后释放）执行权造成同轮 pending 被误跳过。
        if not run.sync_record_id:
            logger.debug(f"🤖 run {run_id} 的 sync_record_id 尚未回填，跳过本轮")
            return
        if not _try_acquire_run(run_id):
            logger.debug(f"🤖 恢复 run {run_id} 跳过：本进程已在处理")
            return
        try:
            repo = get_database_manager().agent_runs

            # 恢复开始即刷新 started_at；统一时间戳由调用方注入（单一时钟基准）
            ts = int(time.time())
            repo.refresh_started_at(run_id, ts)

            sync_record = self._get_sync_record(run.sync_record_id)
            if sync_record is None:
                logger.warning(f"🤖 恢复 run {run_id} 关联 sync_record 缺失，标记失败")
                repo.mark_failed(
                    run_id, stop_reason="error", last_error="sync_record missing"
                )
                return

            bgm = self._build_bgm(sync_record)
            try:
                # 续跑的场景内部逻辑（replay / 补执行 / loop / 落库 / 失败分流）
                # 全部收敛在 continue_run 内；此处不再触碰其私有符号。
                await llm_assist_module.continue_run(
                    run_id,
                    sync_record,
                    bgm,
                    notification_service=get_notification_service(),
                )
            except Exception as e:
                # 兜底：continue_run 内部已按可重试性完成计数/置终态（不向调用方抛），
                # 走到这里说明出现未预期的外层异常，仅记录日志、不重复计数。
                logger.error(f"🤖 恢复续跑 {run_id} 未预期异常: {e}")
        finally:
            _release_run(run_id)

    # ------------------------------------------------------------------
    # 正常处理
    # ------------------------------------------------------------------

    async def _process_run(self, run: AgentRunRecord) -> None:
        """处理单条 pending run：查 sync_record → llm_assist.run → 异常重试。

        入口取得本进程执行权，防止恢复扫描误捞正在处理的 run（T7 并发化后尤为关键）；
        未取得执行权（本进程已有协程在处理）直接跳过，且不释放他人持有的执行权。
        """
        run_id = run.run_id
        # sync_record_id 由 persist 后回填，存在毫秒级窗口；未回填时跳过本轮
        # （不 mark_failed，等回填后下一轮再处理），避免无意义地占用执行权。
        if not run.sync_record_id:
            logger.debug(f"🤖 run {run_id} 的 sync_record_id 尚未回填，跳过本轮")
            return
        if not _try_acquire_run(run_id):
            logger.debug(f"🤖 处理 run {run_id} 跳过：本进程已在处理")
            return
        try:
            repo = get_database_manager().agent_runs

            sync_record = self._get_sync_record(run.sync_record_id)
            if sync_record is None:
                logger.warning(f"🤖 run {run_id} 关联 sync_record 缺失，标记失败")
                repo.mark_failed(
                    run_id, stop_reason="error", last_error="sync_record missing"
                )
                return

            bgm = self._build_bgm(sync_record)
            try:
                # F5：thinking_level 统一从集中配置读取并透传给 llm_assist.run
                # （config_override 由 llm_assist.run 内部从同一配置读取）。
                match_cfg = config_manager.get_sync_llm_match_config()
                thinking_level = match_cfg["llm_match_thinking_level"]
                # atomic_claim / 状态流转 / 落库均在 llm_assist.run 内部完成
                await llm_assist_module.run(
                    run_id,
                    sync_record=sync_record,
                    bgm=bgm,
                    thinking_level=thinking_level,
                    notification_service=get_notification_service(),
                )
            except Exception as e:
                logger.error(f"🤖 处理 run {run_id} 异常: {e}")
                # 计数与达上限置终态在 repo 内单点事务完成，携带 last_error 供排查
                repo.increment_attempts(run_id, last_error=str(e)[:500])
        finally:
            _release_run(run_id)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _get_sync_record(self, sync_record_id: Any) -> dict | None:
        """按 id 查询同步记录；缺失或异常返回 None。"""
        if not sync_record_id:
            return None
        try:
            return get_database_manager().get_sync_record_by_id(int(sync_record_id))
        except Exception as e:
            logger.debug(f"🤖 查询 sync_record {sync_record_id} 失败: {e}")
            return None

    def _build_bgm(self, sync_record: dict):
        """从用户配置构造 BangumiApi 实例（失败返回 None，交由场景层降级）。"""
        try:
            user_name = sync_record.get("user_name")
            cfg = get_active_bangumi_config(user_name)
            if not cfg or not cfg.get("username") or not cfg.get("access_token"):
                logger.debug("🤖 无可用 Bangumi 账号配置，bgm 为 None")
                return None

            dev = config_manager.get_dev_http_snapshot()
            return BangumiApi(
                username=cfg["username"],
                access_token=cfg["access_token"],
                private=cfg.get("private", False),
                http_proxy=dev["script_proxy"],
                ssl_verify=dev["ssl_verify"],
                bgm_api_proxy=dev["bgm_api_proxy"],
                bgm_next_proxy=dev["bgm_next_proxy"],
                ech_mode=dev["ech_mode"],
            )
        except Exception as e:
            logger.warning(f"🤖 构造 BangumiApi 失败: {e}")
            return None


# 全局单例
llm_match_scheduler = LlmMatchScheduler()

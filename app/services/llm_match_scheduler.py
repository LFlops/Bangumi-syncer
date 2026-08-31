"""LLM 匹配增强调度器（spec §3.6 / Task T12，场景 M8/M9/M10 + 恢复 + 去重 + 清理）

继承 BaseScheduler，同构 bangumi_replay_scheduler，按 ``[sync] llm_match_cron``
（默认 ``*/1 * * * *``）定时轮询 agent_runs 处理 match 任务。

每轮顺序（串行处理，F3/R40）：
1. **清理**：终态且 ended_at 超保留期 → 先删 agent_steps 再删 agent_runs（级联）+ 日志
2. **恢复扫描（D18）**：``processing`` 且 started_at 超时的遗留 run → 刷新 started_at
   → sync_record 缺失则 mark_failed；否则重建种子 + 重放 replay_delta → 续跑 loop
3. **正常处理**：逐条原子拾取（由 llm_assist.run 内部 atomic_claim 负责）→
   调 ``llm_assist.run`` → 异常捕获累加 attempts（≥3 标 failed）

去重落在落任务入口（T14 的 _handle_match_failure），本调度器只处理已存在任务。
幂等：恢复中崩溃 → 下次再扫（list_stale_processing 按 started_at 超时判定）。
"""

from __future__ import annotations

from typing import Any

from app.core.config import config_manager
from app.core.database import get_database_manager
from app.core.logging import logger
from app.services.base.scheduler import BaseScheduler
from app.services.llm.models import Message, ToolResultBlock
from app.services.matching import llm_assist as llm_assist_module


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


class LlmMatchScheduler(BaseScheduler):
    """LLM 匹配增强调度器（APScheduler 周期任务）"""

    JOB_ID = "llm_match_pending"
    DEFAULT_CRON = "*/1 * * * *"  # 每 60s
    DRIVER_NAME = "LlmMatch"

    # 每轮最多处理 N 条 pending，防堆积（F3/R40）
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
        """单轮调度：清理 → 恢复扫描 → 正常处理（串行、limit 防堆积）。"""
        if not self._is_enabled():
            return

        dbm = get_database_manager()
        repo = dbm.agent_runs

        # 1. 清理终态过期
        retention_days = _cfg_int(
            config_manager.get("sync", "llm_match_retention_days", fallback=7), 7
        )
        try:
            deleted = repo.cleanup_terminal(retention_days=retention_days)
        except Exception as e:
            logger.debug(f"🤖 清理终态 agent_run 失败: {e}")
            deleted = 0
        if deleted and deleted > 0:
            logger.info(f"🤖 清理终态过期 agent_run {deleted} 条")

        # 2. 恢复扫描（D18）
        recovery_timeout = _cfg_int(
            config_manager.get("sync", "llm_match_recovery_timeout_s", fallback=120),
            120,
        )
        try:
            stale = repo.list_stale_processing(timeout_seconds=recovery_timeout)
        except Exception as e:
            logger.debug(f"🤖 恢复扫描失败: {e}")
            stale = []
        for run in stale:
            try:
                await self._recover_run(run)
            except Exception as e:
                logger.error(f"🤖 恢复 run {run.get('run_id')} 异常: {e}")

        # 3. 正常处理（limit 防堆积；切片双保险，避免上层 mock 返回超量）
        try:
            pending = repo.list_pending(limit=self.BATCH_SIZE)[: self.BATCH_SIZE]
        except Exception as e:
            logger.debug(f"🤖 列出 pending agent_run 失败: {e}")
            pending = []
        for run in pending:
            try:
                await self._process_run(run)
            except Exception as e:
                logger.error(f"🤖 处理 run {run.get('run_id')} 异常: {e}")

    # ------------------------------------------------------------------
    # 恢复扫描
    # ------------------------------------------------------------------

    async def _recover_run(self, run: dict) -> None:
        """恢复单条崩溃遗留的 processing run（D18）。

        1. 刷新 started_at（防下一轮重复恢复）
        2. sync_record 缺失 → mark_failed(error)
        3. 否则重建种子 + 重放 → 续跑 loop
        """
        run_id = run["run_id"]
        repo = get_database_manager().agent_runs

        # 恢复开始即刷新 started_at（B-3）
        repo.refresh_started_at(run_id)

        sync_record = self._get_sync_record(run.get("sync_record_id"))
        if sync_record is None:
            logger.warning(f"🤖 恢复 run {run_id} 关联 sync_record 缺失，标记失败")
            repo.mark_failed(
                run_id, stop_reason="error", last_error="sync_record missing"
            )
            return

        await self._continue_replay(run, sync_record)

    async def _continue_replay(self, run: dict, sync_record: dict) -> None:
        """断点恢复续跑（简化版，spec §3.6 / I-2）。

        重建种子消息 → trace.replay 重建可续跑消息列表 + 终局响应：
        - ``last_response`` 为 None → 全部轮次已完整记录 → 以剩余轮次续跑通用循环
        - ``last_response`` 非 None（F2）：
          * stop_reason=end_turn → 直接 mark_no_suggestion（不调 LLM）
          * 捕获 submit_suggestion（含 tool_calls 中的 terminal 工具）→ 走校验落库
          * 含 tool_use（非终局，缺失工具）→ 补执行缺失只读工具 + 回填结果后
            进入下一轮 loop（remaining 已减 1，因为该轮 LLM 已经发生过）
        """
        from app.services.agent import trace
        from app.services.agent.budget import get_max_iterations
        from app.services.agent.loop import RunResult, run as loop_run
        from app.services.llm.tools import get_tool_registry

        run_id = run["run_id"]
        repo = get_database_manager().agent_runs

        try:
            # F5：thinking_level 与 config_override 统一从集中配置读取
            match_cfg = config_manager.get_sync_llm_match_config()
            thinking_level = match_cfg.get("llm_match_thinking_level") or "medium"
            raw_max = match_cfg.get("llm_match_max_iterations")
            config_override = None
            if raw_max not in (None, ""):
                try:
                    config_override = int(str(raw_max).strip())
                except (TypeError, ValueError):
                    config_override = None

            max_iterations = get_max_iterations(
                "match", thinking_level, config_override=config_override
            )

            candidates = llm_assist_module._extract_candidates(sync_record)
            seed = llm_assist_module.build_seed_messages(
                sync_record, candidates, llm_assist_module.DEFAULT_SYSTEM_TEMPLATE
            )

            replay_result = trace.replay(
                run_id, seed_builder=lambda: seed, max_iterations=max_iterations
            )
            remaining = max_iterations - replay_result.executed_iterations
            if remaining <= 0:
                logger.debug(f"🤖 恢复 {run_id} 已无剩余轮次，跳过续跑")
                return

            last_response = replay_result.last_response

            # F2：终局响应直接分派，避免无谓重调 LLM
            if last_response is not None:
                stop = last_response.get("stop_reason")
                tcs = last_response.get("tool_calls") or []

                if stop == "end_turn":
                    # 无建议：直接标记，不调 LLM
                    repo.mark_no_suggestion(run_id, stop_reason="end_turn")
                    return

                # 捕获 submit_suggestion（终局）→ 走校验落库路径
                submit_tc = next(
                    (tc for tc in tcs if tc.get("name") == "submit_suggestion"), None
                )
                if stop == "submit_suggestion" or submit_tc is not None:
                    sug = (submit_tc or {}).get("input") or {}
                    result = RunResult(
                        stop_reason="submit_suggestion",
                        suggestion=sug,
                        last_response=None,
                    )
                    bgm = self._build_bgm(sync_record)
                    llm_assist_module._handle_result(
                        get_database_manager(),
                        run_id,
                        result,
                        sync_record=sync_record,
                        sync_record_id=sync_record.get("id"),
                        bgm=bgm,
                        notification_service=None,
                    )
                    return

                # 含 tool_use（非终局，存在缺失工具）→ 补执行 + 回填后继续 loop
                bgm = self._build_bgm(sync_record)
                registry = get_tool_registry()
                # 先确保工具已注册（恢复路径可能尚未在正常路径注册过），否则补执行时
                # registry.get 找不到工具；同时注册后才能拿到 tools_schemas 供 loop 续跑。
                llm_assist_module.register_match_tools(registry, bgm)
                for tc in replay_result.missing_tool_calls:
                    await self._replay_missing_tool(
                        tc, registry, replay_result.messages
                    )
                # 该轮 LLM 已发生过，计入预算（F2：remaining 已减）
                remaining = max(0, remaining - 1)
                if remaining <= 0:
                    logger.debug(f"🤖 恢复 {run_id} 补执行后已无剩余轮次，跳过续跑")
                    return
                defns = llm_assist_module.register_match_tools(registry, bgm)
                tools_schemas = [d.to_schema() for d in defns]
                # 注：register_match_tools 已在补执行前调用过，此处再调用为幂等刷新
                chat_fn = llm_assist_module._build_default_chat_fn()
                span_recorder = llm_assist_module._SpanRecorder(run_id)
                result = await loop_run(
                    chat_fn=chat_fn,
                    tools_schemas=tools_schemas,
                    tool_calls_fn=registry.execute_batch,
                    max_iterations=remaining,
                    tool_choice_terminal="submit_suggestion",
                    seed_messages=replay_result.messages,
                    span_recorder=span_recorder,
                )
                llm_assist_module._handle_result(
                    get_database_manager(),
                    run_id,
                    result,
                    sync_record=sync_record,
                    sync_record_id=sync_record.get("id"),
                    bgm=bgm,
                    notification_service=None,
                )
                return

            # last_response 为 None：全部轮次已完整记录 → 续跑通用循环
            bgm = self._build_bgm(sync_record)
            registry = get_tool_registry()
            defns = llm_assist_module.register_match_tools(registry, bgm)
            tools_schemas = [d.to_schema() for d in defns]

            # 缺失工具补执行（仅 readonly，写/终止性工具在续跑 loop 中自然触发）
            for tc in replay_result.missing_tool_calls:
                await self._replay_missing_tool(tc, registry, replay_result.messages)

            # 续跑 loop（从 replay 重建消息续跑）
            chat_fn = llm_assist_module._build_default_chat_fn()
            span_recorder = llm_assist_module._SpanRecorder(run_id)
            result = await loop_run(
                chat_fn=chat_fn,
                tools_schemas=tools_schemas,
                tool_calls_fn=registry.execute_batch,
                max_iterations=remaining,
                tool_choice_terminal="submit_suggestion",
                seed_messages=replay_result.messages,
                span_recorder=span_recorder,
            )
            llm_assist_module._handle_result(
                get_database_manager(),
                run_id,
                result,
                sync_record=sync_record,
                sync_record_id=sync_record.get("id"),
                bgm=bgm,
                notification_service=None,
            )
        except Exception as e:
            logger.error(f"🤖 恢复续跑 {run_id} 异常: {e}")
            repo.increment_attempts(run_id)

    async def _replay_missing_tool(
        self, tool_call: dict, registry, messages: list
    ) -> None:
        """补执行单条缺失的只读工具调用（readonly 校验）。

        F4：执行结果作为 ``Message(role="user", content=[ToolResultBlock(...)])``
        追加到 ``messages``，保证 assistant(tool_use) 后存在对应的 tool_result，
        符合会话协议（每条 tool_use 有且仅有一条 tool_result）。
        """
        name = (tool_call or {}).get("name")
        if not name:
            return
        defn = registry.get(name)
        if defn is None or defn.access != "read":
            # 非只读（write/terminal）不重放，续跑 loop 中自然触发
            return
        args = (tool_call or {}).get("input") or {}
        tool_use_id = (tool_call or {}).get("id", "")
        try:
            result = await registry.execute(name, args)
            content = str(result)
            is_error = False
        except Exception as e:
            logger.debug(f"🤖 恢复补执行工具 {name} 失败: {e}")
            content = f"工具执行失败: {type(e).__name__}"
            is_error = True
        messages.append(
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id=tool_use_id, content=content, is_error=is_error
                    )
                ],
            )
        )

    # ------------------------------------------------------------------
    # 正常处理
    # ------------------------------------------------------------------

    async def _process_run(self, run: dict) -> None:
        """处理单条 pending run：查 sync_record → llm_assist.run → 异常重试。"""
        run_id = run["run_id"]
        repo = get_database_manager().agent_runs

        sync_record = self._get_sync_record(run.get("sync_record_id"))
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
            thinking_level = match_cfg.get("llm_match_thinking_level") or "medium"
            # atomic_claim / 状态流转 / 落库均在 llm_assist.run 内部完成
            await llm_assist_module.run(
                run_id,
                sync_record=sync_record,
                bgm=bgm,
                thinking_level=thinking_level,
            )
        except Exception as e:
            logger.error(f"🤖 处理 run {run_id} 异常: {e}")
            attempts = repo.increment_attempts(run_id)
            if attempts >= 3:
                repo.mark_failed(run_id, stop_reason="failed", last_error=str(e)[:500])

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
            from app.core.accounts import get_active_bangumi_config
            from app.utils.bangumi_api import BangumiApi

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

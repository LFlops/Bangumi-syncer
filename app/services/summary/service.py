"""Summary 生成服务 —— 编排数据库查询、LLM 调用和通知发送。"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from app.core.database import database_manager
from app.core.logging import logger

from ..llm import Message, get_llm_client
from ..memory.service import MemoryService
from ..notification_service import notification_service
from .models import SummaryJobConfig, SummaryRecord

# 内部常量 —— 用户可自定义的 prompt 结构，不暴露到 config.ini
_USER_PROMPT_TEMPLATE = (
    "{date_from} 至 {date_to} 观影记录（共 {record_count} 条）：\n\n{records}"
)

_MEMORY_SECTION = "## 历史执行上下文"
_OVERLAP_NOTE = "以下记录已在上次总结中覆盖，可简述或跳过，不必重复展开：\n"


class SummaryService:
    """生成 AI 驱动的追番观影总结。"""

    def __init__(self):
        # 记忆统一入口（extractor/retriever 是其内部组件，业务层不直接碰 repository）
        self.memory = MemoryService(
            database_manager.memory, database_manager.sync_records
        )

    @property
    def llm_client(self):
        """每次取模块级单例——LLM 配置保存会 reset_llm_client()，缓存实例会失效。"""
        return get_llm_client()

    # ------------------------------------------------------------------
    # 查询与构建（Phase 2.0.2 拆解，execute_job / generate_summary 共用）
    # ------------------------------------------------------------------

    def _query_records(
        self, job_config: SummaryJobConfig
    ) -> tuple[list[SummaryRecord], str, str]:
        """计算日期范围并查询记录，返回 (records, date_from, date_to)。"""
        now = datetime.now()
        date_from = (now - timedelta(days=job_config.lookback_days)).strftime(
            "%Y-%m-%d"
        )
        date_to = now.strftime("%Y-%m-%d")

        records = database_manager.get_records_in_date_range(
            date_from=date_from,
            date_to=date_to,
            limit=job_config.max_records,
            user_name=job_config.user_name.strip() or None,
        )
        converted = [
            SummaryRecord(
                id=r["id"],
                timestamp=r["timestamp"],
                user_name=r["user_name"],
                title=r["title"],
                bgm_title=r.get("bgm_title") or "",
                season=r["season"],
                episode=r["episode"],
                media_type=r.get("media_type") or "episode",
                source=r["source"],
                status=r["status"],
                consumed_run_id=r.get("consumed_run_id"),
            )
            for r in records
        ]
        return converted, date_from, date_to

    def _build_messages(
        self,
        records: list[SummaryRecord],
        system_prompt: str,
        date_from: str,
        date_to: str,
    ) -> list[Message]:
        """格式化记录并构建 system + user 两条消息。"""
        records_text = self._format_records(records)
        user_content = _USER_PROMPT_TEMPLATE.format(
            date_from=date_from,
            date_to=date_to,
            records=records_text,
            record_count=len(records),
        )
        return [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_content),
        ]

    def _build_memory_context(
        self, job_config: SummaryJobConfig, task_id: str, records: list[SummaryRecord]
    ) -> str:
        """检索历史记忆并格式化为注入文本（含重叠标注）。"""
        # 关键词 = 今日明细标题提取（规则提取，bgm_title 去重取前 5）
        keywords: list[str] = []
        seen: set[str] = set()
        for r in records:
            t = r.bgm_title
            if t and t not in seen:
                seen.add(t)
                keywords.append(t)
            if len(keywords) >= 5:
                break
        past_memories = self.memory.retrieve(
            task_type="summary",
            task_id=task_id,
            limit=job_config.memory_limit,
            keywords=keywords,
        )
        context = self.memory.format_memory_context(past_memories)

        # 窗口重叠标注：今日明细中已被消费的记录提示简述/跳过
        overlaps = self.memory.find_overlaps(records)
        if overlaps:
            lines = "\n".join(
                f"- {r.bgm_title} S{r.season}E{r.episode}"
                f"（已消费于总结 {r.consumed_run_id[:8]}）"
                for r in overlaps[:20]
            )
            context = f"{context}\n{_OVERLAP_NOTE}{lines}"
        return context

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def generate_summary(self, job_config: SummaryJobConfig) -> dict:
        """查询数据库，格式化记录，调用 LLM（预览用，不含记忆注入）。

        返回字典，包含以下键：summary_text、model、usage、record_count、
        date_from、date_to。
        """
        records, date_from, date_to = self._query_records(job_config)
        system_prompt = job_config.system_prompt.strip()
        if not system_prompt:
            system_prompt = SummaryJobConfig.system_prompt
        messages = self._build_messages(records, system_prompt, date_from, date_to)

        response = await self.llm_client.chat(
            messages,
            job_name=job_config.name,
        )

        return {
            "summary_text": response.content,
            "model": response.model,
            "usage": response.usage,
            "latency_ms": response.latency,
            "record_count": len(records),
            "date_from": date_from,
            "date_to": date_to,
        }

    async def execute_job(self, job_config: SummaryJobConfig) -> None:
        """完整执行：查询 → 注入记忆 → 调 LLM → 提取记忆 → 发送通知。

        失败语义（对齐 spec 2.0.2 失败语义表）：
        - 查询/构建阶段失败：发 summary_job_failed 并终止（配置问题需告知用户）；
        - chat 失败：不写标记、不发通知（下次调度重新总结）；
        - 记忆提取失败：仅记日志，不影响通知（best-effort）；
        - 通知失败：不二次通知（消费标记已写，投递走通知重试/告警兜底）。
        """
        task_id = f"summary-{job_config.name}"
        # === 1-3. 查询明细 + 注入记忆 + 构建消息 ===
        try:
            records, date_from, date_to = self._query_records(job_config)

            # 注入历史上下文（memory_enabled 开关短路，不调 retrieve）
            memory_context = ""
            if job_config.memory_enabled:
                memory_context = self._build_memory_context(
                    job_config, task_id, records
                )

            # 历史上下文拼进 system prompt（多 system message 对 OpenAI 兼容端点不安全）
            system_prompt = job_config.system_prompt.strip()
            if not system_prompt:
                system_prompt = SummaryJobConfig.system_prompt
            if memory_context:
                system_prompt = (
                    f"{_MEMORY_SECTION}\n{memory_context}\n\n{system_prompt}"
                )
            messages = self._build_messages(records, system_prompt, date_from, date_to)
        except Exception as e:
            logger.error(f"Summary job '{job_config.name}' failed: {e}")
            summary_text = (
                f"追番总结任务执行异常：{e}\n"
                "请检查任务配置（Cron 表达式、回溯天数等）是否正确。"
            )
            self._send_failure_notification(
                job_config,
                summary_text,
                inbox_type="summary_job_failed",
                inbox_title=f"追番总结异常：{job_config.name}",
                inbox_body="执行异常，请检查任务配置",
            )
            return

        # === chat：失败不写标记、不发通知（重新总结）===
        try:
            response = await self.llm_client.chat(
                messages,
                job_name=job_config.name,
            )
        except Exception as e:
            logger.error(f"LLM chat failed for '{job_config.name}': {e}")
            return

        # === 4. 提取记忆（读写同开关：memory_enabled=false 不注入也不写入）===
        if job_config.memory_enabled:
            run_id = str(uuid4())  # 单次执行的唯一标识（记忆写入与消费标记共用）
            try:
                await self.memory.extract_and_store(
                    task_type="summary",
                    task_id=task_id,
                    run_id=run_id,
                    messages=messages,  # 完整上下文：缓存前缀 + 摘要来源
                    response=response,  # 响应：summary 生成 + full_text 存储
                    outcome="success",
                    tokens_used=response.usage.total_tokens
                    if response.usage
                    else 0,
                    record_ids=[r.id for r in records],  # 同一事务标记消费
                )
            except Exception as e:
                logger.error(f"Failed to store memory: {e}")

        # === 5. 通知：失败不二次通知（消费标记已写，投递走通知重试/告警兜底）===
        try:
            self._dispatch_notification(
                job_config, response, records, date_from, date_to
            )
        except Exception as e:
            logger.error(f"Notification failed for '{job_config.name}': {e}")

    def _dispatch_notification(
        self,
        job_config: SummaryJobConfig,
        response,
        records: list[SummaryRecord],
        date_from: str,
        date_to: str,
    ) -> None:
        """空内容→失败通知 / 正常→成功通知（保持既有失败语义）。"""
        if not response.content and not response.model:
            summary_text = (
                "AI 追番总结生成失败：LLM 返回空内容（所有重试已耗尽）。\n"
                "请检查 LLM 配置中的 api_base、api_key 是否正确，"
                "以及网络连通性。"
            )
            logger.error(
                f"Summary job '{job_config.name}' LLM 返回空内容，发送失败提示通知"
            )
            self._send_failure_notification(
                job_config,
                summary_text,
                inbox_type="summary_llm_failed",
                inbox_title=f"追番总结失败：{job_config.name}",
                inbox_body="LLM 返回空内容，请检查 API 地址和密钥",
            )
            return

        result = {
            "summary_text": response.content,
            "model": response.model,
            "usage": response.usage,
            "latency_ms": response.latency,
            "record_count": len(records),
            "date_from": date_from,
            "date_to": date_to,
        }
        self._send_success_notification(job_config, result)

    def _send_success_notification(
        self, job_config: SummaryJobConfig, result: dict
    ) -> None:
        """发送成功通知（webhook + 邮件）。

        P5：通过 notification_service.notify() 统一入口发送，仅走 webhook/email 渠道，
        不写站内信（write_in_app=False）。
        """
        user_name = job_config.user_name.strip() if job_config.user_name else ""
        usage = result["usage"]
        notification_service.notify(
            f"watching_summary_{job_config.name}",
            source="summary",
            skip_cooldown=True,
            write_in_app=False,
            job_name=job_config.name,
            user_name=user_name,
            summary_text=result["summary_text"],
            date_range=f"{result['date_from']} ~ {result['date_to']}",
            record_count=result["record_count"],
            lookback_days=job_config.lookback_days,
            model=result["model"],
            tokens_used=usage.total_tokens if usage else 0,
        )

    def _send_failure_notification(
        self,
        job_config: SummaryJobConfig,
        summary_text: str,
        *,
        inbox_type: str,
        inbox_title: str,
        inbox_body: str = "",
    ) -> None:
        """发送失败通知（webhook + 邮件 + 收件箱）。

        P4.7：通过 notification_service.notify() 统一入口发送，替代原先的
        get_notifier().send_notification_by_type() + database_manager.insert_notification()
        显式双调用。webhook/email 类型为 watching_summary_{name}（按 job 配置段），
        站内信 type 由 inbox_type 显式指定（按失败原因），两者解耦。
        """
        notification_service.notify(
            f"watching_summary_{job_config.name}",
            source="summary",
            skip_cooldown=True,
            in_app_type=inbox_type,
            in_app_title=inbox_title,
            in_app_body=inbox_body or summary_text,
            job_name=job_config.name,
            user_name=job_config.user_name.strip() or "",
            summary_text=summary_text,
            date_range="",
            record_count=0,
            lookback_days=job_config.lookback_days,
            model="",
            tokens_used=0,
        )

    def _format_records(self, records: list[SummaryRecord]) -> str:
        """将同步记录格式化为紧凑的文本表格。"""
        if not records:
            return "（无记录）"
        lines = []
        for r in records:
            ts = str(r.timestamp)[:16]
            user = r.user_name
            title = r.title
            bgm = r.bgm_title
            display_title = f"{title}（{bgm}）" if bgm and bgm != title else title
            if r.media_type == "movie":
                ep_label = "剧场版"
            else:
                ep_label = f"S{r.season}E{r.episode}"
            line = f"[{ts}] {user} | {display_title} | {ep_label} | {r.source} | {r.status}"
            lines.append(line)
        return "\n".join(lines)


# 单例
summary_service = SummaryService()

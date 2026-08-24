"""记忆提取器：总结执行成功后提炼一行摘要并写入记忆（含消费标记）。"""

from __future__ import annotations

from app.core.database.agent_memory import AgentMemoryRepository
from app.core.logging import logger
from app.services.llm import Message, get_llm_client
from app.services.llm.models import ChatResponse

from .models import MemoryEntry

_SUMMARY_PROMPT = (
    "请用一句话总结以下追番总结的内容（不超过 100 字），保留关键信息："
    "看了哪些番剧、进度、异常情况。必须列出本次涉及的全部番剧名"
    "（进度可从简，番剧名供后续关键词检索召回本条记忆）。只输出摘要本身。"
)

_SUMMARY_MAX_LEN = 200


class MemoryExtractor:
    def __init__(self, repo: AgentMemoryRepository, llm_client=None):
        self._repo = repo
        # 测试可注入；None 时 _summarize 内现取全局单例（LLM 配置保存会
        # reset_llm_client()，缓存实例会滞留旧 api_base/key）
        self._llm = llm_client

    async def extract_and_store(
        self,
        task_type: str,
        task_id: str,
        run_id: str,
        messages: list[Message],  # 总结调用的完整对话上下文（缓存前缀 + 摘要来源）
        response: ChatResponse,  # 总结响应（含全文 content）
        outcome: str,
        tokens_used: int,
        record_ids: list[int],  # 今日明细记录 id（store_and_mark 标记消费用）
        job_name: str | None = None,  # 摘要调用用量归属 llm_usage 用
    ) -> None:
        summary = await self._summarize(messages, response, job_name=job_name)
        if not summary:
            return  # 空响应（LLM 重试耗尽）不写记忆，避免无效条目
        # 原子单元：INSERT 记忆 + 标记消费（同一事务，见 store_and_mark）
        self._repo.store_and_mark(
            MemoryEntry(
                task_type=task_type,
                task_id=task_id,
                run_id=run_id,
                summary=summary,
                full_text=response.content,  # 全文存 full_text（回溯用，不注入）
                outcome=outcome,
                tokens_used=tokens_used,
            ),
            record_ids=record_ids,
        )
        # 清理旧记忆（独立 best-effort 事务，失败不回滚上面的 run）
        self._repo.prune(task_type, task_id, keep=1000)

    async def _summarize(
        self,
        messages: list[Message],
        response: ChatResponse,
        job_name: str | None = None,
    ) -> str:
        """一行摘要：复用总结调用的完整对话上下文作前缀，命中 LLM prompt 缓存。

        缓存利用：摘要调用紧跟总结调用（同一 execute_job 内，Anthropic 5 分钟
        TTL / OpenAI 自动前缀缓存）——完整历史作前缀，输入 token 按缓存价格，
        且无截断信息损失。job_name 使摘要调用 token 在 llm_usage 中归属任务
        （与主调用同组，用量统计口径完整）。
        """
        if not response.content:
            return ""
        try:
            summary_messages = list(messages)
            summary_messages.append(Message(role="assistant", content=response.content))
            summary_messages.append(Message(role="user", content=_SUMMARY_PROMPT))
            llm = self._llm or get_llm_client()
            resp = await llm.chat(summary_messages, job_name=job_name)
            if resp.content:
                return resp.content.strip()[:_SUMMARY_MAX_LEN]
        except Exception as e:
            logger.warning(f"摘要 LLM 调用失败，使用规则截取: {e}")
        return response.content.strip()[:_SUMMARY_MAX_LEN]  # 规则兜底：截断

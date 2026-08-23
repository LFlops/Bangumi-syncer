"""记忆读取器：检索历史记忆、格式化注入文本、识别窗口重叠。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.database.agent_memory import AgentMemoryRepository

from .models import MemoryEntry

if TYPE_CHECKING:
    # 仅类型注解用；运行时导入会经 summary 包 __init__ 回到 memory.retriever 形成环
    from app.services.summary.models import SummaryRecord


class MemoryRetriever:
    def __init__(self, repo: AgentMemoryRepository):
        self._repo = repo

    def retrieve(
        self,
        task_type: str,
        task_id: str,
        limit: int = 5,
        keywords: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """检索历史记忆：recent 取 limit 条（连续性）+ keywords 命中全保留（相关性）。

        keywords 命中**不占 memory_limit 额度**——recent 是"上下文连续性"、
        keywords 是"主题相关性"（今日明细标题捞窗口外相关历史），目的不同都注入；
        总量 = limit + keywords 命中数（命中通常少，token 可控）。
        （Phase 2.3 引入 feedback 后，此处再增加"feedback 全量优先"）
        """
        entries: list[MemoryEntry] = []

        # 1. 最近 N 次同任务执行
        entries.extend(self._repo.get_recent(task_type, task_id, limit=limit))

        # 2. 关键词搜索（FTS5，task_type 过滤；命中不占 limit 额度）
        if keywords:
            clean = [k for k in keywords if k and k.strip()]  # 过滤空字符串
            if clean:
                entries.extend(
                    self._repo.search_fts(clean, task_type=task_type, limit=limit)
                )

        # 3. 去重（按 run_id，防双路径命中）；keywords 命中不占额度不收束
        return self._deduplicate_and_rank(entries)

    def _deduplicate_and_rank(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        """按 run_id 去重，保留顺序（recent 在前、keywords 命中随后）。

        recent 条数已在 get_recent(limit) 源头受限；keywords 命中不占额度
        全部保留（phase3.x 反馈通道的优先排序也在此扩展）。
        """
        seen: set[str] = set()
        ranked: list[MemoryEntry] = []
        for e in entries:
            if e.run_id in seen:
                continue
            seen.add(e.run_id)
            ranked.append(e)
        return ranked

    def format_memory_context(self, entries: list[MemoryEntry]) -> str:
        """MemoryEntry 列表 → 注入文本（每条一行）。

        phase3.x 引入用户反馈后，此处增加 `[用户反馈]` 前缀标记
        （见 specs/agent-phase3-summary-enhanced.md）。
        """
        return "\n".join(f"- {e.summary}" for e in entries)

    def find_overlaps(self, records: list[SummaryRecord]) -> list[SummaryRecord]:
        """返回今日明细中已被消费的记录（consumed_run_id IS NOT NULL）。

        数据基础：2.0.1 的 store_and_mark 在每次总结成功后标记 sync_records。
        精确到集、无窗口近似——无论多早被消费都能命中（covered 方案的
        "最近 K 条并集"对超过窗口的旧集会漏标）。
        """
        return [r for r in records if r.consumed_run_id is not None]

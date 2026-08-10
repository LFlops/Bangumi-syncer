# Phase 2.0.1: 写入记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 1（ContentBlock 类型）
> 交付物：`agent_working_memory` 表 + repository + MemoryExtractor（写入侧完整能力）
> 执行时机：Phase 1.1 之后、Phase 2.0.2 之前（一个 phase 只做一件事）
> 设计总览见 `agent-phase2-memory.md`（数据库 schema、记忆策略权衡）

## 目标

记忆的**写入侧**：建表（含 FTS5 同步触发器）、repository 写入能力、MemoryExtractor（成功路径摘要生成、covered 覆盖列表）。读取与注入在 Phase 2.0.2。

## MemoryEntry 模型

```python
# app/services/memory/models.py

from pydantic import BaseModel

class CoveredItem(BaseModel):
    """covered 条目：本次覆盖的一条记录（Pydantic：类型安全 + 序列化一站式）。"""
    title: str
    season: int | None = None      # 电影可为 None
    episode: int | None = None

@dataclass
class MemoryEntry:
    id: int | None = None
    task_type: str = ""
    task_id: str = ""
    run_id: str = ""
    summary: str = ""
    covered: list[CoveredItem] = field(default_factory=list)
    outcome: str = "success"                            # success | partial | feedback
    tokens_used: int = 0
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "MemoryEntry":
        """从 DB row 构造（covered 做 json.loads + model_validate 容错，失败兜底空列表）。"""
        ...
```

- **`covered` 用 Pydantic `CoveredItem`**（不是 dict + 注释）：类型安全（ty 检查字段）、序列化一站式（`model_dump`/`model_validate`）、与项目 app/models/ 的 Pydantic 惯例一致
- 入库：`json.dumps([c.model_dump() for c in entry.covered])`；读取：`[CoveredItem.model_validate(x) for x in json.loads(raw or "[]")]`
- 无 `decisions_taken`/`error_message`（无消费方，见总览"字段取舍"）

## 数据库（migration）

表 + 索引 + FTS5 + **同步触发器**（`app/core/database/connection.py` 的 `__ensure_agent_memory()`）：

- schema 见总览文档（`agent-phase2-memory.md` 数据库一节）
- **触发器是必须的**：`agent_memory_fts` 是 external content 表，无触发器则 INSERT 后 FTS 不更新、`search_fts` 搜不到新记录（DELETE/UPDATE 同理）
- **prune 默认 1000 条/任务**（FTS5 解耦检索性能与保留量，1000 条 ≈ 200KB 无压力，每日任务 ≈ 2.7 年历史）

## AgentMemoryRepository

```python
# app/core/database/agent_memory.py（继承 BaseRepository）

class AgentMemoryRepository:
    def insert(self, entry: MemoryEntry) -> int: ...
    def prune(self, task_type: str, task_id: str, keep: int = 1000) -> int:
        """超出 keep 的旧记录：先 INSERT INTO archive，再 DELETE 主表
        （触发器同步删 FTS 索引）。默认 1000 条/任务（热记忆窗口）。
        跳过 outcome='feedback' 条目：用户偏好长期有效，不归档不删除。"""
    def get_recent(self, task_type: str, task_id: str, limit: int = 5) -> list[MemoryEntry]:
        """按 task 取最近 N 条（created_at DESC）。"""
    def search_fts(self, query: str, task_type: str, limit: int = 5) -> list[MemoryEntry]:
        """FTS5 全文检索（热记忆），按 task_type 过滤（JOIN 主表取全字段）。"""
    def search_archive(
        self, task_type: str | None, keywords: str, limit: int = 50
    ) -> list[MemoryEntry]:
        """冷记忆检索（LIKE，无 FTS）——Phase 3 失败定位等查全量历史用。"""
    def get_latest(self, task_type: str, task_id: str) -> MemoryEntry | None:
        """最近一条（Phase 2.5 使用）。"""
```

- `search_fts` **必须带 task_type 过滤**（FTS5 查询条件或 JOIN 后过滤），避免跨任务命中（summary 任务搜到 sync/diagnostic 的记忆）
- `prune` 在每次 insert 后调用：**降级到冷存储而非删除**（归档表见总览），主表保持有界、历史可追溯
- `search_archive`：冷记忆无 FTS，LIKE 检索 + task_type 可选过滤——面向低频"查全量历史"场景（热路径仍走 FTS）

## MemoryExtractor

```python
# app/services/memory/extractor.py

_SUMMARY_PROMPT = (
    "请用一句话总结以下追番总结的内容（不超过 50 字），保留关键信息："
    "看了哪些番剧、进度、异常情况。只输出摘要本身。"
)

class MemoryExtractor:
    def __init__(self, repo: AgentMemoryRepository, llm_client=None):
        self._repo = repo
        self._llm = llm_client  # 缺省用 get_llm_client()（延迟获取，避免 import 环）

    async def extract_and_store(
        self,
        task_type: str,
        task_id: str,
        run_id: str,
        llm_response: str,
        records: list[SyncRecord],  # 本次覆盖的 sync records（covered 来源）
        outcome: str,
        tokens_used: int,
    ) -> None:
        summary = await self._summarize(llm_response)
        if not summary:
            return  # 空响应（LLM 重试耗尽）不写记忆，避免无效条目
        covered = self._extract_covered(records)
        await self._repo.insert(MemoryEntry(
            task_type=task_type,
            task_id=task_id,
            run_id=run_id,
            summary=summary,
            covered=covered,
            outcome=outcome,
            tokens_used=tokens_used,
        ))
        # 清理旧记忆（每个 task 最多保留 1000 条）
        await self._repo.prune(task_type, task_id, keep=1000)

    async def _summarize(self, llm_response: str) -> str:
        """一行摘要：LLM 内置模板生成；LLM 不可用/失败时规则截取兜底。"""
        if not llm_response:
            return ""
        try:
            resp = await self._llm.chat([
                Message(role="system", content=_SUMMARY_PROMPT),
                Message(role="user", content=llm_response[:2000]),
            ])
            if resp.content:
                return resp.content.strip()[:200]
        except Exception:
            logger.warning("摘要 LLM 调用失败，使用规则截取", exc_info=True)
        return llm_response.strip()[:200]  # 规则兜底：截断

    def _extract_covered(self, records: list[SyncRecord]) -> list[CoveredItem]:
        """covered：本次覆盖的记录列表（规则提取，非 LLM）。"""
        return [
            CoveredItem(
                title=r.bgm_title or r.ori_title or "",
                season=r.season,
                episode=r.episode,
            )
            for r in records
        ]
```

### 关键设计点（review 修复）

- **签名含 `records`**：covered 从 records 规则提取（非 LLM）
- **covered 的 season/episode 可为 None**（电影）：照实存，比对时（Phase 2.5）tuple 含 None 参与匹配即可
- **`_summarize` 实现**：开发者内置模板（`_SUMMARY_PROMPT` 写死，不暴露配置）+ LLM 调用 + **规则截断兜底**（LLM 失败不抛异常）
- **空响应不写记忆**：LLM 重试耗尽返回空响应时跳过（避免无效条目）
- **容错**：`execute_job` 调 `extract_and_store` 时整体 try/except 包裹（记忆写入失败不影响主流程的 `_dispatch_notification`，见 2.0.2）
- **成本说明**：每次成功执行多一次摘要 LLM 调用（小 prompt ~50 token），这是"摘要存"策略的固有成本；规则兜底保证 LLM 不可用时功能不中断

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/services/memory/__init__.py` | 记忆模块包 |
| 新增 | `app/services/memory/models.py` | MemoryEntry dataclass |
| 新增 | `app/services/memory/extractor.py` | MemoryExtractor（_summarize/covered/空响应跳过） |
| 新增 | `app/core/database/agent_memory.py` | AgentMemoryRepository（insert/prune/get_recent/search_fts/get_latest） |
| 修改 | `app/core/database/connection.py` | `__ensure_agent_memory()` migration（主表 + 归档表 + 索引 + FTS5 + 触发器） |

> 总计：新增 4 个文件，修改 1 个文件

## BDD 测试场景

### Scenario W1 成功路径写入
- **Given** extract_and_store 传入 llm_response + records
- **When** 执行
- **Then** `agent_working_memory` 新增一条：`summary` 为摘要、`covered` 含 records 的结构化列表、`outcome="success"`

### Scenario W2 摘要 LLM 失败规则兜底
- **Given** `_summarize` 的 LLM 调用抛异常
- **When** 执行
- **Then** 不抛异常，summary 为规则截断（前 200 字符）

### Scenario W3 空响应不写记忆
- **Given** llm_response 为空（LLM 重试耗尽）
- **When** extract_and_store
- **Then** 不写入记忆（无无效条目）

### Scenario W4 FTS5 触发器同步
- **Given** insert 一条记忆
- **When** `search_fts` 查询该记录的关键词
- **Then** 能命中（触发器已同步 FTS 表）

### Scenario W5 写入失败不影响调用方
- **Given** extract_and_store 抛异常（如 DB 错误）
- **When** execute_job 调用处（外层 try/except）
- **Then** 调用方不中断，继续执行后续流程

### Scenario W6 prune 归档而非删除
- **Given** 同一 task 写入 1005 条
- **When** 每次 insert 后 prune(keep=1000)
- **Then** 主表仅保留最近 1000 条
- **And** archive 表包含被归档的 5 条（含原 created_at/run_id）

### Scenario W9 feedback 条目不被 prune
- **Given** 主表含 1 条 `outcome="feedback"` 的旧条目（在 keep 窗口外）
- **When** prune(keep=1000)
- **Then** feedback 条目仍在主表（不归档不删除）
- **And** archive 表不含该条目

### Scenario W8 归档可检索
- **Given** archive 表含某关键词记录
- **When** `search_archive(task_type, keywords)`
- **Then** 能命中（LIKE 检索冷记忆）

### Scenario W7 covered 含 None 字段
- **Given** records 含电影（season/episode 为 None）
- **When** 提取 covered
- **Then** covered 条目照实存 None，不报错

## 验证方式

1. 单元测试：W1-W7 全部通过
2. 手动：触发一次 summary 成功执行，检查 `agent_working_memory` 表记录（summary/covered/outcome 正确）
3. 手动：`sqlite3` 验证 FTS 触发器（insert 后 `SELECT * FROM agent_memory_fts` 有对应行）

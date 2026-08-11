# Phase 2.0.1: 写入记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 1（LLM 层：Message/LLMClient，`_summarize` 仅发纯文本 chat，不需要 ContentBlock 新类型）
> 交付物：`agent_working_memory` 表 + repository + MemoryExtractor（写入侧完整能力）
> 执行时机：Phase 1.1 之后、Phase 2.0.2 之前（一个 phase 只做一件事）
> 设计总览见 `agent-phase2-memory.md`（数据库 schema、记忆策略权衡）

## 目标

记忆的**写入侧**：建表（含 FTS5 同步触发器）、repository 写入能力、MemoryExtractor（成功路径摘要生成）、剧集消费标记（mark_consumed）。读取与注入在 Phase 2.0.2。

## MemoryEntry 模型

```python
# app/services/memory/models.py

@dataclass
class MemoryEntry:
    id: int | None = None
    task_type: str = ""
    task_id: str = ""
    run_id: str = ""
    summary: str = ""
    outcome: str = "success"                            # success | partial（feedback 取值 Phase 2.3 引入）
    tokens_used: int = 0
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "MemoryEntry":
        """从 DB row 构造。"""
        ...
```

- 无 `covered`（窗口重叠去重用剧集消费标记，见总览"剧集消费标记"）；无 `decisions_taken`/`error_message`（无消费方，见总览"字段取舍"）

## 数据库（migration）

表 + 索引 + FTS5 + **同步触发器**（`app/core/database/connection.py` 的 `__ensure_agent_memory()`）：

- schema 见总览文档（`agent-phase2-memory.md` 数据库一节）
- **触发器是必须的**：`agent_memory_fts` 是 external content 表，无触发器则 INSERT 后 FTS 不更新、`search_fts` 搜不到新记录（DELETE/UPDATE 同理）
- **prune 默认 1000 条/任务**（FTS5 解耦检索性能与保留量，1000 条 ≈ 200KB 无压力，每日任务 ≈ 2.7 年历史）
- **消费标记迁移（幂等）**：`__ensure_sync_records_consumed`——`PRAGMA table_info(sync_records)` 检查 `consumed_run_id`/`consumed_at` 列，缺失则 `ALTER TABLE ADD COLUMN`（老库自动补列，不加约束无风险；不建辅助表——单值标记加列无 join、生命周期一致，辅助表优势是 YAGNI）

## AgentMemoryRepository

```python
# app/core/database/agent_memory.py（继承 BaseRepository）

class AgentMemoryRepository:
    def insert(self, entry: MemoryEntry) -> int: ...
    def prune(self, task_type: str, task_id: str, keep: int = 1000) -> int:
        """超出 keep 的旧记录：先 INSERT INTO archive，再 DELETE 主表
        （触发器同步删 FTS 索引）。默认 1000 条/任务（热记忆窗口）。
        （Phase 2.3 引入 feedback 条目后，此处增加跳过逻辑）

        事务性：归档 INSERT 与主表 DELETE 在**同一事务**内（_run_write 提供），
        防止"删了未归档"丢数据——任一步失败整体回滚。"""
    def get_recent(self, task_type: str, task_id: str, limit: int = 5) -> list[MemoryEntry]:
        """按 task 取最近 N 条记忆（created_at DESC，朴素实现——
        Phase 2.3 引入 feedback 后此处增加排除逻辑）。"""
    def search_fts(self, query: str, task_type: str, limit: int = 5) -> list[MemoryEntry]:
        """FTS5 全文检索（热记忆），按 task_type 过滤（JOIN 主表取全字段）。"""
    def search_archive(
        self, task_type: str | None, keywords: str, limit: int = 50
    ) -> list[MemoryEntry]:
        """冷记忆检索（LIKE，无 FTS）——Phase 3 失败定位等查全量历史用。"""

- `search_fts` **必须带 task_type 过滤**（FTS5 查询条件或 JOIN 后过滤），避免跨任务命中（summary 任务搜到 sync/diagnostic 的记忆）
- `prune` 在每次 insert 后调用：**降级到冷存储而非删除**（归档表见总览），主表保持有界、历史可追溯
- `search_archive`：冷记忆无 FTS，LIKE 检索 + task_type 可选过滤——面向低频"查全量历史"场景（热路径仍走 FTS）

### SyncRecordsRepository.mark_consumed（剧集消费标记，归属 sync_records 表）

```python
# app/core/database/sync_records.py（操作 sync_records 表，表归对应 repo 管）

def mark_consumed(self, record_ids: list[int], run_id: str) -> int:
    """标记今日明细记录已被本次总结消费：
    UPDATE sync_records SET consumed_run_id=?, consumed_at=now WHERE id IN (...)。
    与 extract_and_store 同流程（记忆写成功才标记）。"""
```


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
        # 无 import 环（memory → llm.client → core.config，反向无依赖），可顶层 import
        self._llm = llm_client or get_llm_client()

    async def extract_and_store(
        self,
        task_type: str,
        task_id: str,
        run_id: str,
        llm_response: str,
        outcome: str,
        tokens_used: int,
    ) -> None:
        summary = await self._summarize(llm_response)
        if not summary:
            return  # 空响应（LLM 重试耗尽）不写记忆，避免无效条目
        await self._repo.insert(MemoryEntry(
            task_type=task_type,
            task_id=task_id,
            run_id=run_id,
            summary=summary,
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
```

### 关键设计点（review 修复）

- **无 covered**：窗口重叠去重改用剧集消费标记（`mark_consumed`，见 repository 与总览"剧集消费标记"）
- **`_summarize` 实现**：开发者内置模板（`_SUMMARY_PROMPT` 写死，不暴露配置）+ LLM 调用 + **规则截断兜底**（LLM 失败不抛异常）
- **空响应不写记忆**：LLM 重试耗尽返回空响应时跳过（避免无效条目）
- **容错**：`execute_job` 调 `extract_and_store` 时整体 try/except 包裹（记忆写入失败不影响主流程的 `_dispatch_notification`，见 2.0.2）
- **成本说明**：每次成功执行多一次摘要 LLM 调用（小 prompt ~50 token），这是"摘要存"策略的固有成本；规则兜底保证 LLM 不可用时功能不中断

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/services/memory/__init__.py` | 记忆模块包 |
| 新增 | `app/services/memory/models.py` | MemoryEntry dataclass |
| 新增 | `app/services/memory/extractor.py` | MemoryExtractor（_summarize/空响应跳过） |
| 新增 | `app/core/database/agent_memory.py` | AgentMemoryRepository（insert/prune/get_recent/search_fts/search_archive） |
| 修改 | `app/core/database/sync_records.py` | SyncRecordsRepository 增加 `mark_consumed`（消费标记）；summary `_query_records` 底层查询方法 SELECT 需带 `consumed_run_id`（SyncRecord 加字段后查询侧同步） |
| 修改 | `app/core/database/connection.py` | `__ensure_agent_memory()` migration（主表 + 归档表 + 索引 + FTS5 + 触发器）；`__ensure_sync_records_consumed`（consumed_run_id/consumed_at 幂等补列） |

> 总计：新增 4 个文件，修改 2 个文件

## BDD 测试场景

### Scenario W1 成功路径写入
- **Given** extract_and_store 传入 llm_response
- **When** 执行（随后 mark_consumed 标记今日明细）
- **Then** `agent_working_memory` 新增一条：`summary` 为摘要、`outcome="success"`
- **And** 今日明细的 sync_records 被标记 `consumed_run_id`（mark_consumed 同事务）

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

### Scenario W8 归档可检索
- **Given** archive 表含某关键词记录
- **When** `search_archive(task_type, keywords)`
- **Then** 能命中（LIKE 检索冷记忆）

### Scenario W9 feedback 条目不被 prune
- **Given** 主表含 1 条 `outcome="feedback"` 的旧条目（在 keep 窗口外）
- **When** prune(keep=1000)
- **Then** feedback 条目仍在主表（不归档不删除）
- **And** archive 表不含该条目

### Scenario W10 老库补列幂等（migration）
- **Given** 已有 sync_records 表（无 consumed_run_id/consumed_at 列，模拟老用户）
- **When** 启动 `__ensure_sync_records_consumed`
- **Then** 两列被 ALTER TABLE 补上
- **And** 再次执行幂等（列已存在，跳过）

## 验证方式

1. 单元测试：W1-W10 全部通过
2. 手动：触发一次 summary 成功执行，检查 `agent_working_memory` 表记录（summary/outcome 正确）与 sync_records 的 consumed_run_id 标记
3. 手动：`sqlite3` 验证 FTS 触发器（insert 后 `SELECT * FROM agent_memory_fts` 有对应行）

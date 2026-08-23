# Phase 2 记忆 spec 评审 · 定稿决议清单

> 评审对象：`agent-phase2-memory.md` / `agent-phase2.0.1-write-memory.md` /
> `agent-phase2.0.2-read-memory.md` / `agent-phase2.0.3-cleanup.md`
> 目的：把评审中发现的矛盾/缺口收敛为「决议 + 建议 + 待改 spec 位置」，作为进入开发前的定稿依据。
> 每条决议默认给出「推荐方案」，除非显式否决，否则按推荐方案改 spec。

---

## P0 · 阻断开发，必须先定稿

### D1. `database_manager` 如何暴露 memory / sync_records

- **问题**：2.0.2/2.0.3 代码写 `database_manager.memory`、`database_manager.sync_records.mark_consumed(...)`，
  但 facade（`app/core/database/__init__.py`）不对外暴露 repo 实例，只有私有 `self._sync` 与转发方法。
- **推荐**：**facade 新增公开 repo 属性**（对齐既有 `self.llm_usage` 公开属性先例），不动既有转发方法：
  - `DatabaseManager` 新增 `self.memory = AgentMemoryRepository(self._connection)`（公开属性，等价 `llm_usage` 的暴露方式）。
  - 新增公开别名 `self.sync_records = self._sync`（一行别名，避免改动既有 ~20 个 sync 转发方法；也可选择把 `_sync` 直接改名为 `sync_records`）。
  - `MemoryRetriever(repo)` / `MemoryService(memory_repo)` 的**构造签名**（`sync_records_repo`
    形参在 hy-review20260817 #7 中删除——消费标记经共享 connection 穿透式访问，不注入 repo），
    业务层直接传 `database_manager.memory`。
  - 2.0.2 `execute_job` 里 `database_manager.memory`、`database_manager.sync_records.mark_consumed(...)` **原样成立，无需改代码**。
- **否决**：不做 facade 转发方法（需新增 ~7 个 `*_memory`/`mark_consumed` 转发方法，且要改 MemoryRetriever/MemoryService 构造签名，代价大于收益）。
- **待改**：2.0.1 文件变更清单补一行 `app/core/database/__init__.py`（新增 `memory` + `sync_records` 公开属性）；2.0.3 `MemoryService` 实例化处注明传 `database_manager.memory` / `database_manager.sync_records`。

### D2. summary 链路用 dict 还是类型化对象

- **问题**：`_query_records` 返回 `list[dict]`，但 `keywords`/`record_ids`/`find_overlaps` 用属性访问且类型标注 `SyncRecord`；
  `SyncRecord`（`app/models/sync.py`）又缺 `bgm_title`/`consumed_run_id` 字段。
- **推荐**：**引入 `SummaryRecord` dataclass**（`app/services/summary/models.py`，与 `SummaryJobConfig` 同文件、同为 `@dataclass`），`_query_records` 返回 `list[SummaryRecord]`：
  - 字段：`id/timestamp/user_name/title/bgm_title/season/episode/media_type/source/status/consumed_run_id(str|None=None)`。
  - `find_overlaps(records: list[SummaryRecord]) -> list[SummaryRecord]`，`r.consumed_run_id` 属性访问；`keywords`/`record_ids`/`overlap_note` 同样属性访问。
  - `_format_records`（D3 重构）由 `r.get(...)` 改为 `r.xxx` 属性访问。
  - **共享方法 `get_records_in_date_range` 保持返回 `list[dict]` 不变**，由 `_query_records` 内部做一行 dict→`SummaryRecord` 转换（避免波及 14 处测试断言）。
  - **不改 `app/models/sync.py` 的 `SyncRecord`**（它是 API 模型、半死代码，与 summary 内部载体不匹配）；删掉 2.0.2 文件清单里"修改 `app/models/sync.py`"这一条。
- **为何 `@dataclass` 而非 `BaseModel`**：summary 内部数据载体，数据来自自有 SQL（可信）、不跨 I/O 边界、不需校验/序列化；与 `SummaryJobConfig`/`MemoryEntry` 风格一致。
- **待改**：2.0.2 `find_overlaps` 类型标注、`_format_records`（D3）、文件变更清单（删 `app/models/sync.py`、`summary/models.py` 补 `SummaryRecord`）。

### D3. execute_job 的 service 拆解要显式声明

- **问题**：2.0.2 `execute_job` 用 `self._query_records`/`self._build_messages`/`self.llm_client`/`self._dispatch_notification`，
  这些在当前 `SummaryService` 里都不存在（当前是单体 `generate_summary` + `get_llm_client()` + `_send_*_notification`）。
- **推荐**：在 2.0.2 开头新增「**前置重构（本 phase 内完成）**」小节，明确把 `generate_summary` 拆成（不改变对外行为）：
  - `_query_records(job_config) -> tuple[list[SummaryRecord], str, str]`（同步；返回 `records, date_from, date_to`）
  - `_build_messages(records, system_prompt) -> list[Message]`（同步；含既有 system + user）
  - `self.llm_client`（`__init__` 中 `get_llm_client()` 存入）
  - `_dispatch_notification(job_config, response, records, date_from, date_to) -> None`（同步；承载现有 `_send_success_notification`/`_send_failure_notification`，保持空内容→失败通知语义）
  - `self.memory_retriever`/`self.memory_extractor`（`__init__` 构造，与 `llm_client` 并列）
  - `generate_summary` 改为薄封装供 `test_summary_job`（`app/api/summary_jobs.py`）复用，test 端点签名/返回不变。
- **同步修正**：2.0.2 片段里 `records = await self._query_records(...)` 去 `await` 改 tuple 解构；`await self._dispatch_notification(...)` 去 `await`；`memory_extractor` 裸名改 `self.memory_extractor`。
- **待改**：2.0.2 新增“前置重构”小节（已写）+ 文件清单 `service.py` 行补重构说明。

### D4. 历史上下文注入不能产生第二条 system

- **问题**：`messages.insert(0, Message(role="system", content="## 历史执行上下文..."))` 与 `_build_messages` 已有的 system 形成两条 system；
  OpenAI provider 直接把多条 `role=system` 塞进 messages，多 system 对兼容端点不安全（Anthropic 在 provider 内 `\n\n` 合并、OpenAI 原样转发可能 400）。
- **推荐**：**拼进现有 system prompt 内容**，不新增 system message：
  - `execute_job` 第 3 步：`system_prompt = job_config.system_prompt`；若 `memory_context` 非空则 `system_prompt = f"## 历史执行上下文\n{memory_context}\n\n{system_prompt}"`，再 `_build_messages(records, system_prompt)`。
  - `overlap_note` 同理拼进 `memory_context`（在 `_build_messages` 之前），不独立插 system。
- **与 Phase 2.2 的关系**：**不涉及**——改在 summary service（2.0.2 先于 2.2，且语义上 memory_context 就是 system prompt 的一部分）；2.2 的 `openai_compat.py` 不动（不加多 system 合并防御，避免 YAGNI）。
- **待改**：2.0.2 `execute_job` 第 3 步 + `overlap_note` 注入顺序。

### D5. clear_task 消费标记的 run_id→task_id 定位

- **问题**：`consumed_run_id` 只存 run_id、不存 task_id；run_id→task_id 映射只存在于 `agent_working_memory` 与 `agent_working_memory_archive`。
  清消费标记必须先取该 task 的 run_id 集合，且需覆盖归档表、顺序敏感。
- **推荐**（已定稿）：
  - 消费标记清理折叠进 `AgentMemoryRepository.clear_task`（单一 `_run_write` 原子事务），**而非**独立 `SyncRecordsRepository.clear_consumed_by_task`（避免跨 repo 各 commit 破坏原子性；消费标记是记忆域数据，见 2.0.1）。
  - 顺序：① 收集 run_id（主表 + 归档表 UNION，必须在删表前取）；② 删主表；③ 删归档表；④ `UPDATE sync_records SET consumed_run_id = NULL WHERE consumed_run_id IN (run_ids)`（清标记，不删记录）。
  - 归档表 run_id 也参与（归档过的 run_id 可能仍被消费标记引用）。
  - `rename_task` 无需动消费标记（consumed_run_id 只关联 run_id，改名不改变 run_id）——维持现状。
- **待改**：2.0.3 `clear_task` 说明 + 文件清单（删 `sync_records.py` 行）+ C3/C12 场景。

### D6. API 透传漏了 `app/models/summary.py`

- **问题**：API 走 `SummaryJobCreate`/`SummaryJobUpdate`/`SummaryJobResponse`（Pydantic），均无 `memory_enabled`/`memory_limit`；
  2.0.2 文件清单只列 `services/summary/models.py`（内部 `SummaryJobConfig`），漏了 API 模型。
- **推荐**：`app/models/summary.py` 三个模型补 `memory_enabled: bool`（Create 默认 False / Update 为 Optional / Response 必填）、`memory_limit: int`（默认 5）。
- **待改**：2.0.2 文件变更清单 + 2.0.2 配置小节。

---

## P1 · 语义统一（不改会引入 bug 或误解）

### D7. 记忆写入 + 消费标记应同一事务（原子）

- **问题**：原 spec 把 `extract_and_store`（insert+prune）与 `mark_consumed` 拆成两次独立 `_run_write`，
  产生"记忆已写但标记未写"的中间态，被迫写容忍逻辑，反而更复杂。
- **推荐**（已定稿）：**折叠成单一事务**——
  - `store_and_mark(entry, record_ids)`：INSERT 记忆 + UPDATE sync_records 消费标记，同一 `_run_write`（run 的原子单元）。
  - `prune`：**独立 best-effort 事务**（维护性；失败不回滚已成功且已消耗 LLM token 的 run）。
  - 消费标记写/清都折叠进 memory repo（对称于 D5 的 clear_task），不设独立 `SyncRecordsRepository.mark_consumed`。
  - `_summarize`（async LLM）在事务外，先 summarize 再 `store_and_mark`。
- **待改**：2.0.1 `extract_and_store`/`store_and_mark`/W1/文件清单；2.0.2 `execute_job` 第 4 步 + 失败语义表；2.0.3 MemoryService 说明。

### D8. `_format_memory_context` 归属与调用方式

- **问题**：正文+文件清单说"归属 MemoryRetriever"，代码却 `self._format_memory_context(...)`（self=SummaryService），函数又定义成模块级。
- **推荐**：**归属 MemoryRetriever**，作为实例方法 `memory_retriever.format_memory_context(entries)`（去掉下划线或保留私有均可，但调用侧统一 `memory_retriever.xxx(...)`）。
  execute_job 里改为 `memory_context = memory_retriever.format_memory_context(past_memories)`。
- **待改**：2.0.2 `execute_job` + `_format_memory_context` 定义处。

### D9. `outcome="partial"` 无写入路径

- **问题**：`MemoryEntry.outcome` 注释 `success | partial`，但 2.0.1/2.0.2 永远写 `"success"`，失败不写。
- **推荐**（已定稿）：**删除 `partial`**（无写入路径，不留注释）；`outcome` 本阶段仅 `success`，`feedback` 由 Phase 2.3 引入（有明确写入路径）。
- **待改**：2.0.1 `MemoryEntry.outcome` 注释改为「本阶段仅 success；feedback 取值 Phase 2.3 引入」。

### D10. 删除 `consumed_at`（无读者，YAGNI）

- **问题**：`consumed_at` 被 `store_and_mark` 写入、migration 补列，但没有任何读取方（`find_overlaps` 只看 `consumed_run_id IS NOT NULL`）；且"何时消费"可由 `consumed_run_id` join 记忆表 `created_at` 推导（归档表也有 created_at）。
- **推荐**（已定稿）：**删除 `consumed_at`**，只留 `consumed_run_id`——migration 少一列、`store_and_mark` 少写一个字段。
- **待改**：总览"剧集消费标记"、2.0.1 migration/store_and_mark/W10/W11、connection.py 文件清单。

### D11. `find_overlaps` 去掉 async

- **问题**：`async def find_overlaps` 但无 await。
- **推荐**：改 `def`（同步），execute_job 里去掉 `await`。
- **待改**：2.0.2 `find_overlaps` 定义与调用。

### D12. `get_records_in_date_range` 的 SELECT 改动写清楚

- **问题**："SELECT 需带 consumed_run_id" 只被顺带提一句，无 BDD 场景。
- **推荐**：明确 `get_records_in_date_range` 增加 `consumed_run_id`；补一条 BDD「查询返回 dict 含 consumed_run_id 键」。
- **待改**：2.0.1 文件清单、2.0.2 新增/补 BDD。

### D13. prune 粒度表述统一

- **问题**：总览"主表 1000 条" vs "1000 条/任务" vs "每 task 最多 1000"。
- **推荐**：统一为「**每 (task_type, task_id) 各保留 1000 条**」；`prune(task_type, task_id, keep=1000)` 语义不变。
- **待改**：总览"记忆清理机制"/"FTS5 必要性"等小节措辞。

### D14. `tokens_used` 语义注释

- **问题**：`extract_and_store` 里 `tokens_used=response.usage.total_tokens` 是**总结调用**的 token，不是摘要调用。
- **推荐**：字段注释明确「记录本次总结生成的 token（摘要调用成本不单独计）」。
- **待改**：2.0.1 `extract_and_store` 注释 / `MemoryEntry.tokens_used`。

---

## P2 · 小修（可选，不改也能开发）

### D15. `search_fts` 的 task_type 过滤形态写死

- **推荐**：external content 表 + JOIN 主表后 `WHERE agent_working_memory.task_type = ?`（行数少，够用）。
- **待改**：2.0.1 `search_fts` 注释。

### D16. `find_overlaps` 是否下沉 SQL（已定稿：不做）

- **推荐**：维持现状（在已加载 `records` 上 Python 过滤，零额外查询）。**不做**，避免过度设计。
- **待改**：无。

---

## 决议状态追踪

| 编号 | 决议 | 状态 |
|---|---|---|
| D1 | facade 新增公开 repo 属性（memory + sync_records 别名） | ✅ 已定稿 |
| D2 | 引入 SummaryRecord dataclass（_query_records 返回 list[SummaryRecord]） | ✅ 已定稿 |
| D3 | 显式声明 service 拆解（_query_records/_build_messages/llm_client/_dispatch_notification） | ✅ 已定稿 |
| D4 | 历史上下文拼进现有 system（不新增第二条 system） | ✅ 已定稿 |
| D5 | clear_task 折叠进 memory repo 单一事务（run_id 收集先于删表，含归档） | ✅ 已定稿 |
| D6 | 补 app/models/summary.py 三模型 | ✅ 已定稿 |
| D7 | insert + 消费标记同一事务（prune 独立 best-effort） | ✅ 已定稿 |
| D8 | _format_memory_context 归 MemoryRetriever | ✅ 已定稿 |
| D9 | 删除 partial，仅 success | ✅ 已定稿 |
| D10 | 删除 consumed_at（无读者） | ✅ 已定稿 |
| D11 | find_overlaps 改 sync | ✅ 已定稿 |
| D12 | get_records_in_date_range SELECT 写清 + 补 BDD | ✅ 已定稿 |
| D13 | prune 粒度统一"每 (task_type,task_id) 1000" | ✅ 已定稿 |
| D14 | tokens_used 语义注释 | ✅ 已定稿 |
| D15 | search_fts task_type 过滤写死 | ✅ 已定稿 |
| D16 | find_overlaps 不下沉 SQL | ✅ 已定稿 |

# Phase 2.0.2: 读取并使用记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 2.0.1（表 + repository + extractor）
> 交付物：MemoryRetriever + execute_job 注入 + memory_limit 配置（读取侧完整能力）
> 执行时机：Phase 2.0.1 之后、Phase 2.1 之前（一个 phase 只做一件事）
> 设计总览见 `agent-phase2-memory.md`

## 目标

记忆的**读取侧**：MemoryRetriever（最近 N 条 + FTS5 关键词检索 + 去重排序）、`_format_memory_context` 注入格式化、`execute_job` 注入历史上下文、`memory_limit` 用户配置（含前端表单）。

## MemoryRetriever

```python
# app/services/memory/retriever.py

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
        """检索历史记忆，按相关性+新鲜度排序。"""
        entries: list[MemoryEntry] = []

        # 1. 最近 N 次同任务执行
        entries.extend(self._repo.get_recent(task_type, task_id, limit=limit))

        # 2. 关键词搜索（FTS5，task_type 过滤）
        if keywords:
            clean = [k for k in keywords if k and k.strip()]  # 过滤空字符串
            if clean:
                entries.extend(self._repo.search_fts(
                    " ".join(clean), task_type=task_type, limit=limit
                ))

        # 3. 去重（按 run_id，防双路径命中）+ 排序 + 收束到 limit
        return self._deduplicate_and_rank(entries, limit)

    def _deduplicate_and_rank(
        self, entries: list[MemoryEntry], limit: int
    ) -> list[MemoryEntry]:
        """按 run_id 去重，recent 优先，收束到 limit 条。"""
        seen: set[str] = set()
        ranked: list[MemoryEntry] = []
        for e in entries:                      # recent 在前，天然优先
            if e.run_id in seen:
                continue
            seen.add(e.run_id)
            ranked.append(e)
            if len(ranked) >= limit:
                break
        return ranked
```

### 关键设计点（review 修复）

- **`search_fts` 带 task_type 过滤**：跨任务命中会污染上下文（summary 任务注入 sync/diagnostic 的记忆）
- **keywords 空字符串过滤**：`[job_config.user_filter or ""]` 在 user_filter 为空时产生 `""` 元素——`filter` 掉避免 FTS5 查空串
- **去重**：`_deduplicate_and_rank` 按 run_id，recent 路径优先（双路径命中时只注入一次）

## _format_memory_context 格式

```python
def _format_memory_context(entries: list[MemoryEntry]) -> str:
    """MemoryEntry 列表 → 注入文本。"""
    lines = []
    for e in entries:
        prefix = "[用户反馈]" if e.outcome == "feedback" else ""  # Phase 2.3 条目
        lines.append(f"- {prefix} {e.summary}".rstrip())
    return "\n".join(lines)
```

- 每条记忆一行：`- [用户反馈] 摘要内容`（反馈条目带前缀，Phase 2.3）
- 无 `[历史异常]` 前缀：失败不写记忆，异常模式识别是 Phase 3 日志分析 Agent 的独立功能

## execute_job 注入（修正顺序 + limit=0 短路）

```python
async def execute_job(self, job_config: SummaryJobConfig) -> None:
    task_id = f"summary-{job_config.name}"

    # === 1. 查询明细（先于注入：keywords 需要 records）===
    records = await self._query_records(job_config)

    # === 2. 注入记忆 ===
    memory_retriever = MemoryRetriever(self.memory_repo)
    if job_config.memory_limit > 0:                      # limit=0 短路，不调 retrieve
        # 关键词 = user_filter + 今日明细标题提取（规则提取，并集）
        keywords = [job_config.user_filter or ""]
        keywords += [r.bgm_title for r in records if r.bgm_title][:5]
        past_memories = memory_retriever.retrieve(
            task_type="summary",
            task_id=task_id,
            limit=job_config.memory_limit,   # 用户可配置
            keywords=keywords,
        )
        memory_context = self._format_memory_context(past_memories)
    else:
        memory_context = ""

    messages = self._build_messages(records, job_config.system_prompt)

    # === 3. 注入历史上下文到 system prompt ===
    if memory_context:
        messages.insert(0, Message(
            role="system",
            content=f"## 历史执行上下文\n{memory_context}"
        ))

    response = await self.llm_client.chat(messages)

    # === 4. 提取记忆（Phase 2.0.1；整体容错，失败不影响通知）===
    try:
        await memory_extractor.extract_and_store(
            task_type="summary",
            task_id=task_id,
            run_id=str(uuid4()),
            llm_response=response.content,
            records=records,
            decisions=[],
            outcome="success",
            tokens_used=response.usage.total_tokens if response.usage else 0,
        )
    except Exception:
        logger.exception("Failed to store memory")

    # === 5. 原有逻辑 ===
    await self._dispatch_notification(response)
```

### 关键设计点（review 修复）

- **顺序修正**：`_query_records` 提到 retrieve 之前（keywords 依赖 records，原示例有 NameError）
- **`memory_limit=0` 短路**：直接跳过 retrieve（get_recent(limit=0) 语义未定义，短路最清晰）
- **提取记忆 try/except 包裹**：`_summarize` 的 LLM 调用等失败不影响 `_dispatch_notification`
- **`tokens_used` 空值防护**：`response.usage` 可能为 None（LLM 重试耗尽返回空响应）——但此时 `response.content` 也为空，提取出的摘要为空字符串，可接受（空响应不写记忆，避免无效条目）

## memory_limit 配置

```ini
[summary-daily]
# ... 既有字段
memory_limit = 5        # 注入记忆条数，0 = 不注入记忆（默认 5）
```

- `app/services/summary/models.py`：`SummaryJobConfig` 增加 `memory_limit: int = 5`（from_config_dict 解析，非法值回落默认）
- `app/core/config.py`：`_SUMMARY_FIELDS` 增加 `"memory_limit"`
- **前端**：summary job 表单（`templates/config.html` 的 summary 卡片 + JS `renderSummaryJobs`/保存逻辑）加"记忆条数"输入框

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/services/memory/retriever.py` | MemoryRetriever（retrieve/_deduplicate_and_rank）+ `_format_memory_context`（或放 summary service） |
| 修改 | `app/services/summary/service.py` | `execute_job()` 注入记忆（顺序修正、limit=0 短路、提取容错） |
| 修改 | `app/services/summary/scheduler.py` | 传递 memory_repo（或共享实例） |
| 修改 | `app/services/summary/models.py` | `SummaryJobConfig` 增加 `memory_limit` |
| 修改 | `app/core/config.py` | `_SUMMARY_FIELDS` 增加 `memory_limit` |
| 修改 | `app/api/summary_jobs.py` | summary job CRUD 透传 memory_limit |
| 修改 | `templates/config.html` + `static/js/`（summary 相关） | 表单"记忆条数"输入框 |

> 总计：新增 1 个文件，修改 6 个文件

## BDD 测试场景

### Scenario R1 注入最近记忆
- **Given** 同 task 已有 2 条记忆，`memory_limit=5`
- **When** execute_job 注入
- **Then** system prompt 含 `## 历史执行上下文` 与 2 条摘要（每行 `- 摘要`）

### Scenario R2 关键词检索 + task_type 过滤
- **Given** 其他 task 的记忆含相同关键词
- **When** retrieve(keywords=[...])
- **Then** 只命中本 task 的记忆

### Scenario R3 双路径去重
- **Given** 某记忆既在最近 N 条又在关键词命中
- **When** retrieve
- **Then** 该记忆只注入一次（run_id 去重）

### Scenario R4 memory_limit=0 短路
- **Given** `memory_limit=0`
- **When** execute_job
- **Then** 不调用 retrieve，system prompt 无历史上下文

### Scenario R5 空关键词过滤
- **Given** keywords 含空字符串（user_filter 为空）
- **When** retrieve
- **Then** 不抛异常，FTS5 用非空关键词查询

### Scenario R6 配置透传
- **Given** `[summary-daily] memory_limit = 3`
- **When** 读取 SummaryJobConfig
- **Then** `memory_limit == 3`

### Scenario R7 提取记忆失败不影响通知（集成）
- **Given** extract_and_store 抛异常
- **When** execute_job
- **Then** `_dispatch_notification` 正常执行，异常仅记录日志

## 验证方式

1. 单元/集成测试：R1-R7 全部通过
2. 手动：触发 summary job 3 次（不同日期范围），检查第 3 次 LLM 请求日志 system prompt 含前 2 次摘要
3. 手动：设置 `memory_limit=0`，确认执行不注入历史上下文（退化验证）
4. 手动：FTS5 关键词（今日明细标题）检索到相关记忆

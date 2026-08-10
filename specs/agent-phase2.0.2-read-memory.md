# Phase 2.0.2: 读取并使用记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 2.0.1（表 + repository + extractor）
> 交付物：MemoryRetriever + execute_job 注入 + memory_limit 配置 + 窗口重叠去重（读取侧完整能力）
> 执行时机：Phase 2.0.1 之后、Phase 2.1 之前（一个 phase 只做一件事）
> 设计总览见 `agent-phase2-memory.md`

## 目标

记忆的**读取侧**：MemoryRetriever（最近 N 条 + FTS5 关键词检索 + 去重排序）、`_format_memory_context` 注入格式化、`execute_job` 注入历史上下文、`memory_limit` 用户配置（含前端表单）、**窗口重叠去重**（covered 比对 + overlap_note 标注，原 Phase 2.5 并入本 phase——读取侧过滤逻辑）。

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
    if job_config.memory_enabled:                        # 开关短路，不调 retrieve
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
- **`memory_enabled` 开关短路**：`false` 时直接跳过 retrieve（不注入任何记忆，含 feedback 条目）——用户显式关闭该任务的记忆功能，反馈强约束也随之失效；**memory_limit 只管条数（最小值 1），不用 0 表示关闭（单一关闭途径）**
- **提取记忆 try/except 包裹**：`_summarize` 的 LLM 调用等失败不影响 `_dispatch_notification`
- **`tokens_used` 空值防护**：`response.usage` 可能为 None（LLM 重试耗尽返回空响应）——但此时 `response.content` 也为空，提取出的摘要为空字符串，可接受（空响应不写记忆，避免无效条目）

## 窗口重叠去重（原 Phase 2.5 并入）

处理**窗口重叠**场景：summary 任务每日触发但 `lookback_days=7` 时，第 2 天执行的明细（2-8 天）与昨日总结覆盖范围（1-7 天）重叠——2-7 天记录被重复叙述。本 phase 实现精确识别与标注（读取侧过滤逻辑）。

### 重叠识别（注入前）

```python
async def find_overlaps(
    self,
    task_id: str,
    records: list[SyncRecord],
) -> list[SyncRecord]:
    """比对今日明细与最近一次记忆的 covered 列表，返回重叠记录。"""
    latest = await self._repo.get_latest(task_type="summary", task_id=task_id)
    if not latest or not latest.covered:
        return []
    # latest.covered 为 list[CoveredItem]（Pydantic，见 2.0.1）
    covered = {(c.title, c.season, c.episode) for c in latest.covered}
    return [
        r for r in records
        if (r.bgm_title, r.season, r.episode) in covered
    ]
```

### 标注注入（并入历史上下文，非独立 system prompt）

```python
# execute_job 中，注入历史上下文时（memory_context 构建后）：
overlaps = await retriever.find_overlaps(task_id, records)
if overlaps:
    overlap_note = (
        "以下记录已在上次总结中覆盖，可简述或跳过，不必重复展开：\n"
        + "\n".join(f"- {r.bgm_title} S{r.season}E{r.episode}" for r in overlaps[:20])
    )
    memory_context = f"{memory_context}\n{overlap_note}"   # 并入历史上下文部分
```

- **默认标注而非过滤**：不删除明细（避免改变"过去 7 天完整总结"的语义）；LLM 自主决定简述/跳过，用户可在 system_prompt 中要求"重复也详细总结"覆盖此行为
- **注入位置**：overlap_note 附加到 `## 历史执行上下文` 的 memory_context 内（与 2.0.2 的注入同一位置，不独立插入 system prompt）

## memory_enabled / memory_limit 配置（每任务独立，无继承）

```ini
[summary-daily]
# ... 既有字段
memory_enabled = false   # 记忆开关（默认 false，每任务显式声明，无全局继承）
memory_limit = 5         # 注入记忆条数（最小值 1，仅 enabled=true 时生效）
```

**为什么每任务独立（不用全局 + 覆盖）**：三态继承（未配置=继承全局）对 INI 配置是语义负担——用户无法直观判断"全局 true 时某任务未配置是开是关"。每任务显式声明，一眼可读；未来其他任务（feiniu 等）接入时在自己的配置段声明，语义同样清晰。

- `app/services/summary/models.py`：`SummaryJobConfig` 增加 `memory_enabled: bool = False`、`memory_limit: int = 5`（from_config_dict 解析，非法值回落默认）
- `app/core/config.py`：`_SUMMARY_FIELDS` 增加 `"memory_enabled"`、`"memory_limit"`
- **前端**：summary job 表单（`templates/config.html` 的 summary 卡片 + JS `renderSummaryJobs`/保存逻辑）加"记忆"开关（switch）+ "记忆条数"输入框——**开关关闭时条数输入框禁用**

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

### Scenario R4 memory_enabled=false 短路
- **Given** `memory_enabled=false`（默认值）
- **When** execute_job
- **Then** 不调用 retrieve，system prompt 无历史上下文（含 feedback 不注入）

### Scenario R8 memory_enabled=true 开启注入
- **Given** `memory_enabled=true`、`memory_limit=3`
- **When** execute_job
- **Then** 注入最近 3 条历史摘要

### Scenario R5 空关键词过滤
- **Given** keywords 含空字符串（user_filter 为空）
- **When** retrieve
- **Then** 不抛异常，FTS5 用非空关键词查询

### Scenario R6 配置透传
- **Given** `[summary-daily] memory_enabled = true`、`memory_limit = 3`
- **When** 读取 SummaryJobConfig
- **Then** `memory_enabled is True`、`memory_limit == 3`
- **And** 未配置时 `memory_enabled is False`（默认关闭）、`memory_limit == 5`

### Scenario R7 提取记忆失败不影响通知（集成）
- **Given** extract_and_store 抛异常
- **When** execute_job
- **Then** `_dispatch_notification` 正常执行，异常仅记录日志

### Scenario D1 无重叠不注入提示
- **Given** 最近记忆无 covered 列表（或今日明细无重叠）
- **When** 注入前检查
- **Then** system prompt 不含 overlap_note

### Scenario D2 重叠记录识别
- **Given** 最近记忆 covered 含 `{"title": "芙莉莲", "season": 1, "episode": 10}`，今日明细含同一条
- **When** `find_overlaps()`
- **Then** 返回该记录

### Scenario D3 标注注入（并入历史上下文）
- **Given** 有重叠记录
- **When** execute_job 注入
- **Then** memory_context 含"已在上次总结中覆盖，可简述或跳过"提示及重叠列表
- **And** overlap_note 位于 `## 历史执行上下文` 内（非独立 system prompt）

### Scenario D4 非重叠记录不受影响
- **Given** 明细含新记录（不在 covered）
- **When** 注入
- **Then** 新记录正常呈现，不被标注

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/services/memory/retriever.py` | MemoryRetriever（retrieve/_deduplicate_and_rank）+ `find_overlaps` + `_format_memory_context`（或放 summary service） |
| 修改 | `app/services/summary/service.py` | `execute_job()` 注入记忆（顺序修正、limit=0 短路、提取容错、overlap_note 并入） |
| 修改 | `app/services/summary/scheduler.py` | 传递 memory_repo（或共享实例） |
| 修改 | `app/services/summary/models.py` | `SummaryJobConfig` 增加 `memory_enabled`/`memory_limit` |
| 修改 | `app/core/config.py` | `_SUMMARY_FIELDS` 增加 `memory_enabled`/`memory_limit` |
| 修改 | `app/api/summary_jobs.py` | summary job CRUD 透传 memory_enabled/memory_limit |
| 修改 | `templates/config.html` + `static/js/`（summary 相关） | 表单"记忆"开关 + "记忆条数"输入框（开关关时禁用） |

> 总计：新增 1 个文件，修改 6 个文件

## 验证方式

1. 单元/集成测试：R1-R7 + D1-D4 全部通过
2. 手动：触发 summary job 3 次（不同日期范围），检查第 3 次 LLM 请求日志 system prompt 含前 2 次摘要
3. 手动：设置 `memory_enabled=false`（默认），确认执行不注入历史上下文（退化验证）
4. 手动：FTS5 关键词（今日明细标题）检索到相关记忆
5. 手动：每日触发 + lookback_days=7 连续执行 2 次，第 2 次 system prompt 的 `## 历史执行上下文` 内含 overlap_note 且仅列重叠记录
6. E2E（占位，参考 Phase 1 `tests/e2e/test_config_llm.py` 模式）：summary job 表单的"记忆"开关 + "记忆条数"输入框——渲染默认（开关关、条数禁用）、开启后保存携带 memory_enabled/memory_limit、回显

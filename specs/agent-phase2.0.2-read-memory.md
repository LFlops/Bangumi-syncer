# Phase 2.0.2: 读取并使用记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 2.0.1（表 + repository + extractor）
> 交付物：MemoryRetriever + execute_job 注入 + memory_limit 配置 + 窗口重叠去重（读取侧完整能力）
> 执行时机：Phase 2.0.1 之后、Phase 2.1 之前（一个 phase 只做一件事）
> 设计总览见 `agent-phase2-memory.md`

## 目标

记忆的**读取侧**：MemoryRetriever（最近 N 条 + FTS5 关键词检索 + 去重排序）、`_format_memory_context` 注入格式化、`execute_job` 注入历史上下文、`memory_limit` 用户配置（含前端表单）、**窗口重叠去重**（剧集消费标记比对 + overlap_note 标注，原 Phase 2.5 并入本 phase——读取侧过滤逻辑）。

## 前置重构（本 phase 内完成，execute_job 依赖）

当前 `SummaryService` 是单体 `generate_summary`（日期计算 + 查询 + 格式化 + 构建消息 + 调 LLM 全在一个方法里）+ 薄 `execute_job` + `_send_success_notification`/`_send_failure_notification`/`_format_records`。而本 phase 的 `execute_job` 需要拿到「查询结果 records」和「构建好的 messages」两个中间值来做记忆注入与重叠标注，因此先做一次**不改变对外行为**的拆解：

| 新成员 | 签名 | 来源（从 generate_summary 抽出） |
|---|---|---|
| `_query_records` | `(job_config) -> tuple[list[SummaryRecord], str, str]`（同步） | 日期范围计算 + `database_manager.get_records_in_date_range(...)` + dict→`SummaryRecord` 转换；返回值含 `(records, date_from, date_to)` |
| `_build_messages` | `(records, system_prompt) -> list[Message]`（同步） | `_format_records` 格式化 + 拼 system/user 两条消息 |
| `self.llm_client` | 实例属性 | `__init__` 中 `self.llm_client = get_llm_client()`（替代每次 `get_llm_client()`） |
| `_dispatch_notification` | `(job_config, response, records, date_from, date_to) -> None` | 现有 `execute_job` 的「空内容→失败通知 / 正常→成功通知」分支，内部复用 `_send_success_notification`/`_send_failure_notification` |

要点：
- **`_query_records`/`_build_messages` 是同步方法**（DB 查询与字符串拼接无 await；`execute_job`/`generate_summary` 保持 async，仅在 `chat` 处 await）。因此 2.0.2 `execute_job` 片段里 `records = await self._query_records(...)` 的 `await` 去掉，改为 `records, date_from, date_to = self._query_records(...)`。
- **日期范围共享**：`date_from/date_to` 原来在 `generate_summary` 内计算、既用于查询又塞进 result；拆解后由 `_query_records` 返回，`_dispatch_notification` 复用（避免两处重复计算日期）。
- **`generate_summary` 改为薄封装**（复用 `_query_records` + `_build_messages` + `self.llm_client`，继续返回 dict），供 `test_summary_job`（`app/api/summary_jobs.py`）使用——**test 端点签名与返回不变**（预览不含记忆，符合预期）。
- **`_dispatch_notification` 保持现有失败语义**：`chat` 返回空内容（`not response.content and not response.model`）→ `_send_failure_notification`（summary_llm_failed）；否则 `_send_success_notification`。记忆提取（步骤 4）在 try/except 内、`_dispatch_notification`（步骤 5）在 try 外，维持 2.0.2 失败语义表。
- `_format_records` 由 `r.get(...)` 改为 `r.xxx`（`SummaryRecord` 属性访问，见 D2）。
- `memory_retriever`/`memory_extractor` 作为 service 成员在 `__init__` 构造（与 `self.llm_client` 并列）：`self.memory_retriever = MemoryRetriever(database_manager.memory)`、`self.memory_extractor = MemoryExtractor(database_manager.memory)`；2.0.2 片段里 `memory_extractor` 裸名改为 `self.memory_extractor`。

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
                entries.extend(self._repo.search_fts(
                    " ".join(clean), task_type=task_type, limit=limit
                ))

        # 3. 去重（按 run_id，防双路径命中）；不按 limit 收束（keywords 额度独立）
        return self._deduplicate_and_rank(entries, limit)

    def _deduplicate_and_rank(
        self, entries: list[MemoryEntry], limit: int
    ) -> list[MemoryEntry]:
        """按 run_id 去重，保留顺序（recent 在前、keywords 命中随后）。

        recent 条数已在 get_recent(limit) 源头受限；keywords 命中不占额度
        全部保留，故此处无需收束（Phase 2.3 的 feedback 优先排序也在此扩展）。
        """
        seen: set[str] = set()
        ranked: list[MemoryEntry] = []
        for e in entries:
            if e.run_id in seen:
                continue
            seen.add(e.run_id)
            ranked.append(e)
        return ranked
```

> **额度语义定稿**：`memory_limit` 只约束 recent 路径（上下文连续性）；keywords 命中与（Phase 2.3 的）feedback 都不占额度——相关性/强约束高价值记忆不受注入量限制。

### 关键设计点（review 修复）

- **`search_fts` 带 task_type 过滤**：跨任务命中会污染上下文（summary 任务注入 sync/diagnostic 的记忆）
- **keywords 空字符串过滤**：`[job_config.user_filter or ""]` 在 user_filter 为空时产生 `""` 元素——`filter` 掉避免 FTS5 查空串
- **去重**：`_deduplicate_and_rank` 按 run_id，recent 路径优先（双路径命中时只注入一次）

## _format_memory_context 格式（归属：MemoryRetriever，记忆读取统一）

```python
def _format_memory_context(entries: list[MemoryEntry]) -> str:
    """MemoryEntry 列表 → 注入文本。"""
    return "\n".join(f"- {e.summary}" for e in entries)
```

- 每条记忆一行：`- 摘要内容`（Phase 2.3 引入 feedback 后，此处增加 `[用户反馈]` 前缀标记）
- 无 `[历史异常]` 前缀：失败不写记忆，异常模式识别是 Phase 3 日志分析 Agent 的独立功能

## execute_job 注入（修正顺序 + memory_enabled 短路）

```python
async def execute_job(self, job_config: SummaryJobConfig) -> None:
    task_id = f"summary-{job_config.name}"

    # === 1. 查询明细（先于注入：keywords 需要 records）===
    records, date_from, date_to = self._query_records(job_config)

    # === 2. 注入记忆 ===
    # repo 经 facade 公开属性 database_manager.memory 获取（见 2.0.1 文件清单中 __init__.py 的公开属性）
    memory_retriever = MemoryRetriever(database_manager.memory)
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

    # === 3. 注入历史上下文：拼进 system prompt（而非新增第二条 system message）===
    # 多 system message 对 OpenAI 兼容端点不安全（Anthropic 会 \n\n 合并、OpenAI 原样转发可能 400），
    # 故把 memory_context 拼进现有 system prompt 内容。
    system_prompt = job_config.system_prompt
    if memory_context:
        system_prompt = f"## 历史执行上下文\n{memory_context}\n\n{system_prompt}"
    messages = self._build_messages(records, system_prompt)

    response = await self.llm_client.chat(messages)

    # === 4. 提取记忆（Phase 2.0.1；读写同开关：memory_enabled=false 时不注入也不写入）===
    if job_config.memory_enabled:
        run_id = str(uuid4())          # 单次执行的唯一标识（记忆写入与消费标记共用）
        try:
            await self.memory_extractor.extract_and_store(
                task_type="summary",
                task_id=task_id,
                run_id=run_id,
                messages=messages,             # 完整上下文：缓存前缀 + 摘要来源
                response=response,             # 响应：summary 生成 + full_text 存储
                outcome="success",
                tokens_used=response.usage.total_tokens if response.usage else 0,
                record_ids=[r.id for r in records],   # store_and_mark 同一事务标记消费
            )
        except Exception:
            logger.exception("Failed to store memory")

    # === 5. 原有逻辑 ===
    self._dispatch_notification(job_config, response, records, date_from, date_to)
```

### 关键设计点（review 修复）

- **顺序修正**：`_query_records` 提到 retrieve 之前（keywords 依赖 records，原示例有 NameError）
- **`memory_enabled` 开关短路（读写一致）**：`false` 时跳过 retrieve **且跳过 extract_and_store**（不注入也不写入）——用户显式关闭该任务的记忆功能：不积累数据、不产生摘要 LLM 调用成本；feedback 强约束也随之失效；**memory_limit 只管条数（最小值 1），不用 0 表示关闭（单一关闭途径）**
- **提取记忆 try/except 包裹**：`_summarize` 的 LLM 调用等失败不影响 `_dispatch_notification`
- **`tokens_used` 空值防护**：`response.usage` 可能为 None（LLM 重试耗尽返回空响应）——但此时 `response.content` 也为空，提取出的摘要为空字符串，可接受（空响应不写记忆，避免无效条目）
- **历史上下文拼进 system prompt（不新增第二条 system）**：多 system message 对 OpenAI 兼容端点不安全（Anthropic 在 provider 内 `\n\n` 合并、OpenAI 原样转发多 system 可能 400）；故在 `execute_job` 里把 `memory_context` 拼进现有 system prompt 内容，而非 `messages.insert(0, Message(role="system", ...))`。与 Phase 2.2 无关（改在 summary service，不碰 `openai_compat.py`）

## 窗口重叠去重（原 Phase 2.5 并入）

处理**窗口重叠**场景：summary 任务每日触发但 `lookback_days=7` 时，第 2 天执行的明细（2-8 天）与昨日总结覆盖范围（1-7 天）重叠——2-7 天记录被重复叙述。本 phase 实现精确识别与标注（读取侧过滤逻辑）。

### 重叠识别（注入前，基于剧集消费标记；归属：MemoryRetriever）

```python
async def find_overlaps(
    self,
    records: list[SummaryRecord],
) -> list[SummaryRecord]:
    """返回今日明细中已被消费的记录（consumed_run_id IS NOT NULL）。

    数据基础：2.0.1 的 store_and_mark 在每次总结成功后标记 sync_records。
    精确到集、无窗口近似——无论多早被消费都能命中（covered 方案的
    "最近 K 条并集"对超过窗口的旧集会漏标）。
    """
    return [
        r for r in records
        if r.consumed_run_id is not None       # SummaryRecord.consumed_run_id 字段
    ]
```

- **无窗口概念**：判断"该集是否已被消费"直接查记录自身的标记——任意久远都精确
- **可追溯**：`r.consumed_run_id` → 可查该次总结摘要（overlap_note 引用）
- **失败自洽**：总结失败不标记 → 下次重新总结；重复观看 → 标记更新为最新 run_id
- **无需记忆表改动**：消费标记在 sync_records（记录侧），记忆表专注执行摘要

### 标注注入（并入历史上下文，非独立 system prompt）

```python
# execute_job 中，注入历史上下文时（memory_context 构建后）：
overlaps = await retriever.find_overlaps(records)
if overlaps:
    overlap_note = (
        "以下记录已在上次总结中覆盖，可简述或跳过，不必重复展开：\n"
        + "\n".join(
            f"- {r.bgm_title} S{r.season}E{r.episode}（已消费于总结 {r.consumed_run_id[:8]}）"
            for r in overlaps[:20]
        )
    )
    memory_context = f"{memory_context}\n{overlap_note}"   # 并入历史上下文部分
```

- **默认标注而非过滤**：不删除明细（避免改变"过去 7 天完整总结"的语义）；LLM 自主决定简述/跳过，用户可在 system_prompt 中要求"重复也详细总结"覆盖此行为
- **注入位置与顺序**：overlap_note 附加到 `memory_context` 内（与历史上下文同一位置，不独立插入 system prompt）；**必须在步骤 3 的 `_build_messages` 之前**追加（因为 system_prompt 已含 memory_context，之后无法再改）

### 消费标记失败语义（判定规则定稿）

消费标记的写入时机：execute_job 第 4 步（chat 成功后，`store_and_mark` 同一事务内写记忆 + 标记消费，同一 try 块）。各失败点的语义：

| 失败点 | 消费标记 | 通知 | 下次行为 |
|---|---|---|---|
| **chat 失败**（LLM 调用异常） | 不写 | 不发 | **重新总结**（未消费） |
| **store_and_mark 失败**（DB 异常，同 try 块） | 不写（记忆+标记原子回滚） | **照发**（try 外） | **重新总结**（可能重复通知，概率低可接受） |
| **通知失败**（投递层，try 外） | **已写** | 失败 | **不重新总结**（已消费，inbox 失败通知含内容可查） |

**判定规则**：消费成功 = **chat 成功 + store_and_mark 成功**（第 4 步完成）。通知是投递层，独立于消费——通知失败**不回滚消费标记**（总结已生成；投递走通知重试/告警兜底；回滚方案否决——通知持续失败时每次执行重新生成浪费 token）。
**原子性**：记忆 INSERT 与消费标记 UPDATE 是 `store_and_mark` 的同一事务（见 2.0.1）——不存在"记忆已写但标记未写"的中间态，失败时两者一起回滚。
**memory_enabled=false 不标记消费**：一致性成立——关闭记忆 = 不注入 = 不需要重叠去重（find_overlaps 在注入 if 内，不会被调用）；关闭期间总结过的记录重新开启后视为未消费（可接受，关闭期间不追踪消费状态）。

## memory_enabled / memory_limit 配置（每任务独立，无继承）

```ini
[summary-daily]
# ... 既有字段
memory_enabled = false   # 记忆开关（默认 false，每任务显式声明，无全局继承）
memory_limit = 5         # 注入记忆条数（最小值 1，仅 enabled=true 时生效）
```

**为什么每任务独立（不用全局 + 覆盖）**：三态继承（未配置=继承全局）对 INI 配置是语义负担——用户无法直观判断"全局 true 时某任务未配置是开是关"。每任务显式声明，一眼可读；未来其他任务（feiniu 等）接入时在自己的配置段声明，语义同样清晰。

`SummaryRecord`（`app/services/summary/models.py`，summary 链路内部观影记录载体，`_query_records` 返回类型；与 `SummaryJobConfig` 同为 `@dataclass`）：

```python
@dataclass
class SummaryRecord:
    id: int
    timestamp: str
    user_name: str
    title: str
    bgm_title: str
    season: int
    episode: int
    media_type: str
    source: str
    status: str
    consumed_run_id: str | None = None   # 消费标记（NULL=未消费；D12 补 SELECT 后填充）
```

> 为什么 `@dataclass` 而非 `BaseModel`：summary 链路内部载体，数据来自自有 SQL 查询（可信）、不跨 I/O 边界、不需校验/序列化；与 `SummaryJobConfig`/`MemoryEntry` 风格一致。共享方法 `get_records_in_date_range` 保持返回 `list[dict]` 不变，由 `_query_records` 内部做一行 dict→`SummaryRecord` 转换（避免波及 14 处测试断言）。

- `app/services/summary/models.py`：`SummaryJobConfig` 增加 `memory_enabled: bool = False`、`memory_limit: int = 5`（from_config_dict 解析，非法值回落默认）；新增 `SummaryRecord` dataclass（见上）
- `app/models/summary.py`：`SummaryJobCreate`（`memory_enabled: bool = False`、`memory_limit: int = 5`）、`SummaryJobUpdate`（`memory_enabled: Optional[bool] = None`、`memory_limit: Optional[int] = None`）、`SummaryJobResponse`（`memory_enabled: bool`、`memory_limit: int`）；`from_config_dict` 解析 `memory_enabled` 用与 `enabled` 相同的布尔容错、`memory_limit` 用 `_int(..., 5)` 回落
- `app/core/config.py`：`_SUMMARY_FIELDS` 增加 `"memory_enabled"`、`"memory_limit"`
- **前端**：summary job 表单（`templates/config.html` 的 summary 卡片 + JS `renderSummaryJobs`/保存逻辑）加"记忆"开关（switch）+ "记忆条数"输入框——**开关关闭时条数输入框禁用**；表单标注"记忆按任务隔离，多用户场景建议每用户一个任务"

### 多用户说明

- `user_name` 为空（多用户 job）：记忆为**任务级混合**——注入含所有用户历史（与任务级混合总结一致，可接受）
- 多用户**隔离**需求：`user_name` 固定单用户或每用户一个 job（前端表单已标注）——不做按用户分组注入（YAGNI）

## BDD 测试场景

### Scenario R1 注入最近记忆
- **Given** `memory_enabled=true`、同 task 已有 2 条记忆、`memory_limit=5`
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

### Scenario R9 keywords 命中不占额度
- **Given** `memory_limit=5`、recent 5 条 + FTS 关键词命中 3 条（无重叠）
- **When** retrieve(keywords=[...])
- **Then** 注入 8 条（recent 5 条 + keywords 命中 3 条全部保留，未被 limit 收束丢弃）

### Scenario R4 memory_enabled=false 短路（读写一致）
- **Given** `memory_enabled=false`（默认值）
- **When** execute_job
- **Then** 不调用 retrieve，system prompt 无历史上下文（含 feedback 不注入）
- **And** 不调用 extract_and_store（记忆表无新写入，无摘要 LLM 调用）

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
- **Given** 今日明细全部 `consumed_run_id IS NULL`（未消费）
- **When** 注入前检查
- **Then** system prompt 不含 overlap_note

### Scenario D2 重叠记录识别
- **Given** 今日明细某条 `consumed_run_id` 非空（已被总结消费）
- **When** `find_overlaps()`
- **Then** 返回该记录

### Scenario D5 无窗口限制（任意久远消费命中）
- **Given** S1E10 在 30 次执行前被消费（consumed_run_id 保留，早于记忆窗口）
- **And** 今日明细含 S1E10（用户重看）
- **When** `find_overlaps()`
- **Then** 返回 S1E10（消费标记精确到集，无窗口近似）

### Scenario D3 标注注入（并入历史上下文）
- **Given** 有重叠记录
- **When** execute_job 注入
- **Then** memory_context 含"已在上次总结中覆盖，可简述或跳过"提示及重叠列表
- **And** overlap_note 位于 `## 历史执行上下文` 内（非独立 system prompt）

### Scenario D4 非重叠记录不受影响
- **Given** 明细含新记录（consumed_run_id IS NULL）
- **When** 注入
- **Then** 新记录正常呈现，不被标注

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/services/memory/retriever.py` | MemoryRetriever（retrieve/_deduplicate_and_rank/find_overlaps）+ `_format_memory_context`（记忆读取统一放 retriever） |
| 修改 | `app/models/summary.py` | `SummaryJobCreate`/`SummaryJobUpdate`/`SummaryJobResponse` 增加 `memory_enabled`/`memory_limit` 字段（API CRUD 透传用） |
| 修改 | `app/services/summary/service.py` | 前置重构（`_query_records`/`_build_messages`/`self.llm_client`/`_dispatch_notification`）+ `execute_job()` 注入记忆（顺序修正、memory_enabled 短路、提取容错、overlap_note 并入） |
| 修改 | `app/services/summary/models.py` | `SummaryJobConfig` 增加 `memory_enabled`/`memory_limit`；新增 `SummaryRecord` dataclass |
| 修改 | `app/core/config.py` | `_SUMMARY_FIELDS` 增加 `memory_enabled`/`memory_limit` |
| 修改 | `app/api/summary_jobs.py` | summary job CRUD 透传 memory_enabled/memory_limit |
| 修改 | `templates/config.html` + `static/js/`（summary 相关） | 表单"记忆"开关 + "记忆条数"输入框（开关关时禁用） |

> 总计：新增 1 个文件，修改 6 个文件（`scheduler.py` 无需改动：service 经 `database_manager.memory` 直接访问，不走 scheduler 传递）

## 验证方式

1. 单元/集成测试：R1-R7 + D1-D4 全部通过
2. 手动：触发 summary job 3 次（不同日期范围），检查第 3 次 LLM 请求日志 system prompt 含前 2 次摘要
3. 手动：设置 `memory_enabled=false`（默认），确认执行不注入历史上下文（退化验证）
4. 手动：FTS5 关键词（今日明细标题）检索到相关记忆
5. 手动：每日触发 + lookback_days=7 连续执行 2 次，第 2 次 system prompt 的 `## 历史执行上下文` 内含 overlap_note 且仅列重叠记录
6. E2E（占位，参考 Phase 1 `tests/e2e/test_config_llm.py` 模式）：summary job 表单的"记忆"开关 + "记忆条数"输入框——渲染默认（开关关、条数禁用）、开启后保存携带 memory_enabled/memory_limit、回显

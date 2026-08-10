# Phase 2: 定时任务长期记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 1（ContentBlock 类型）
> 交付物：定时任务第 N 次执行可引用前 N-1 次结果

## 目标

让定时任务（首先是 AI 追番总结 scheduler，后续扩展到其他 scheduler）的第 N 次执行能够引用前 N-1 次执行的结果，打破"每次都从头开始"的限制。

## 核心场景

`summary_scheduler` 每天早上 9 点生成"昨日追番总结"。现在第 10 次执行时，prompt 中只包含"昨天的同步记录"。有了记忆后，prompt 中还会包含：

- 前 9 次总结的摘要（知道之前说过什么）——**Phase 2 直接支持**
- 用户反馈/偏好（用户上次说"不要太啰嗦"）——**依赖 Phase 2.3**（反馈通道：API + inbox 入口），Phase 2 只提供存储与注入能力
- 历史异常模式（"上周这个时候服务器挂了，数据不完整"）——**依赖 Phase 2.4**（失败路径记录：execute_job 失败时写入记忆），LLM 注入时自行识别模式，无需专门检测

## 设计

```
每次定时任务执行前：
  1. MemoryRetriever.retrieve(task_id, limit=5)
     → 从 agent_working_memory 查最近的执行摘要
     → 用 FTS5 搜索与当前任务关键词相关的内容
  2. 注入到 LLM prompt 的 "历史上下文" 部分

每次定时任务执行后：
  1. MemoryExtractor.extract(result)
     → 让 LLM 用一句话总结本次执行的关键发现
     → 结构化写入 agent_working_memory
```

## 数据库

```sql
CREATE TABLE agent_working_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,        -- 'summary', 'sync', 'diagnostic', 等
    task_id TEXT NOT NULL,          -- scheduler 名称, 如 'summary-daily'
    run_id TEXT NOT NULL UNIQUE,    -- UUID
    summary TEXT NOT NULL,          -- LLM 生成的一行摘要
    key_findings TEXT,              -- JSON: [{"key": "...", "value": "..."}]
    decisions_taken TEXT,           -- JSON: ["decision1", "decision2"]
    outcome TEXT NOT NULL,          -- 'success', 'partial', 'failed'
    tokens_used INTEGER,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_memory_task ON agent_working_memory(task_type, task_id);
CREATE INDEX idx_memory_created ON agent_working_memory(created_at);

-- FTS5 全文检索
CREATE VIRTUAL TABLE agent_memory_fts USING fts5(
    task_type, summary, key_findings, outcome,
    content='agent_working_memory',
    content_rowid='id'
);
```

## MemoryRetriever / MemoryExtractor

```python
# app/services/memory/retriever.py

class MemoryRetriever:
    """检索与当前任务相关的历史记忆"""

    def __init__(self, repo: AgentMemoryRepository):
        self._repo = repo

    async def retrieve(
        self,
        task_type: str,
        task_id: str,
        limit: int = 5,
        keywords: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """检索历史记忆，按相关性+新鲜度排序"""
        entries = []

        # 1. 最近 N 次同任务执行
        recent = await self._repo.get_recent(task_type, task_id, limit=limit)
        entries.extend(recent)

        # 2. 关键词搜索 (FTS5)
        if keywords:
            kw_results = await self._repo.search_fts(
                " ".join(keywords), limit=limit
            )
            entries.extend(kw_results)

        # 3. 去重 + 排序
        return self._deduplicate_and_rank(entries, limit)

class MemoryExtractor:
    """从执行结果中提取关键信息写入记忆"""

    async def extract_and_store(
        self,
        task_type: str,
        task_id: str,
        run_id: str,
        llm_response: str,
        decisions: list[str],
        outcome: str,
        tokens_used: int,
        error_message: str | None = None,  # Phase 2.4 使用（失败路径）
    ) -> None:
        summary = await self._summarize(llm_response)
        await self._repo.insert(MemoryEntry(
            task_type=task_type,
            task_id=task_id,
            run_id=run_id,
            summary=summary,
            key_findings=self._extract_key_findings(llm_response),
            decisions_taken=json.dumps(decisions),
            outcome=outcome,
            tokens_used=tokens_used,
            error_message=error_message,
        ))
        # 清理旧记忆（每个 task 最多保留 100 条）
        await self._repo.prune(task_type, task_id, keep=100)
```

## 记忆策略权衡

权衡维度：**信息价值 / token 成本 / 实现复杂度**。

| 策略 | 信息价值 | token 成本 | 实现复杂度 | 何时值得 |
|---|---|---|---|---|
| **append-only + 数量上限**（当前） | 中——能引用历史，有噪音 | 中——注入最近 N 条 | **最低** | **现在（Phase 2）** |
| 滚动摘要（rolling summary） | 高——长期记忆不膨胀 | 低——一份长期摘要替代 N 条明细 | 中——需定期 LLM 压缩 | 记忆量大、注入成本高时 |
| 语义检索（embedding） | 高——按语义联想 | 低——只注入相关片段 | 高——需 embedding 模型 | 跨主题联想时（Phase 3 诊断） |
| 反馈优先加权 | 高——用户约束是强信号 | 低——反馈条目少 | 中——依赖反馈通道 | **Phase 2.3 之后** |

**Phase 2 选择 append-only 的理由**：记忆量小（每日 1 次执行），噪音与 token 浪费可忽略；实现最简，先验证"记忆价值假设"。不做滚动摘要（YAGNI）与语义检索（FTS5 关键词对 summary 场景够用）。

**为演进留的扩展点（现在设计，不做实现）**：
1. `MemoryEntry` 已有 `outcome/error_message/key_findings` 字段——失败记录、结构化发现天然可存，未来压缩/加权都能用
2. retriever 的 `_deduplicate_and_rank` 是策略集中点——未来加"反馈优先"、"旧记忆摘要化"只改这里
3. 反馈加权先用 `[用户反馈]` 前缀约定（Phase 2.3），确认价值后再升为结构化 priority 字段

**演进路径**：append-only（Phase 2）→ 前缀优先（Phase 2.3 后）→ 滚动摘要（记忆膨胀时）。

### append-only 与 `_summarize` 不矛盾（两个维度）

- **append-only = 记录条数维度**（存储策略）：N 次执行 = N 条独立记录，不跨记录压缩合并（rolling summary 才是其对立面）
- **`_summarize` = 单条记录字段维度**：一次执行的完整响应提炼一行存入该条记录的 `summary` 字段
- 存储层面仍是纯 append，摘要只是每条记录的字段提炼

### 为什么不全量存/全量读

- **存储不是瓶颈**（一年几 MB），**注入才是瓶颈**：全量读时 token 成本线性膨胀 + 上下文窗口有限 + 信息冗余（注入 10 条全文 ≈ 5000 token，超过总结本身）
- **摘要 = 注入粒度的最优解**：保留"发生了什么"语义，30 token/条可控
- **关键词搜索 = 全量读取的选择性替代**：只注入最近 N 条时，FTS5 从全部历史按需捞相关记录
- **存取分离**：存什么与读什么是两个决策；"摘要存 + 最近 N 读 + 关键词补"是 token 成本最优解
- 何时全量：记忆量极小（N≤3）时全量注入无妨（summary 字段本就精简）；回溯全文走 inbox 通知记录，无需记忆表存 full_text

### 注入条数暴露为用户配置（memory_limit）

- `[summary-{name}]` 新增 `memory_limit`（默认 5，**0 = 不注入记忆**），自部署用户可按模型上下文/token 预算/任务复杂度调整
- `SummaryJobConfig` 增加 `memory_limit` 字段；前端 summary job 表单加输入框
- 落地"Phase 2 在 [summary-{name}] 加可选字段"的预留

### 去重策略

| 重复类型 | 处理 |
|---|---|
| 同记忆条目双路径命中（recent + keywords） | **去重**（`_deduplicate_and_rank` 按 run_id） |
| 摘要间重复叙事（连续多次提同一番剧） | 靠 `memory_limit` 数量控制，不去重（append-only 固有噪音） |
| 明细 vs 摘要交叉（今日明细与历史摘要同番剧） | **不去重**：明细是事实清单、摘要是历史叙事，用途不同不互斥；若观察实际冗余，在 `_format_memory_context` 加规则（策略集中点） |
| **窗口重叠**（每日触发 + lookback_days=7：第 2 天明细 2-8 天与昨日总结 1-7 天重叠） | **Phase 2 软去重 + 数据基础，精确去重见 Phase 2.5**：注入历史摘要让 LLM 看到重复（行为不可控但可用）；Phase 2 的 `key_findings` 存结构化覆盖列表供 2.5 比对 |

### key_findings 存结构化覆盖信息（Phase 2.5 的数据基础）

`extract_and_store` 时，`key_findings` 除发现项外，存入本次总结覆盖的记录列表（规则从 records 提取，非 LLM）：

```json
// key_findings 示例
{
  "covered": [{"title": "葬送的芙莉莲", "season": 1, "episode": 10}, ...],
  "findings": [...]
}
```

Phase 2.5 用 `covered` 列表按 `(title, season, episode)` 精确比对今日明细，实现窗口重叠去重。

### 摘要生成与评测

- **prompt 归属**：摘要是系统内部操作，用**开发者内置模板**（写死在 MemoryExtractor），不暴露用户配置；预留 `memory_prompt` 可选字段（YAGNI）
- **评测分层**：单元测试（mock LLM → 断言 summary 字段写入）→ 开发期人工抽查 → Phase 3 LLM-as-judge（事实保真度）

### 关键词来源（查询词来自当前任务侧）

- **摘要无需"生成关键词"**：FTS5 是全文索引，查询词直接匹配摘要全文
- 查询词由调用方（summary service）提供：今日 sync records 提取番剧标题（`bgm_title/ori_title` 去重取前 N）+ user_filter 并集
- **生产者是规则（纯代码），不是 LLM**：`bgm_title` 是同步匹配后的规范化标题（结构化字段），一行代码提取，零 LLM 参与、零 function call
- 未来若需从自由文本提取（如 Phase 2.3 反馈"最近看的番都很好看"→ 偏好关键词）：走普通 `llm_client.chat()` 单次补全，**不是 function call**（function call 是 Phase 3 模型主动调工具的机制）
- 现有 `keywords=[job_config.user_filter]` 升级为并集

## 接入现有 SummaryScheduler

在 `app/services/summary/service.py` 的 `execute_job()` 中：

```python
async def execute_job(self, job_config: SummaryJobConfig) -> None:
    task_id = f"summary-{job_config.name}"

    # === 新增：注入记忆 ===
    memory_retriever = MemoryRetriever(self.memory_repo)
    # 关键词 = user_filter + 今日明细标题提取（规则提取，并集）
    keywords = [job_config.user_filter or ""]
    keywords += [r.bgm_title for r in records if r.bgm_title][:5]
    past_memories = await memory_retriever.retrieve(
        task_type="summary",
        task_id=task_id,
        limit=job_config.memory_limit,   # 用户可配置，0 = 不注入
        keywords=keywords,
    )
    memory_context = self._format_memory_context(past_memories)

    # === 原有逻辑 ===
    records = await self._query_records(job_config)
    messages = self._build_messages(records, job_config.system_prompt)

    # === 注入历史上下文到 system prompt ===
    if memory_context:
        messages.insert(0, Message(
            role="system",
            content=f"## 历史执行上下文\n{memory_context}"
        ))

    response = await self.llm_client.chat(messages)

    # === 新增：提取记忆 ===
    memory_extractor = MemoryExtractor(self.memory_repo)
    await memory_extractor.extract_and_store(
        task_type="summary",
        task_id=task_id,
        run_id=str(uuid4()),
        llm_response=response.content,
        decisions=[],   # summary 任务暂无决策
        outcome="success",
        tokens_used=response.usage.total_tokens,
    )

    # === 原有逻辑 ===
    await self._dispatch_notification(response)
```

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/services/memory/__init__.py` | 记忆模块包 |
| 新增 | `app/services/memory/retriever.py` | MemoryRetriever + MemoryExtractor（成功路径摘要） |
| 新增 | `app/core/database/agent_memory.py` | AgentMemoryRepository（CRUD + FTS5） |
| 修改 | `app/core/database/connection.py` | `__ensure_agent_memory()` migration |
| 修改 | `app/services/summary/service.py` | `execute_job()` 注入记忆、成功路径提取回写 |
| 修改 | `app/services/summary/scheduler.py` | 传递 memory_repo（或共享实例） |
| 修改 | `app/services/summary/models.py` | `SummaryJobConfig` 增加 `memory_limit`（默认 5） |
| 修改 | `app/core/config.py` | `_SUMMARY_FIELDS` 增加 `memory_limit` |

> 总计：新增 3 个文件，修改 5 个文件
> 用户反馈通道（API + inbox 入口）见 Phase 2.3（`agent-phase2.3-feedback.md`）；失败路径记忆见 Phase 2.4（`agent-phase2.4-failure-memory.md`），本 phase 仅正常路径

## 验证方式

1. 手动触发 summary job 3 次（每次使用不同日期范围的测试数据）
2. 检查 `agent_working_memory` 表，确认 3 条记忆记录
3. 检查第 3 次执行的 LLM 请求日志，确认 system prompt 中包含前 2 次执行的摘要
4. 用 FTS5 搜索关键词（今日明细标题），确认能检索到相关记忆
5. 设置 `memory_limit=0`，确认执行时**不注入**历史上下文（退化验证）

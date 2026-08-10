# Phase 2 设计总览：定时任务长期记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 1（ContentBlock 类型）
> 交付物：定时任务第 N 次执行可引用前 N-1 次结果
> **本文档是设计总览**（决策与数据模型）；实施拆分为两份：
> - **Phase 2.0.1 写入记忆**（`agent-phase2.0.1-write-memory.md`）：表 + repository + MemoryExtractor
> - **Phase 2.0.2 读取并使用记忆**（`agent-phase2.0.2-read-memory.md`）：MemoryRetriever + 注入 + memory_limit 配置

## 目标

让定时任务（首先是 AI 追番总结 scheduler，后续扩展到其他 scheduler）的第 N 次执行能够引用前 N-1 次执行的结果，打破"每次都从头开始"的限制。

## 核心场景

`summary_scheduler` 每天早上 9 点生成"昨日追番总结"。现在第 10 次执行时，prompt 中只包含"昨天的同步记录"。有了记忆后，prompt 中还会包含：

- 前 9 次总结的摘要（知道之前说过什么）——**Phase 2.0.1 + 2.0.2 直接支持**
- 用户反馈/偏好（用户上次说"不要太啰嗦"）——**依赖 Phase 2.3**（反馈通道：API + inbox 入口），Phase 2.0.1 提供存储、2.0.2 提供注入能力
- 历史异常模式（"上周这个时候服务器挂了，数据不完整"）——**独立功能，依赖 Phase 3 日志分析 Agent**（日志页"AI 分析"按钮 → Agent 工具链诊断 → knowledge_base 沉淀），**不混入 summary 注入**（避免追番总结掺杂运维信息，功能内聚）

## 设计概览

```
每次定时任务执行前（2.0.2）：
  1. MemoryRetriever.retrieve(task_id, limit=memory_limit)
     → 从 agent_working_memory 查最近的执行摘要
     → 用 FTS5 搜索与当前任务关键词相关的内容
  2. 注入到 LLM prompt 的 "历史上下文" 部分

每次定时任务执行后（2.0.1）：
  1. MemoryExtractor.extract_and_store(result, records)
     → 让 LLM 用一句话总结本次执行的关键发现（或规则兜底）
     → 结构化写入 agent_working_memory（含 covered 覆盖列表）
```

## 数据库（2.0.1 建表）

```sql
CREATE TABLE agent_working_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,        -- 'summary', 'sync', 'diagnostic', 等
    task_id TEXT NOT NULL,          -- scheduler 名称, 如 'summary-daily'
    run_id TEXT NOT NULL UNIQUE,    -- UUID
    summary TEXT NOT NULL,          -- 一行摘要（成功=LLM 生成，失败=规则）
    key_findings TEXT,              -- JSON: {"covered": [...], "findings": [...]}
    decisions_taken TEXT,           -- JSON: ["decision1", "decision2"]
    outcome TEXT NOT NULL,          -- 'success', 'partial', 'failed', 'feedback'
    tokens_used INTEGER,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_memory_task ON agent_working_memory(task_type, task_id);
CREATE INDEX idx_memory_created ON agent_working_memory(created_at);

-- FTS5 全文检索（external content 表）
CREATE VIRTUAL TABLE agent_memory_fts USING fts5(
    task_type, summary, key_findings, outcome,
    content='agent_working_memory',
    content_rowid='id'
);

-- 同步触发器（必须：external content 表不自动同步）
CREATE TRIGGER agent_memory_ai AFTER INSERT ON agent_working_memory BEGIN
    INSERT INTO agent_memory_fts(rowid, task_type, summary, key_findings, outcome)
    VALUES (new.id, new.task_type, new.summary, new.key_findings, new.outcome);
END;
CREATE TRIGGER agent_memory_ad AFTER DELETE ON agent_working_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, task_type, summary, key_findings, outcome)
    VALUES ('delete', old.id, old.task_type, old.summary, old.key_findings, old.outcome);
END;
CREATE TRIGGER agent_memory_au AFTER UPDATE ON agent_working_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, task_type, summary, key_findings, outcome)
    VALUES ('delete', old.id, old.task_type, old.summary, old.key_findings, old.outcome);
    INSERT INTO agent_memory_fts(rowid, task_type, summary, key_findings, outcome)
    VALUES (new.id, new.task_type, new.summary, new.key_findings, new.outcome);
END;
```

## 记忆策略权衡

权衡维度：**信息价值 / token 成本 / 实现复杂度**。

| 策略 | 信息价值 | token 成本 | 实现复杂度 | 何时值得 |
|---|---|---|---|---|
| **append-only + 数量上限**（当前） | 中——能引用历史，有噪音 | 中——注入最近 N 条 | **最低** | **现在（Phase 2.0.1/2.0.2）** |
| 滚动摘要（rolling summary） | 高——长期记忆不膨胀 | 低——一份长期摘要替代 N 条明细 | 中——需定期 LLM 压缩 | 记忆量大、注入成本高时 |
| 语义检索（embedding） | 高——按语义联想 | 低——只注入相关片段 | 高——需 embedding 模型 | 跨主题联想时（Phase 3 诊断） |
| 反馈优先加权 | 高——用户约束是强信号 | 低——反馈条目少 | 中——依赖反馈通道 | **Phase 2.3 之后** |

**Phase 2.0.1/2.0.2 选择 append-only 的理由**：记忆量小（每日 1 次执行），噪音与 token 浪费可忽略；实现最简，先验证"记忆价值假设"。不做滚动摘要（YAGNI）与语义检索（FTS5 关键词对 summary 场景够用）。

**为演进留的扩展点（现在设计，不做实现）**：
1. `MemoryEntry` 已有 `outcome/error_message/key_findings` 字段——失败记录、结构化发现天然可存，未来压缩/加权都能用
2. retriever 的 `_deduplicate_and_rank` 是策略集中点——未来加"反馈优先"、"旧记忆摘要化"只改这里
3. 反馈加权先用 `[用户反馈]` 前缀约定（Phase 2.3），确认价值后再升为结构化 priority 字段

**演进路径**：append-only（2.0.1/2.0.2）→ 前缀优先（Phase 2.3 后）→ 滚动摘要（记忆膨胀时）。

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
- `SummaryJobConfig` 增加 `memory_limit` 字段；前端 summary job 表单加输入框（见 2.0.2）
- 落地"Phase 2 在 [summary-{name}] 加可选字段"的预留

### 去重策略

| 重复类型 | 处理 |
|---|---|
| 同记忆条目双路径命中（recent + keywords） | **去重**（`_deduplicate_and_rank` 按 run_id，2.0.2） |
| 摘要间重复叙事（连续多次提同一番剧） | 靠 `memory_limit` 数量控制，不去重（append-only 固有噪音） |
| 明细 vs 摘要交叉（今日明细与历史摘要同番剧） | **不去重**：明细是事实清单、摘要是历史叙事，用途不同不互斥；若观察实际冗余，在 `_format_memory_context` 加规则（策略集中点） |
| **窗口重叠**（每日触发 + lookback_days=7：第 2 天明细 2-8 天与昨日总结 1-7 天重叠） | **2.0.1 存 covered 数据基础，精确去重见 Phase 2.5**：2.0.2 注入历史摘要让 LLM 看到重复（软去重） |

> 注：失败路径不写记忆（无 outcome="failed" 条目）——异常模式识别是 Phase 3 日志分析 Agent 的独立功能，summary 链路保持内聚

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

- **prompt 归属**：摘要是系统内部操作，用**开发者内置模板**（写死在 MemoryExtractor，见 2.0.1），不暴露用户配置；预留 `memory_prompt` 可选字段（YAGNI）
- **评测分层**：单元测试（mock LLM → 断言 summary 字段写入）→ 开发期人工抽查 → Phase 3 LLM-as-judge（事实保真度）

### 关键词来源（查询词来自当前任务侧）

- **摘要无需"生成关键词"**：FTS5 是全文索引，查询词直接匹配摘要全文
- 查询词由调用方（summary service，2.0.2）提供：今日 sync records 提取番剧标题（`bgm_title/ori_title` 去重取前 N）+ user_filter 并集
- **生产者是规则（纯代码），不是 LLM**：`bgm_title` 是同步匹配后的规范化标题（结构化字段），一行代码提取，零 LLM 参与、零 function call
- 未来若需从自由文本提取（如 Phase 2.3 反馈"最近看的番都很好看"→ 偏好关键词）：走普通 `llm_client.chat()` 单次补全，**不是 function call**（function call 是 Phase 3 模型主动调工具的机制）
- 现有 `keywords=[job_config.user_filter]` 升级为并集

## 执行序列

Phase 1 → 1.1 → **2.0.1 → 2.0.2** → 2.1 → 2.2 → 2.3 → 2.5 → Phase 3

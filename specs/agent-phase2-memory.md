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
    summary TEXT NOT NULL,          -- 一行摘要（LLM 生成，失败兜底规则截断）
    covered TEXT,                   -- JSON: [{"title","season","episode"}]（Phase 2.5 比对用）
    outcome TEXT NOT NULL,          -- 'success', 'partial', 'feedback'
    tokens_used INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_memory_task ON agent_working_memory(task_type, task_id);
CREATE INDEX idx_memory_created ON agent_working_memory(created_at);

-- FTS5 全文检索（external content 表）
CREATE VIRTUAL TABLE agent_memory_fts USING fts5(
    task_type, summary, outcome,
    content='agent_working_memory',
    content_rowid='id'
);

-- 同步触发器（必须：external content 表不自动同步）
CREATE TRIGGER agent_memory_ai AFTER INSERT ON agent_working_memory BEGIN
    INSERT INTO agent_memory_fts(rowid, task_type, summary, outcome)
    VALUES (new.id, new.task_type, new.summary, new.outcome);
END;
CREATE TRIGGER agent_memory_ad AFTER DELETE ON agent_working_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, task_type, summary, outcome)
    VALUES ('delete', old.id, old.task_type, old.summary, old.outcome);
END;
CREATE TRIGGER agent_memory_au AFTER UPDATE ON agent_working_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, task_type, summary, outcome)
    VALUES ('delete', old.id, old.task_type, old.summary, old.outcome);
    INSERT INTO agent_memory_fts(rowid, task_type, summary, outcome)
    VALUES (new.id, new.task_type, new.summary, new.outcome);
END;
```

**归档表（prune 降级到冷存储，方案 B）**：

```sql
CREATE TABLE agent_working_memory_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,      -- 归档保留，追溯/去重仍可用
    summary TEXT NOT NULL,
    covered TEXT,
    outcome TEXT NOT NULL,
    tokens_used INTEGER,
    created_at TEXT,                  -- 保留原值（非重新默认）
    archived_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_memory_archive_task ON agent_working_memory_archive(task_type, task_id);
```

- **prune 语义**：从"删除"变为"**降级到冷存储**"——超出 keep 的旧记录先 `INSERT INTO archive` 再 `DELETE` 主表（触发器同步删 FTS 索引，归档表无 FTS）；**`outcome=feedback` 条目跳过 prune（长期保留，见下）**
- 主表（带 FTS）= 热记忆（recent/关键词注入）；归档表（无 FTS）= 冷记忆（Phase 3 失败定位等查全量历史走 `search_archive`，LIKE 检索）
- 存储量级：主表 1000 条 ≈ 200KB/任务；归档表每年 ≈ 2MB——均无压力

### 归档消费边界（summary 不接入归档 + FTS5 定位）

- **summary 任务不接入归档记忆**：其消费需求是"近期上下文"（注入 memory_limit 条 + FTS 捞近期相关），1000 条热窗口（≈2.7 年）已覆盖全部实际需求；3 年前的记忆对今日总结价值趋近于零
- **归档记忆的服务对象是 Phase 3 诊断**（追溯"很久以前的类似错误模式"）——追溯性需求与 summary 的近期性需求不同
- **FTS5 vs LIKE 的 balance 原则**：按"频率 × 精度"分配索引——热记忆高频毫秒级（execute_job 每日注入热路径）→ FTS5；归档低频秒级（诊断触发时）→ LIKE。FTS5 服务热路径，不因归档存在而失去意义
- **feedback 条目长期保留**：`outcome=feedback` 的用户偏好是长期有效强约束（"不要太啰嗦"3 年后依然生效），prune 跳过——执行摘要照常归档（近期价值）；注入时反馈优先（Phase 2.3：memory_limit 窗口内 feedback 优先，剩余给执行摘要）

**字段取舍**：
- `covered`：结构化覆盖列表（规则从 records 提取，零成本）——Phase 2.5 窗口重叠去重的比对数据（机器可精确比对）；`summary` 给 LLM 读（叙事，不可比对）、`outcome` 分类（状态枚举）——三者分工不同互不替代
- 不设 `decisions_taken`：无消费方（Phase 3 用 agent_steps 记录决策，重复）；不设 `error_message`：失败不写记忆（异常识别是 Phase 3 日志分析 Agent 的独立功能）

### 标识规范（task_id / run_id）

| 标识 | 产生方 | 产生时机 | 消费方 |
|---|---|---|---|
| `task_id` | **调度层**——`{task_type}-{name}` 约定，从 section 名派生（summary 任务在 execute_job 中 `f"summary-{job_config.name}"`） | 每次执行开始时 | 写入（extract_and_store 接收）、读取过滤（retrieve/get_recent/search_fts/prune 按 task 分组） |
| `run_id` | **调用方**——每次执行生成 `str(uuid4())`；MemoryExtractor 只接收不生成 | 每次执行开始时 | 写入（UNIQUE）、去重（_deduplicate_and_rank）、追溯 |

- task_id : run_id = **1 : N**（一个任务多次执行）
- 任何写记忆的调用方遵循同样约定：Phase 2.3 反馈生成自己的 run_id（outcome=feedback）、未来 Phase 3 agent 任务定义自己的 task_id
- **归档保留两者**：追溯/去重不因归档中断

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
1. `MemoryEntry` 已有 `outcome/covered` 字段——状态分类、结构化覆盖天然可存，未来压缩/加权都能用
2. retriever 的 `_deduplicate_and_rank` 是策略集中点——未来加"反馈优先"、"旧记忆摘要化"只改这里
3. 反馈加权先用 `[用户反馈]` 前缀约定（Phase 2.3），确认价值后再升为结构化 priority 字段

### FTS5 必要性（多任务/长期运行场景）与 prune 权衡

- **FTS5 不是当前规模的过度设计**：未来多任务（Phase 3 失败定位/诊断等 Agent 任务）都会写记忆，多任务 × 高频 × 长期运行 = 数万条。失败定位需快速检索"类似错误模式"——LIKE 全表扫描在数万条（~5MB 文本）退化到秒级，FTS5 倒排索引毫秒级
- **关键洞察：FTS5 解耦"保留量"与"检索性能"**——倒排索引查询与总量基本无关。因此 prune 只需考虑存储与历史深度，不需要担心性能
- **prune 默认 1000 条/任务**：每日任务 ≈ 2.7 年历史；存储 ~200KB/任务无压力；检索由 FTS5 保障
- **职责分离**：`memory_limit` 管注入量（默认 5），`prune` 管增长上限（默认 1000）——互不干扰

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

### covered 存结构化覆盖信息（Phase 2.5 的数据基础）

`extract_and_store` 时，`covered` 存入本次总结覆盖的记录列表（规则从 records 提取，非 LLM）：

```json
// covered 示例
[{"title": "葬送的芙莉莲", "season": 1, "episode": 10}, ...]
```

Phase 2.5 用 `covered` 列表按 `(title, season, episode)` 精确比对今日明细，实现窗口重叠去重。covered 的 season/episode 可为 None（电影），比对时 tuple 含 None 参与匹配即可。

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

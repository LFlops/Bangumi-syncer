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
  1. MemoryExtractor.extract_and_store(result, record_ids)
     → 让 LLM 用一句话总结本次执行的关键发现（或规则兜底）
     → store_and_mark 同一事务：结构化写入 agent_working_memory + 标记消费
     → prune 独立 best-effort 归档旧记忆
```

## 数据库（2.0.1 建表）

```sql
CREATE TABLE agent_working_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,        -- 'summary', 'sync', 'diagnostic', 等
    task_id TEXT NOT NULL,          -- scheduler 名称, 如 'summary-daily'
    run_id TEXT NOT NULL UNIQUE,    -- UUID
    summary TEXT NOT NULL,          -- 一行摘要（LLM 生成，失败兜底规则截断）
    full_text TEXT,                   -- 本次总结全文（回溯/诊断用；随 prune/归档同生命周期）
    outcome TEXT NOT NULL,          -- 'success', 'partial', 'feedback'
    tokens_used INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 剧集消费标记（sync_records 表新增列，migration ALTER TABLE）
-- consumed_run_id: 最近一次消费该集的总结 run_id（NULL = 未被消费）
-- 窗口重叠去重：查今日明细中 consumed_run_id IS NOT NULL 的记录，精确无窗口
```

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
    full_text TEXT,
    outcome TEXT NOT NULL,
    tokens_used INTEGER,
    created_at TEXT,                  -- 保留原值（非重新默认）
    archived_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_memory_archive_task ON agent_working_memory_archive(task_type, task_id);
```

- **prune 语义**：从"删除"变为"**降级到冷存储**"——超出 keep 的旧记录先 `INSERT INTO archive` 再 `DELETE` 主表（触发器同步删 FTS 索引，归档表无 FTS）；**`outcome=feedback` 条目跳过 prune（长期保留，见下）**
- 主表（带 FTS）= 热记忆（recent/关键词注入）；归档表（无 FTS）= 冷记忆（Phase 3 失败定位等查全量历史走 `search_archive`，LIKE 检索）
- 存储量级：每 (task_type, task_id) 主表 1000 条 ≈ 200KB/任务；归档表每年 ≈ 2MB——均无压力

### 归档消费边界（summary 不接入归档 + FTS5 定位）

- **summary 任务不接入归档记忆**：其消费需求是"近期上下文"（注入 memory_limit 条 + FTS 捞近期相关），1000 条热窗口（≈2.7 年）已覆盖全部实际需求；3 年前的记忆对今日总结价值趋近于零
- **归档记忆的服务对象是 Phase 3 诊断**（追溯"很久以前的类似错误模式"）——追溯性需求与 summary 的近期性需求不同
- **FTS5 vs LIKE 的 balance 原则**：按"频率 × 精度"分配索引——热记忆高频毫秒级（execute_job 每日注入热路径）→ FTS5；归档低频秒级（诊断触发时）→ LIKE。FTS5 服务热路径，不因归档存在而失去意义
- **feedback 条目长期保留**：`outcome=feedback` 的用户偏好是长期有效强约束（"不要太啰嗦"3 年后依然生效），prune 跳过——执行摘要照常归档（近期价值）；注入时反馈优先（Phase 2.3：memory_limit 窗口内 feedback 优先，剩余给执行摘要）

**字段取舍**：
- `summary` 给 LLM 读（叙事）、`outcome` 分类（状态枚举）、`tokens_used` 计费——职责单一
- 不设 `covered`：窗口重叠去重改用**剧集消费标记**（sync_records 的 consumed_run_id，见"剧集消费标记"小节）——精确到集、无窗口近似、不依赖记忆保留窗口
- 不设 `decisions_taken`：无消费方（Phase 3 用 agent_steps 记录决策，重复）；不设 `error_message`：失败不写记忆（异常识别是 Phase 3 日志分析 Agent 的独立功能）

### 标识规范（task_id / run_id）

| 标识 | 产生方 | 产生时机 | 消费方 |
|---|---|---|---|
| `task_id` | **调度层**——`{task_type}-{name}` 约定，从 section 名派生（summary 任务在 execute_job 中 `f"summary-{job_config.name}"`） | 每次执行开始时 | 写入（extract_and_store 接收）、读取过滤（retrieve/get_recent/search_fts/prune 按 task 分组） |
| `run_id` | **调用方**——每次执行生成 `str(uuid4())`；MemoryExtractor 只接收不生成 | 每次执行开始时 | 写入（UNIQUE）、去重（_deduplicate_and_rank）、追溯 |

- task_id : run_id = **1 : N**（一个任务多次执行）
- 任何写记忆的调用方遵循同样约定：Phase 2.3 反馈生成自己的 run_id（outcome=feedback）、未来 Phase 3 agent 任务定义自己的 task_id
- **归档保留两者**：追溯/去重不因归档中断

### 记忆清理机制（上下文被污染时如何重置；实施见 Phase 2.0.3）

| 方案 | 可恢复性 | 实现成本 | 责任方 |
|---|---|---|---|
| **改名迁移记忆**（rename_task） | ✓ 记忆跟随任务（改名不丢） | 低——同一事务 UPDATE 主表 + 归档表的 task_id | memory 模块 + 改名流程联动 |
| **重置：显式 `clear_task`** | ✗ 不可恢复（二次确认） | 低——同一事务删主表 + 归档 + 消费标记 | memory 模块（API 层暴露） |
| **重置：复制为新 job** | ✓✓ 旧 job 记忆保留可回滚 | 零——配置系统已支持 | 上游（用户/配置层） |
| B. 软删除标志位 | ✓ 可恢复 | 中——deleted 列 + 全查询过滤 + 清理策略 | memory 模块 |
| D. 业界其他 | 快照/版本化/自动遗忘/审计 | — | — |

- **改名 ≠ 重置（语义修正）**：用户只改显示名时记忆跟随（`rename_task` 迁移）——`summary-{name}` 的 task_id 改名时 UPDATE 记忆表的 task_id；**消费标记天然无需迁移**（consumed_run_id 只关联 run_id，不依赖 task_id）；改名流程联动：`save_summary_config(old_name)` + `rename_notification_type` 已有先例，增加 `rename_task` 调用
- **重置 = 显式操作**：`clear_task`（彻底清空，二次确认——同一事务删主表 + 归档表 + 该 task 相关消费标记，防悬挂引用）或**复制为新 job**（保留旧 job 记忆，新 job 从零开始，可回滚）
- **映射表方案否决**：task_id 从"由 name 派生"变"查映射表"（每个读写多一跳），为"改名不丢记忆"付出的代价远大于 rename_task 的一次性 UPDATE
- **B 否决**：deleted 标志与归档语义重叠、表膨胀、查询复杂度
- **职责划分**：改名迁移归 memory 模块（rename_task），配置层改名流程联动；清理能力归 memory 模块（clear_task）；复制新 job 归配置层——互补不冲突

### 多用户场景：task_id 是隔离单元，调用方决定粒度

- **记忆按 task_id 隔离**（一个任务的记忆只被该任务读写）；**无 user 维度**——隔离粒度由调用方定义 task_id 决定
- **summary 任务**：`user_name` 为空（多用户 job）= 记忆为**任务级混合**（注入含所有用户历史——可接受：总结本身是任务级产出，混合记忆同理）；多用户**隔离**需求 → 调用方约束：`user_name` 固定单用户或每用户一个 job；**不做**按用户分组注入/提取（复杂度高，YAGNI——真实需求出现时再加 user 维度）
- **消费标记**：sync_records 记录级天然带 `user_name` ✓ 无需改
- **feedback**：任务级偏好（多用户 job 下混合反馈注入所有用户，与混合总结一致）——用户标识走 summary 字段前缀约定（见 2.3），不新增列；可检索性：FTS5 全文命中 summary 字段，按用户 SQL 精确过滤（需列）是 YAGNI

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
1. `MemoryEntry` 已有 `outcome` 字段——状态分类天然可存，未来压缩/加权都能用
2. retriever 的 `_deduplicate_and_rank` 是策略集中点——未来加"反馈优先"、"旧记忆摘要化"只改这里
3. 反馈加权先用 `[用户反馈]` 前缀约定（Phase 2.3），确认价值后再升为结构化 priority 字段

### FTS5 必要性（多任务/长期运行场景）与 prune 权衡

- **FTS5 不是当前规模的过度设计**：未来多任务（Phase 3 失败定位/诊断等 Agent 任务）都会写记忆，多任务 × 高频 × 长期运行 = 数万条。失败定位需快速检索"类似错误模式"——LIKE 全表扫描在数万条（~5MB 文本）退化到秒级，FTS5 倒排索引毫秒级
- **关键洞察：FTS5 解耦"保留量"与"检索性能"**——倒排索引查询与总量基本无关。因此 prune 只需考虑存储与历史深度，不需要担心性能
- **prune 默认 1000 条/任务**：每日任务 ≈ 2.7 年历史；存储 ~200KB/任务无压力；检索由 FTS5 保障
- **职责分离**：`memory_limit` 管注入量（默认 5），`prune` 管增长上限（默认 1000）——互不干扰

### FTS5 与联合索引：互补而非替代

| 索引 | 解决的问题 | 记忆检索路径 |
|---|---|---|
| **联合索引**（idx_memory_task：task_type+task_id+created_at） | **结构化查询**（按任务+时间过滤/排序） | `get_recent`——短期记忆注入路径（memory_limit 条） |
| **FTS5**（倒排索引） | **全文检索**（按文本内容关键词匹配） | `search_fts`——中期记忆关键词检索（今日明细标题） |
| LIKE（归档表） | 无索引（冷层低频） | `search_archive`——Phase 3 诊断全量历史 |

- 不是二选一：`LIKE %kw%` 无法用联合索引（全表扫描），**内容检索必须 FTS5**；**结构化过滤必须联合索引**（get_recent 无法用 FTS5 表达"按 task + 时间排序取 N 条"）
- **短期记忆 = 联合索引路径**（get_recent）；FTS5 是**中期记忆的检索手段**；归档 LIKE（冷层）

### 短中长期记忆分层

| 层 | 定义 | 当前实现 | 生命周期 |
|---|---|---|---|
| **短期**（工作记忆） | 当前执行的注入上下文 + 任务状态 | `memory_limit` 条注入（get_recent 联合索引路径）；AgentRun 上下文（Phase 3 内存态） | 单次执行/数天 |
| **中期**（情景记忆） | 近期任务执行的摘要历史 | 热记忆主表 + FTS5 检索（prune 1000 ≈ 2.7 年） | 数周到数年 |
| **长期**（语义/知识） | 跨任务的持久知识 | feedback（永久，不被 prune）+ 归档表 + knowledge_base（Phase 3 沉淀） | 永久 |

**缺口标注**：三层无显式"记忆类别"标记——当前靠 task_type（任务维度）、outcome（feedback 标记）、独立表（knowledge_base）**隐含**分层。改进方向：knowledge_base 作为长期语义记忆的显式载体（Phase 3）；如需跨层统一检索再评估显式类别字段（YAGNI）。

### 纯文本 vs RAG：gap 是词汇鸿沟

- **FTS5 是词法匹配**——查询词与文档用词一致才命中。**词汇鸿沟（vocabulary gap）**：语义相同措辞不同则命中不了（"服务器挂了" vs "503 错误"；中文/日文/别名标题）
- **RAG（embedding 向量检索）**把文本映射到语义空间按距离召回——跨越词汇鸿沟
- **适合 RAG 的项目特征**：语义需求强（查询措辞 ≠ 文档措辞）、大规模非结构化文本（数万条+）、跨语言/同义词、有 embedding 能力、召回率优先（宁多召回让 LLM 过滤）
- **当前判断**：summary 记忆检索（今日明细标题精确匹配、规模小）词汇鸿沟小，不需要 RAG；Phase 3 的可 RAG 候选（2GB 档案标题匹配、knowledge_base 错误模式）见 `agent-phase3-agent.md` 的"RAG 演进"小节

**演进路径**：append-only（2.0.1/2.0.2）→ 前缀优先（Phase 2.3 后）→ 滚动摘要（记忆膨胀时）。

### append-only 与 `_summarize` 不矛盾（两个维度）

- **append-only = 记录条数维度**（存储策略）：N 次执行 = N 条独立记录，不跨记录压缩合并（rolling summary 才是其对立面）
- **`_summarize` = 单条记录字段维度**：一次执行的完整响应提炼一行存入该条记录的 `summary` 字段
- 存储层面仍是纯 append，摘要只是每条记录的字段提炼

### 为什么不全量存/全量读

- **存储不是瓶颈**（一年几 MB），**注入才是瓶颈**：全量读时 token 成本线性膨胀 + 上下文窗口有限 + 信息冗余（注入 10 条全文 ≈ 5000 token，超过总结本身）
- **摘要 = 注入粒度的最优解**：保留"发生了什么"语义，30 token/条可控
- **关键词搜索 = 全量读取的选择性替代**：只注入最近 N 条时，FTS5 从全部历史按需捞相关记录
- **存取分离（记忆表内两列）**：`summary` 存摘要（注入粒度最优，~30 token/条）；`full_text` 存本次总结全文（回溯/诊断用，Phase 3 看历史总结细节）——注：成功通知 `write_in_app=False` 不写站内信，全文项目内无 inbox 载体，故记忆表需 `full_text` 列
- **成本账（为什么存摘要而非全文）**：LLM 摘要 1 次/执行（~100 token，且可命中 prompt 缓存见 2.0.1）+ 注入 5×30 = **~250 token/次** vs 存全文注入 5×300 = **~1500 token/次**——摘要是一次性成本被注入次数摊薄，长期更省；全文存 full_text 列仅供回溯不注入
- 何时全量注入：记忆量极小（N≤3）时全文注入无妨（但 summary 字段本就精简，保持摘要注入）

### 记忆开关与注入条数（memory_enabled / memory_limit，每任务独立）

- `[summary-{name}]` 新增 **`memory_enabled`**（默认 `false`，保守——存量行为不变）与 **`memory_limit`**（默认 5，仅 enabled=true 时生效）
- **每任务独立配置，无全局继承**：三态继承（未配置=继承全局）对 INI 配置是语义负担（用户无法直观判断"全局 true 时某任务未配置是开是关"）；每任务显式声明，一眼可读；未来其他任务接入时在自己的配置段声明
- **单一关闭途径**：`memory_enabled=false` 即关闭（不注入任何记忆，含 feedback）；`memory_limit` 只管条数（最小值 1），不用 0 表示关闭
- `SummaryJobConfig` 增加两字段；前端 summary job 表单加开关 + 条数输入框（开关关时禁用，见 2.0.2）
- 落地"Phase 2 在 [summary-{name}] 加可选字段"的预留

### 去重策略

| 重复类型 | 处理 |
|---|---|
| 同记忆条目双路径命中（recent + keywords） | **去重**（`_deduplicate_and_rank` 按 run_id，2.0.2） |
| 摘要间重复叙事（连续多次提同一番剧） | 靠 `memory_limit` 数量控制，不去重（append-only 固有噪音） |
| 明细 vs 摘要交叉（今日明细与历史摘要同番剧） | **不去重**：明细是事实清单、摘要是历史叙事，用途不同不互斥；若观察实际冗余，在 `format_memory_context` 加规则（策略集中点） |
| **窗口重叠**（每日触发 + lookback_days=7：第 2 天明细 2-8 天与昨日总结 1-7 天重叠） | **剧集消费标记**（2.0.1 标记 sync_records.consumed_run_id，2.0.2 find_overlaps 查标记 + overlap_note 标注）——精确到集、无窗口近似 |

> 注：失败路径不写记忆（无 outcome="failed" 条目）——异常模式识别是 Phase 3 日志分析 Agent 的独立功能，summary 链路保持内聚

### 剧集消费标记（2.0.2 重叠去重的数据基础）

窗口重叠去重从**剧集侧**建模：记录被哪次总结消费过，而非"总结覆盖了哪些记录"。

- **数据位置**：`sync_records` 新增 `consumed_run_id`（最近一次消费该集的总结 run_id，NULL = 未消费）+ `consumed_at`
- **旧库迁移（幂等）**：`__ensure_sync_records_consumed`——启动时 `PRAGMA table_info` 检查列是否存在，缺失则 `ALTER TABLE ADD COLUMN`（项目既有 `__ensure_*` 模式，老用户升级自动补列）
- **为什么加列而非新辅助表**：当前需求是单值标记（最近一次消费），加列无 join、生命周期一致（sync_records 清理时标记随之消失，无孤儿行）；辅助表的优势（消费历史/跨表复用）是 YAGNI
- **写入（2.0.1）**：execute_job 成功路径，`store_and_mark(entry, record_ids)` 同一事务内更新今日明细的消费标记（记忆 INSERT + 标记消费原子，见 2.0.1；`prune` 独立 best-effort）
- **读取（2.0.2）**：`find_overlaps` 查"今日明细中 `consumed_run_id IS NOT NULL`"——**精确到集，无窗口近似**（covered 方案的"最近 K 条并集"对超过窗口的旧集会漏标，消费标记无此问题）
- **可追溯**：标记含 run_id → 直接取该次总结摘要（overlap_note 可带摘要内容）
- **失败自洽**：总结失败不标记 → 下次重新总结；重复观看：标记更新为最新 run_id
- **保留窗口**：随 sync_records 保留策略（记录是核心数据，通常长于记忆窗口）

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

Phase 1 → 1.1 → **2.0.1 → 2.0.2 → 2.0.3** → 2.1 → 2.2 → 2.3 → Phase 3

> 顺序说明：2.3（反馈）与 Phase 3 无依赖，排在 2.2 后是人为顺序——业务价值上 Phase 3 > 2.3，可按需调整（如 Phase 3 提前）。

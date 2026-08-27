# Phase 3 实施 spec：LLM 匹配增强（Match Assist）

> 所属计划：Bangumi-Syncer Agent 化增量计划
> 性质：Phase 3 实施 spec（重新定义：首个落地场景 = 番剧名匹配，非总结增强）
> 状态：**待评审**——评审通过后按 §9 子 phase 拆分实施
> 关联：
> - `agent-phase3-4-decisions.md`（原 P3 总结增强 → 本文档替换；该文档的 J3/J4/C 系列接缝结论仍有效）
> - `agent-phase2.1-tools.md`（工具协议 spec——本文档 §3.2 补齐其未落地的代码）
> - `agent-phase3-agent.md`（Phase 4 参考资料：本文档实现的 registry/工具协议是 Phase 4 Agent 循环的接缝）
> - `agent-phase3-assistant.md`（ADR：自建循环不用 LangGraph——继续有效）

---

## 0. 背景与目标

### 0.1 问题调研（GitHub Issues/PR 分析）

对 upstream `SanaeMio/Bangumi-syncer` 61 个 issue 调研，匹配失败集中在两类，纯规则瀑布难以覆盖：

| 类 | 失败模式 | 案例 |
|---|---|---|
| A1 | 跨季/续集链错配 | #236 Re0 四季度→第一季；#182 无职转生 S3→S01；#57 灰色果实 S02→S01 |
| A2 | 季信息丢失/集数超界 | #97 妄想学生会；#86 凉宫 2009 重制版；#23 我推的孩子 ep 偏移 |
| B1 | 别名/译名跨语言鸿沟 | #128 花开伊吕波剧场版（日文名未用上）；#46 物语系列猫物语(白)→终物语 |

LLM 介入价值集中在 **A1/A2（跨季判断）与 B1（多语言语义）**——恰是规则瀑布最难覆盖、用户反馈最多的场景。

### 0.2 目标

规则匹配全部失败后，由 LLM 单点介入评估候选/补充搜索，产出**待用户手动确认的建议**；用户确认后写入自定义映射并自动补发。Agent 是**建议者而非定案者**——永不自动放通。

### 0.3 对"workflow 越写越重"的回应

本次封装**通用能力原语**（工具协议 + 结构化输出解析），匹配仅作为首个消费方；编排顺序由 LLM 工具调用自主决定，不写死匹配专用 workflow。Phase 4 的通用 Agent 循环直接消费本次组件。

---

## 1. 决策清单（已确认，评审时仅核对）

| 编号 | 决策 | 结论 |
|---|---|---|
| D1 | Agent 介入位置 | `_handle_match_failure`（orchestrator.py:326）挂接，**不进匹配管道**；Agent 永不直接命中，永远产出待确认建议 |
| D2 | Agent 输出形态 | **直选一个推荐** `{subject_id, reason}`；新 subject 追加进 `candidates_json`；现有多候选列表与手动确认保留 |
| D3 | 落库形态 | 新增 `match_llm_jobs` 独立表（LLM 执行层状态机）+ `pending_candidates` 加 `llm_subject_id`/`llm_reason` 两列（用户确认层）；**不用 JSON TEXT 存结构化数据** |
| D4 | 执行方式 | 异步：失败匹配立即落库返回，`llm_match_scheduler` 定时任务轮询处理（AsyncIOScheduler，可直接 await LLM，无同步桥接问题） |
| D5 | 调度参数 | 轮询 30~60s、LLM 失败重试上限 3 次（对齐 `LLMClient.MAX_RETRIES=2` 保守风格） |
| D6 | 状态联动 | `match_llm_jobs` 终态（applied/rejected）随 `pending_candidates` 确认/忽略 API 联动流转 |
| D7 | 开关/降级 | `[sync] llm_match_assist=false` 默认关；开关关 / LLM 配置缺失 / 调用失败 → 不落任务或标 failed，**原失败逻辑完全不变**；LLM 缺失时日志说明 |
| D8 | 前后端检查 | 后端校验 LLM 配置存在才允许开启；前端开关仅 LLM 已配置时显示（参照 dashboard 用量卡片条件渲染先例） |
| D9 | 通知 | 复用 `pending_candidate` 类型，**补站内信**（in_app_type + 标题模板），文案"已由 Agent 匹配，待确认"；不新增通知类型 |
| D10 | KV cache | 两轮间前缀复用（必做）+ 静态 system/tools 前缀跨调用缓存（推荐，可开关） |

---

## 2. 现状基线

### 2.1 匹配失败落点

```
_handle_match_failure（orchestrator.py:326-384）＝统一失败入口
  ├─ 写 sync_records(status=error)
  ├─ 发 anime_not_found 通知（站内信 type=sync_failed）
  └─ _sediment_pending_candidate（__init__.py:541-587）
       └─ if not candidates: return   ← 无候选时零沉淀（LLM 价值最大的场景反而空白）
```

### 2.2 候选机制

- `_collect_candidates_from_trace`（__init__.py:527-539）：收集 trace 全部 step 候选（bangumi-data top-5 + api_search top-5 + post_search 追加），去重按 score 降序
- 全部存 `pending_candidates.candidates_json`，前端列表全展示，用户手动挑一个确认
- 确认闭环：`confirm_pending_candidate`（__init__.py:214-268）→ 写映射 + `_auto_replay_after_confirm` 补发（__init__.py:270-347）

### 2.3 通知现状

- `pending_candidate`（registry:166-174）`in_app_type=None`——**不写站内信**，只走 webhook/email
- `anime_not_found`（registry:144-154）in_app_type=sync_failed

### 2.4 LLM 基础设施现状

- `LLMClient.chat` 为 **async**（client.py:102）；匹配链路 `sync_custom_item` 为**同步**（webhook 经 `_dispatch_media_server_webhook` 的 `async_fn` 后台提交，sync.py:844-899——主链路已异步化，有 `asyncio.to_thread` 先例）
- **Phase 2.1 工具协议未落地**：`llm/models.py` 只有 Text/Thinking/Redacted 三种 block；`anthropic.py:216` 对 `tool_use` 仅"跳过 + warning"
- 调度器先例：`bangumi_replay_scheduler.py`（BaseScheduler + AsyncIOScheduler + 队列空跳过）——`llm_match_scheduler` 同构
- KV cache 先例：`MemoryExtractor._summarize`（extractor.py:56-83）完整历史作前缀命中缓存

---

## 3. 架构设计

### 3.1 总体流程

```
媒体库播放完成 → webhook（async 提交，立即返回 accepted）
  → 同步链路：匹配管道（custom_mapping → bangumi_data → api_search）
  → 成功：bangumi_id_found + 打格子（现有逻辑不变）
  → 失败：_handle_match_failure
      ├─ sync_records(error) + anime_not_found（原逻辑不变）
      └─ 开关开 + LLM 可用 → 落 match_llm_jobs(pending) + trace 记 llm_assist step
                                    ↓
llm_match_scheduler（AsyncIOScheduler，30~60s 轮询）
  ├─ pending → processing
  ├─ 两轮 chat + 并行工具执行（评估候选 / 补充搜索）
  ├─ done + 有建议 → 写/更新 pending_candidates（llm_subject_id/llm_reason，无候选也落）
  │       → 发 pending_candidate 通知（站内信 + webhook，文案"已由 Agent 匹配，待确认"）
  ├─ done + 无建议 → 任务终态 no_suggestion（原失败路径不动）
  └─ LLM 失败 → 重试上限 3 次 → failed（日志 + 保留供排查）
                                    ↓
用户（候选确认页）
  ├─ 应用建议（bypass）→ 写映射 + _auto_replay_after_confirm 补发
  │       → bangumi_id_found（已匹配通知）；match_llm_jobs → applied
  ├─ 手动确认其他候选（现有逻辑保留）；match_llm_jobs → applied
  └─ 忽略 → rejected；match_llm_jobs → rejected
```

### 3.2 通用能力封装（本次实现，Phase 4 消费）

**3.2.1 工具协议补齐（Phase 2.1 未落地部分）**

| 变更 | 文件 | 内容 |
|---|---|---|
| 修改 | `app/services/llm/models.py` | union 追加 `ToolUseBlock(id, name, input)` / `ToolResultBlock(tool_use_id, content, is_error)`（按 phase2.1 spec T1-T7 场景） |
| 修改 | `app/services/llm/providers/anthropic.py` | tool_use/tool_result wire 1:1 转换；`stop_reason="tool_use"`；未知 block 跳过逻辑替换为正式解析 |
| 修改 | `app/services/llm/providers/openai_compat.py` | 内部→wire 拆并（assistant.tool_calls / role=tool 拆分）；wire→内部合并回 ToolResultBlock；arguments JSON 解析失败兜底 `{"raw": ...}` |

**3.2.2 工具注册表与执行器（新文件 `app/services/llm/tools.py`）**

- `ToolDefinition(name, description, parameters, handler, access: "read"/"write")`
- `ToolRegistry.register / execute(name, args)`：write 级工具执行前记录审计日志
- 本次注册的匹配工具（全部是现有服务方法的 adapter 包装，不写新业务逻辑）：

| 工具 | 底层复用 | access |
|---|---|---|
| `search_bangumi(title, types)` | `bgm.search()` | read |
| `get_subject_detail(subject_id)` | `bgm.get_subject()` | read |
| `check_subject(subject_id)` | `_validate_subject_id()`（__init__.py:444） | read |
| `get_related_subjects(subject_id)` | `bgm.get_related_subjects()` | read |

> 写操作（`write_mapping_suggestion`）**不在 Agent 工具集内**——建议写入由调度器服务层完成，Agent 只输出决策，天然满足"不自动放通"。

**3.2.3 结构化输出解析器（新文件 `app/services/llm/output_parser.py`）**

- J3 组件：LLM 返回 JSON 的提取/校验/容错/失败降级（消费方：本场景决策解析 + Phase 4 诊断报告解析）
- 输出模型 `LLMSuggestion` dataclass：`subject_id: str`、`reason: str`

### 3.3 数据层

**3.3.1 `match_llm_jobs` 新表（LLM 执行层）**

```
match_llm_jobs
  id INTEGER PK AUTOINCREMENT
  created_at DATETIME
  request_title TEXT NOT NULL
  request_ori_title TEXT DEFAULT ''
  request_season INTEGER DEFAULT 1
  request_episode INTEGER DEFAULT 0
  request_media_type TEXT DEFAULT 'episode'
  request_release_date TEXT DEFAULT ''
  user_name TEXT DEFAULT ''
  source TEXT DEFAULT ''
  sync_record_id INTEGER          -- 关联 sync_records（补发/审计）
  candidate_id INTEGER            -- 建议写入 pending_candidates 后回填
  status TEXT DEFAULT 'pending'   -- pending/processing/done/failed/no_suggestion/applied/rejected
  attempts INTEGER DEFAULT 0
  last_attempt_at DATETIME
  last_error TEXT
  llm_subject_id TEXT DEFAULT ''  -- 建议结果（done 后回填）
  llm_reason TEXT DEFAULT ''
  trace_summary TEXT DEFAULT ''   -- 失败 trace 摘要（供 LLM 上下文，避免全量 JSON）
  finished_at DATETIME
```

**3.3.2 `pending_candidates` 加两列（用户确认层）**

```
ALTER TABLE pending_candidates ADD COLUMN llm_subject_id TEXT DEFAULT ''
ALTER TABLE pending_candidates ADD COLUMN llm_reason TEXT DEFAULT ''
```

> 不用 JSON TEXT：建议仅两个字段，独立列可查询/可校验；代码层用 `LLMSuggestion` dataclass 建模，不裸传 dict。无候选时 `candidates_json=[]` + 两列有值，复用现有确认/补发全链路。

### 3.4 状态机与联动

```
执行层（调度器驱动）：
  pending → processing → done / failed / no_suggestion
     done+有建议 → 写 pending_candidates(pending) + 回填 candidate_id + 通知

用户处理层（随候选确认/忽略 API 联动）：
  done → applied   （确认建议 → pending_candidates: pending→confirmed）
      → rejected  （忽略 → pending_candidates: pending→rejected）
```

联动实现：`confirm_pending_candidate` / `reject_pending_candidate`（__init__.py:214/513）内部追加一步，按 `candidate_id` 更新 `match_llm_jobs` 终态。两个维度互不阻塞：LLM 任务可独立重试/重跑，不影响用户确认。

### 3.5 `llm_match_scheduler`

- 位置：`app/services/llm_match_scheduler.py`（顶层，继承 `BaseScheduler`，同构 `bangumi_replay_scheduler`）
- 注册：`scheduler_bootstrap.py` 加 `JobSpec(scheduler_id="llm_match", runner=llm_match_scheduler)`
- 启用条件：`[sync] llm_match_assist=true` 且 LLM 配置存在（否则不启动，日志说明"LLM 配置缺失，匹配增强已禁用"）
- cron：`*/1 * * * *`（每 60s，可在 `[sync]` 配置覆盖）；每轮：统计 pending → 0 则跳过 → 逐条 processing → 两轮 chat → 写结果
- 重试：LLM 调用失败 attempts+1，< 3 重试，= 3 标 failed（`last_error` 记录）

### 3.6 匹配接入点（`_handle_match_failure`）

```
追加（原逻辑之后、返回值之前）：
1. 开关关 / LLM 配置缺失 → 跳过（日志说明）
2. trace 补 start_step("llm_assist")（status="pending"，reason="已提交 AI 评估"）——评估完成后
   结果承载于 match_llm_jobs，trace 只记录提交动作（异步评估不回写已入库的 trace）
3. 写 match_llm_jobs(pending)：请求字段 + sync_record_id + trace_summary（失败原因 + 候选 top-5 摘要）
4. 失败不阻塞主流程（落库异常仅日志）
```

### 3.7 确认闭环（bypass）

- 候选确认页 AI 推荐区块：「应用建议」按钮 → `POST /api/pending-candidates/{id}/confirm`（复用现有端点，body 带 `llm_subject_id`）→ `confirm_pending_candidate` 现有逻辑（校验 subject → 写映射 → 补发）+ 联动 `match_llm_jobs → applied`
- 补发成功 → `bangumi_id_found`（已匹配通知，现有逻辑自动触发）

### 3.8 通知

- `notification_registry.py`：`pending_candidate` 增加 `in_app_type="match_pending"`（站内信专用类型，同 sync_failed 模式）+ `in_app_title_template="匹配待确认：{title} {ep_label}"`
- 文案：通知标题/正文体现"已由 Agent 匹配，待用户手动确认"；`anime_not_found` 保留原义（Agent 无建议/未介入时的纯失败）
- `resolve_in_app_type("pending_candidate")` 现有逻辑自动生效（registry:403-412），无需改动通知服务

### 3.9 开关、降级、前后端检查

| 层 | 实现 |
|---|---|
| 配置 | `[sync] llm_match_assist`（bool，默认 false）；`[sync] llm_match_cron`（默认 `*/1 * * * *`，可覆盖） |
| 后端校验 | 配置保存 API：开启时校验 `get_llm_config()["api_key"]` 非空，否则拒绝并提示"需先配置 LLM"；`GET /api/sync/config` 返回 `llm_available` 标志 |
| 前端 | config 页"匹配增强"开关：仅 `llm_available` 时显示；未配置时显示提示"需先配置 LLM"（参照 dashboard 用量卡片条件渲染先例） |
| 降级 | 开关关/配置缺失/LLM 调用失败 → 不落任务或标 failed，**原失败路径零改动** |

### 3.10 KV cache 设计

**轮次内（必做，零成本）——两轮间前缀复用**

```
Round 1: [system(匹配指令+工具定义), user(请求上下文+候选摘要)]
Round 2: Round1 全部 + [assistant(tool_use blocks), user(tool_result blocks)]
```

Round 2 的 system+user 前缀与 Round 1 逐字节一致 → 命中缓存。**前提**：序列化顺序稳定（候选排序、字段顺序固定）。工具结果必须截断（防上下文膨胀；位于前缀之后不影响前缀缓存）。

**跨调用（推荐，可开关）——静态前缀复用**

- system（匹配规则+输出约束）+ 工具 schema 在多次失败匹配间完全相同；真实场景（连看番产生连续失败）同段命中率高
- Anthropic：`cache_control: {"type": "ephemeral"}` 标记（5 分钟 TTL；system+tools 体量不足 1024 tokens 时收益有限，可不开）
- OpenAI：自动前缀缓存，仅需前缀稳定
- provider 差异：**Anthropic 必须显式标记才缓存，OpenAI 自动**——`cache_control` 支持并入 3.2.1 工具协议一起补
- 成本：Anthropic 写入 +25%、读取 -90%；跨调用缓存做成配置开关，轮次内复用无条件做
- 用量：`job_name="llm_match"` 归属 `llm_usage`（沿用 extractor 约定）

---

## 4. 交互模型

**候选确认页（`pending_candidates.html`）现有结构保留，增量变更：**

```
列表行：
  ├─ 现有：时间/标题/季集/来源/候选数/状态/操作
  └─ 新增徽标：AI 评估中（processing）/ AI 推荐（done+建议）/ 普通（无 AI 介入）
              → 徽标数据源：GET /api/pending-candidates 返回 llm 字段 + 关联 match_llm_jobs 状态

详情弹窗：
  ├─ 现有：请求信息 / 候选列表（确认按钮）/ 手动指定 ID
  └─ 新增：AI 推荐区块（当 llm_subject_id 有值）
      ┌────────────────────────────────────────┐
      │ [AI 推荐] 剧场版 花咲くいろは HOME SWEET HOME (ID 49892) │
      │ 理由：标题语义相近……（llm_reason）               │
      │ [应用建议]（bypass，跳过手动流程）                │
      └────────────────────────────────────────┘
      应用后：写映射 + 自动补发 → 已匹配通知（bangumi_id_found）
```

状态实时反馈：列表/详情接口合并返回 `match_llm_jobs.status`，前端刷新即见"评估中 → 已推荐 → 已应用/已忽略"流转。

---

## 5. 文件变更清单

| 操作 | 文件 | 说明 |
|---|---|---|
| **通用能力** | | |
| 修改 | `app/services/llm/models.py` | ToolUseBlock/ToolResultBlock |
| 修改 | `app/services/llm/providers/anthropic.py` | tool wire 1:1 + stop_reason + cache_control |
| 修改 | `app/services/llm/providers/openai_compat.py` | tool 拆并转换 |
| 新增 | `app/services/llm/tools.py` | ToolDefinition/ToolRegistry/execute |
| 新增 | `app/services/llm/output_parser.py` | J3 结构化解析 + LLMSuggestion |
| **数据层** | | |
| 新增 | `app/core/database/match_llm_jobs.py` | 任务表 repository |
| 修改 | `app/core/database/connection.py` | 建表 + pending_candidates 迁移两列 |
| 修改 | `app/core/database/pending_candidates.py` | 查询/写入带 llm 两列 |
| **业务层** | | |
| 新增 | `app/services/matching/llm_assist.py` | 两轮 chat 编排 + 工具执行 + 结果落库 |
| 新增 | `app/services/llm_match_scheduler.py` | 调度器（BaseScheduler） |
| 修改 | `app/services/scheduler_bootstrap.py` | 注册 llm_match |
| 修改 | `app/services/sync_service/__init__.py` | `_handle_match_failure` 接入 / confirm、reject 联动 |
| 修改 | `app/services/sync_service/orchestrator.py` | 失败分支提交任务 + trace step |
| **API/配置** | | |
| 修改 | `app/api/sync.py` | pending-candidates 列表/详情带 llm 字段与任务状态 |
| 修改 | `app/api/config.py`（或 sync config 端点） | llm_match_assist 开关校验 + llm_available |
| 修改 | `app/core/config.py` | `[sync]` 新增键读取 |
| 修改 | `app/core/notification_registry.py` | pending_candidate 补 in_app |
| **前端** | | |
| 修改 | `templates/pending_candidates.html` | AI 推荐区块 + 徽标 |
| 修改 | `static/js/` | 徽标/应用建议交互 |
| 修改 | `templates/config.html` | 开关条件渲染 |
| **测试** | | |
| 新增/修改 | `tests/` | 见 §6 场景 |

> 总计：新增 ~5 文件，修改 ~13 文件。

---

## 6. BDD 测试场景

### 场景 M1 失败落任务
- **Given** `llm_match_assist=true` 且 LLM 已配置
- **When** 同步请求匹配失败（含候选）
- **Then** `match_llm_jobs` 新增 pending 行（含请求字段 + sync_record_id + trace_summary）
- **And** `pending_candidates` 按现有逻辑沉淀（不变）

### 场景 M2 无候选也落任务
- **Given** 匹配失败且 trace 无候选（如 `花开伊吕波剧场版` 类场景）
- **When** 失败处理执行
- **Then** `match_llm_jobs` 仍新增 pending 行（LLM 可补充搜索）

### 场景 M3 开关关闭不介入
- **Given** `llm_match_assist=false`
- **When** 同步请求匹配失败
- **Then** 不写 `match_llm_jobs`，原失败逻辑（error 记录 + anime_not_found + 候选沉淀）完全不变

### 场景 M4 LLM 配置缺失降级
- **Given** `llm_match_assist=true` 但 LLM api_key 为空
- **When** 失败处理执行
- **Then** 不落任务，日志包含"LLM 配置缺失"说明

### 场景 M5 调度器处理到 done（有建议）
- **Given** `match_llm_jobs` 有 pending 行
- **When** 调度器轮询执行两轮 chat（mock LLM 返回建议 subject_id）
- **Then** 任务状态 processing → done，`llm_subject_id`/`llm_reason` 回填
- **And** `pending_candidates` 新增/更新行（candidates_json 追加新 subject，llm 两列有值）
- **And** 触发 `pending_candidate` 通知（含站内信）

### 场景 M6 done 无建议
- **Given** mock LLM 返回"无合适条目"
- **When** 调度器完成评估
- **Then** 任务终态 `no_suggestion`，不产生 `pending_candidates` 行

### 场景 M7 LLM 失败重试
- **Given** mock LLM 连续失败
- **When** 调度器轮询
- **Then** attempts 递增，≤3 次重试；第 3 次后终态 `failed`，`last_error` 记录原因

### 场景 M8 应用建议（bypass）
- **Given** `pending_candidates` 行带 llm_subject_id，任务状态 done
- **When** 用户 POST confirm（body 带 llm_subject_id）
- **Then** 写入自定义映射 + 自动补发（复用 `_auto_replay_after_confirm`）
- **And** `match_llm_jobs` → applied；`pending_candidates` → confirmed
- **And** 补发成功后触发 `bangumi_id_found`

### 场景 M9 忽略建议
- **Given** 同 M8 前置
- **When** 用户 POST reject
- **Then** `pending_candidates` → rejected；`match_llm_jobs` → rejected

### 场景 M10 工具协议（Anthropic 1:1）
- **Given** 内部 assistant 消息含 ToolUseBlock
- **When** `AnthropicProvider._build_request`
- **Then** wire content blocks 与内部模型一一对应（对齐 phase2.1 T1/T6）

### 场景 M11 工具协议（OpenAI 拆并）
- **Given** 内部消息含 ToolUseBlock/ToolResultBlock
- **When** `OpenAICompatProvider._build_request`
- **Then** 拆为 assistant.tool_calls + 多条 role=tool 消息；arguments 为 JSON 字符串（对齐 T3/T4/T5）

### 场景 M12 结构化解析容错
- **Given** LLM 返回带前后缀文本的 JSON
- **When** `output_parser` 解析
- **Then** 提取成功；解析失败返回 None 且调用方降级为"无建议"

### 场景 M13 通知站内信
- **Given** 候选确认通知触发
- **When** `notification_service.notify("pending_candidate", ...)`
- **Then** 站内信写入（type=match_pending，标题含"匹配待确认"）

### 场景 M14 前端开关条件渲染
- **Given** LLM 未配置
- **When** 打开 config 页
- **Then** "匹配增强"开关不显示，展示"需先配置 LLM"提示；配置保存 API 拒绝开启并返回原因

---

## 7. 验证方式

| 层级 | 方式 |
|---|---|
| 单元 | §6 M1-M14 全部通过；工具协议回归 phase2.1 T1-T7 |
| 集成 | mock LLM：构造"跨季错配"（Re0 场景）与"无候选"（花开伊吕波场景）fixture，验证建议产出 + 候选落库 + 补发 |
| 手工 | 真实 LLM + 真实失败记录：观察候选页"评估中→已推荐→应用→已匹配"全链路；对比开关前后的失败处理延迟（webhook 均应立即返回） |
| 性能 | webhook 响应不因 LLM 介入变慢（异步落库即返回）；调度器轮询不堆积 |

---

## 8. 边界与 Phase 4 预留

**本次不做**：自由 Agent 循环（while budget 迭代）、`agent_runs/agent_steps` 表、日志诊断、知识沉淀、多用户分解。

**Phase 4 接缝（本次建立）**：
- `app/services/llm/tools.py`（ToolDefinition/registry/execute）→ Phase 4 Agent 循环直接消费
- `output_parser`（J3）→ 诊断报告解析复用
- `match_llm_jobs` 任务表 + 调度器模式 → Phase 4 诊断任务同构
- KV cache 前缀约定 → Phase 4 任意多轮任务沿用

---

## 9. 子 phase 拆分（评审通过后按序实施，一个 phase 只做一件事）

| 子 phase | 范围 | 依赖 |
|---|---|---|
| 3.0 | 通用能力：工具协议补齐（3.2.1）+ tools.py + output_parser | 无 |
| 3.1 | 数据层 + 调度器：match_llm_jobs 表/repo + pending_candidates 两列 + llm_assist 服务 + llm_match_scheduler | 3.0 |
| 3.2 | 接入 + 闭环 + 通知 + 前端：_handle_match_failure 接入 + confirm/reject 联动 + registry 站内信 + 候选页 UI + config 开关 | 3.1 |

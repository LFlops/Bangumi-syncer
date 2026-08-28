# Phase 3 实施 spec：通用 Agent 骨架 + LLM 匹配增强（Match Assist）

> 所属计划：Bangumi-Syncer Agent 化增量计划
> 性质：Phase 3 实施 spec——**通用 Agent 骨架先行，匹配增强作为首个落地场景**（对齐 2026-08 范围调整决议）
> 状态：**待评审**——评审通过后按 §9 子 phase 拆分实施
> 关联：
> - `agent-phase3-4-decisions.md`（原 P3 总结增强 → 本文档替换；J3/J4/C 系列接缝结论仍有效）
> - `agent-phase2.1-tools.md`（工具协议 spec——本文档 §3.2 补齐其未落地的代码）
> - `agent-phase3-agent.md`（Phase 4 参考资料：诊断场景设计、budget 完整版）
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

### 0.2 目标与范围调整（2026-08 决议）

**范围调整**：不做"匹配特化先行、未来再泛化"，而是**通用 Agent 骨架一次建对，匹配增强作为首个消费方**。

- 通用骨架：工具协议 + 工具注册表 + 结构化解析 + 轻量循环 + otel 概念追踪 + 通用会话表 `agent_runs`/`agent_steps`
- 首个场景：匹配失败后的 LLM 建议（产出**待用户手动确认的建议**，确认后写映射并自动补发；Agent 是**建议者而非定案者**，永不自动放通）
- 关键简化：匹配场景的请求上下文（title/season/media_type/release_date/user_name/match_trace 候选）**全部可从 `sync_records` 还原** → 不需要特化任务表，`agent_runs` 通用承载

### 0.3 对"workflow 越写越重"的回应

封装**通用能力原语**（工具协议/结构化输出/轻量循环/session 模型），场景只做接入；编排由 LLM 工具调用自主决定，不写死场景专用 workflow。

### 0.4 记忆边界（本功能无记忆依赖）

本功能是"单次失败 → 独立判定 → 会话状态机轮转"的闭环：**不写 `agent_working_memory`、不注入历史记忆**。与 Phase 2 记忆（summary 连续性场景）本质不同。记忆的"工具化检索 + 原文优先"演进属 Phase 4 备忘（见 §8），本次不做。

---

## 1. 决策清单（已确认，评审时仅核对）

| 编号 | 决策 | 结论 |
|---|---|---|
| D1 | Agent 介入位置 | `_handle_match_failure`（orchestrator.py:326）挂接，**不进匹配管道**；Agent 永不直接命中，永远产出待确认建议 |
| D2 | Agent 输出形态 | **直选一个推荐** `{subject_id, reason}`；新 subject 追加进 `candidates_json`；现有多候选列表与手动确认保留 |
| D3 | 落库形态 | **通用会话表 `agent_runs` + `agent_steps`**（替代特化 match_llm_jobs）+ `pending_candidates` 加 `llm_subject_id`/`llm_reason` 两列（用户确认层）；**不用 JSON TEXT 存结构化数据** |
| D4 | 执行方式 | 异步：失败匹配立即落库返回，`llm_match_scheduler` 定时任务轮询处理（AsyncIOScheduler，可直接 await LLM，无同步桥接问题） |
| D5 | 调度参数 | 轮询 60s、LLM 调用失败重试上限 3 次（对齐 `LLMClient.MAX_RETRIES=2` 保守风格） |
| D6 | 状态联动 | `agent_runs` 终态（applied/rejected）随 `pending_candidates` 确认/忽略 API 联动流转（经 sync_record_id 关联） |
| D7 | 开关/降级 | `[sync] llm_match_assist=false` 默认关；开关关 / LLM 配置缺失 / 调用失败 → 不落任务或标 failed，**原失败逻辑完全不变**；LLM 缺失时日志说明 |
| D8 | 前后端检查 | 后端校验 LLM 配置存在才允许开启；前端开关仅 LLM 已配置时显示（参照 dashboard 用量卡片条件渲染先例） |
| D9 | 通知 | 复用 `pending_candidate` 类型，**补站内信**（in_app_type + 标题模板），文案"已由 Agent 匹配，待确认"；不新增通知类型 |
| D10 | KV cache | 循环轮次间前缀复用（必做）+ 静态 system/tools 前缀跨调用缓存（推荐，可开关） |
| D11 | 循环形态 | **轻量 for 循环**：`max_iterations` 由 `thinking_level` 映射（off=1 / low=2 / medium=3 / high=5，可配置覆盖）；每轮 chat 返回 tool_use 则执行后继续，`end_turn` 或达上限即终止；不引入 Phase 4 完整 budget 系统 |
| D12 | 输出符合性 | 决策不靠自由文本提取：`submit_suggestion(subject_id, reason)` 作为**终止性工具**（调用即 break，结构保证只生效一次）+ provider 侧 `tool_choice` 强制结构化收尾 |
| D13 | 任务生命周期 | failed 允许**重新入队**（同 key 再次失败时复用记录重置 attempts）；终态超保留期（7 天）由调度器每轮顺带清理，删除数量打日志 |
| D14 | stop_reason 统一 | 所有会话结束必须记录 `stop_reason`：`end_turn` / `submit_suggestion` / `exhausted` / `failed` / `cancelled` / `error`——不裸用成功/失败二分（Phase 4 消费） |
| D15 | 追踪形态 | **自建 otel 概念模型**（trace_id/span_id/parent/span 层级/status/attribute 语义对齐，**不引入 opentelemetry SDK**——零新增依赖，符合项目风格；未来可映射导出） |
| D16 | span 粒度 | **每轮 LLM 调用 + 每次工具执行各一条 span**（agent_steps），可完整复盘 |
| D17 | 观测演进 | 内建"Agent 观测页"（Web UI + chart.js，Phase 4）；远期 `/metrics` 导出（Prometheus 格式，可选，Phase 5）——**默认不引入 Prometheus/Grafana**（单实例 SQLite 数据源用不上，自部署用户零额外负担） |
| D18 | 断点重入 | 会话状态机对 `processing` 的 run **可从断点恢复**：每轮 LLM 调用前后、工具调用前后持久化会话增量（`agent_steps.payload_json`）；服务重启后调度器扫描 processing 遗留 → 重建种子 messages（system 静态模板 + user 从 sync_records 还原）→ 按 steps 重放增量 → 续跑 |

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
- **Phase 2.1 工具协议未落地**：`llm/models.py` 只有 Text/Thinking/Redacted 三种 block；`anthropic.py:216` 对 `tool_use` 仅"跳过 + warning"；openai_compat 无 tools 参数支持
- 调度器先例：`bangumi_replay_scheduler.py`（BaseScheduler + AsyncIOScheduler + 队列空跳过）——`llm_match_scheduler` 同构
- KV cache 先例：`MemoryExtractor._summarize`（extractor.py:56-83）完整历史作前缀命中缓存
- 清理先例：`llm_usage.cleanup_old_llm_usage_logs(30)`（llm_usage.py:311）、`agent_memory.prune(keep=1000)`
- 可观测性：**无 opentelemetry 依赖**（pyproject 确认）；现有唯一"追踪"是 `MatchTrace`（业务级匹配过程，非可观测性追踪）

### 2.5 trace 机制现状

- `MatchTrace.start_step / record_step / to_dict`（match_trace.py:146-271）：steps 序列化进 `sync_records.match_trace` JSON，`finish()` 幂等
- **关键约束**：trace 入库后不可再改——异步评估结果**不回写 trace**，承载于 `agent_runs`/`agent_steps`（同步记录详情页联查）

---

## 3. 架构设计

### 3.1 总体流程

```
媒体库播放完成 → webhook（async 提交，立即返回 accepted）
  → 同步链路：匹配管道（custom_mapping → bangumi_data → api_search）
  → 成功：bangumi_id_found + 打格子（现有逻辑不变）
  → 失败：_handle_match_failure
      ├─ sync_records(error) + anime_not_found + 候选沉淀（原逻辑不变）
      ├─ trace 追加 llm_assist step（status="pending"，persist 之前）
      └─ 开关开 + LLM 可用 → 落 agent_runs(pending, task_type='match')
                                    ↓
llm_match_scheduler（AsyncIOScheduler，每 60s）
  ├─ 清理：终态且 ended_at 超 7 天 → DELETE + 日志
  ├─ 统计 pending → 0 则跳过
  └─ 逐条处理（matching/llm_assist.run）：
       ├─ agent 骨架循环（agent/loop.py）
       │    chat(span) → end_turn？→ 终止（stop_reason=end_turn）
       │        → tool_use？→ 执行工具(span) → 回填 tool_result + [剩余轮次：N] → 继续
       │        → submit_suggestion？→ 捕获参数，break（stop_reason=submit_suggestion）
       ├─ succeeded + 有建议 → 立即校验 + 写 pending_candidates（llm 两列）
       │       + 发待确认通知（方法返回前完成）
       ├─ 无建议 → no_suggestion（原失败路径不动）
       └─ LLM 调用失败 → attempts+1，≤3 重试；=3 → failed（last_error 记录）
                                    ↓
用户（候选确认页）
  ├─ 应用建议（bypass）→ 写映射 + _auto_replay_after_confirm 补发
  │       → bangumi_id_found（已匹配通知）；agent_runs → applied
  ├─ 手动确认其他候选（现有逻辑保留）；agent_runs → applied
  └─ 忽略 → rejected；agent_runs → rejected
```

### 3.2 通用能力封装（本次实现，Phase 4 消费）

**3.2.1 工具协议补齐（Phase 2.1 未落地部分）**

| 变更 | 文件 | 内容 |
|---|---|---|
| 修改 | `app/services/llm/models.py` | union 追加 `ToolUseBlock(id, name, input)` / `ToolResultBlock(tool_use_id, content, is_error)`（按 phase2.1 spec T1-T7 场景） |
| 修改 | `app/services/llm/providers/anthropic.py` | tool_use/tool_result wire 1:1 转换；`stop_reason="tool_use"`；**`tools` 参数透传（name/description/input_schema）+ `tool_choice` 支持 + cache_control 标记** |
| 修改 | `app/services/llm/providers/openai_compat.py` | 内部→wire 拆并（assistant.tool_calls / role=tool 拆分）；wire→内部合并回 ToolResultBlock；arguments JSON 解析失败兜底 `{"raw": ...}`；**`tools` 参数透传 + `tool_choice` function 模式** |

**3.2.2 工具注册表与执行器（新文件 `app/services/llm/tools.py`）**

- `ToolDefinition(name, description, parameters, handler, access: "read"/"write")`
- `parameters` 为 **JSON Schema**（OpenAI function calling 标准）；provider 侧把 schema 转为各自 wire 格式（anthropic `input_schema` / openai `parameters`）
- `ToolRegistry.register / execute(name, args)`：write 级工具执行前记录审计日志
- 本次注册的工具：

| 工具 | 底层复用 | access | schema 要点 |
|---|---|---|---|
| `search_bangumi(title, types)` | `bgm.search()` | read | `title: string (required)`、`subject_types: array[int] (default [2])` |
| `get_subject_detail(subject_id)` | `bgm.get_subject()` | read | `subject_id: string pattern ^\d+$ (required)` |
| `check_subject(subject_id)` | `_validate_subject_id()`（__init__.py:444） | read | 同上 |
| `get_related_subjects(subject_id)` | `bgm.get_related_subjects()` | read | 同上 |
| `submit_suggestion(subject_id, reason)` | 无（终止性工具） | **终止** | `subject_id: string pattern ^\d+$ (required)`、`reason: string maxLength 200` |

> `submit_suggestion` 是**终止性工具**：执行器不落库、仅捕获参数返回循环，循环收到即 break。写库由场景服务层完成（见 3.8）——Agent 只输出决策，天然满足"不自动放通"。

**3.2.3 结构化输出解析器（新文件 `app/services/llm/output_parser.py`）**

- J3 组件：LLM 返回 JSON 的提取/校验/容错/失败降级（消费方：本场景耗尽兜底 + Phase 4 诊断报告解析）
- 输出模型 `LLMSuggestion` dataclass：`subject_id: str`、`reason: str`
- 本场景中它是**兜底**（主路径是 submit_suggestion 工具化输出），不承担主决策解析

**3.2.4 Agent 骨架：轻量循环（新文件 `app/services/agent/loop.py`）**

```
async def run(task_ctx, *, max_iterations, tools, tool_choice_terminal, span_recorder) -> RunResult:
    messages = [system, user]                      # 场景构建
    for i in range(max_iterations):
        resp = await llm.chat(messages, tools=schemas, tool_choice=...)   # span: llm_chat
        if resp.stop_reason == "end_turn":
            return RunResult(stop_reason="end_turn", text=resp.content)
        for tc in resp.tool_calls:
            if tc.name == tool_choice_terminal:    # submit_suggestion
                return RunResult(stop_reason="submit_suggestion", suggestion=tc.input)
            result = await tools.execute(tc)       # span: tool_execute
            messages.append(tool_result(tc, result))
        messages.append(assistant(tc_blocks))
        messages.append(user(f"[剩余轮次：{remaining}]"))   # 透明预算
    return RunResult(stop_reason="exhausted")      # 预算刚性
```

关键点：
- **透明预算**：每轮注入 `[剩余轮次：N]`；剩余 0 时 prompt 强制"必须调用终止工具或声明放弃"
- **提前终止被接受**：第一轮就调用 submit_suggestion 表示已确定，接受并 break
- **结束必有 stop_reason（D14）**：end_turn / submit_suggestion / exhausted / failed / cancelled / error
- 本轮不实现 Phase 4 的 while budget（token/wall-time 预算系统），只做轮次上限

### 3.3 数据层

**3.3.1 `agent_runs` 通用会话表（替代特化 match_llm_jobs）**

```
agent_runs
  id INTEGER PK AUTOINCREMENT
  run_id TEXT NOT NULL UNIQUE            -- uuid：日志/步骤/记忆统一标识
  task_type TEXT NOT NULL                -- 'match' / 'summary' / 'diagnostic'（Phase 4 扩展）
  sync_record_id INTEGER                 -- 业务关联（match 场景：失败同步记录；上下文从 sync_records 还原）
  status TEXT DEFAULT 'pending'          -- pending/processing/succeeded/no_suggestion/failed/cancelled/exhausted/applied/rejected
                                          -- processing = 运行中，兼作"待恢复"标记（D18：重启后由调度器扫描恢复）
  stop_reason TEXT DEFAULT ''            -- end_turn/submit_suggestion/exhausted/failed/cancelled/error（D14）
  attempts INTEGER DEFAULT 0             -- LLM 调用失败次数（≤3 重试）
  last_attempt_at DATETIME
  last_error TEXT
  total_tokens INTEGER DEFAULT 0
  started_at DATETIME                    -- 处理开始（调度器置 processing 时）
  ended_at DATETIME                      -- 终态时间（清理依据，D13）
  created_at DATETIME                    -- 落库时间
```

> 请求上下文（title/ori_title/season/media_type/release_date/user_name/source）与候选列表**不冗余存储**——从 `sync_records`（含 match_trace）还原；user_name 用于构造用户 Bangumi API 实例。无预留字段。

**3.3.2 `agent_steps` span 表（D15/D16/D18）**

```
agent_steps
  id INTEGER PK AUTOINCREMENT
  run_id TEXT NOT NULL                   -- 关联 agent_runs.run_id
  span_id TEXT NOT NULL                  -- uuid（otel span id）
  parent_id TEXT DEFAULT ''              -- 父 span（root span = agent_runs 本身，不单独存）
  name TEXT NOT NULL                     -- llm_chat / tool_execute
  status TEXT DEFAULT 'ok'               -- ok / error
  model TEXT DEFAULT ''                  -- llm_chat：模型名
  tokens INTEGER DEFAULT 0               -- llm_chat：token 数
  latency_ms INTEGER DEFAULT 0
  tool_name TEXT DEFAULT ''              -- tool_execute：工具名
  input_summary TEXT DEFAULT ''          -- 入参摘要（截断 ≤500 字符）
  error TEXT DEFAULT ''
  payload_json TEXT DEFAULT ''           -- 会话增量/响应（D18，截断 ≤2KB/条，见写入时机表）
  started_at DATETIME
  ended_at DATETIME
```

> span 语义对齐 otel：root span = 一次会话（agent_runs 行），child spans = 每轮 LLM 调用 + 每次工具执行各一条（D16）。不引入 SDK，概念可未来映射导出（§8）。
>
> **payload_json 写入时机（D18，每轮 LLM 调用前后、工具调用前后）**：

| span | 前（start） | 后（end） |
|---|---|---|
| `llm_chat` | started_at 记录 | `{response: {stop_reason, content 摘要, tool_calls 摘要}}` + tokens/latency |
| `tool_execute` | `{input: 入参摘要}` | `{result: 截断 tool_result, delta: [assistant(tool_use), user(tool_result)]}`——delta = 本轮会话增量，断点重放的关键 |

> **JSON TEXT 边界说明**：`payload_json` 存的是**会话事件内容**（LLM 对话协议定义的自由结构：role + content blocks），与 otel span attributes 同理——观测/事件数据允许 JSON；业务结构化数据（subject_id/reason 等）仍用独立列。

**3.3.3 `pending_candidates` 加两列（用户确认层）**

```
ALTER TABLE pending_candidates ADD COLUMN llm_subject_id TEXT DEFAULT ''
ALTER TABLE pending_candidates ADD COLUMN llm_reason TEXT DEFAULT ''
```

> 不用 JSON TEXT：建议仅两个字段，独立列可查询/可校验；代码层用 `LLMSuggestion` dataclass 建模，不裸传 dict。无候选时 `candidates_json=[]` + 两列有值，复用现有确认/补发全链路。

### 3.4 状态机与联动

```
执行层（调度器驱动）：
  pending → processing → succeeded / no_suggestion / failed / exhausted
     succeeded（有建议）→ 写 pending_candidates(pending) + 发通知

断点恢复（D18）：
  processing ──服务重启──→ 调度器恢复扫描：重建种子 + 重放增量 → 续跑（仍 processing，直至终态）

用户处理层（随候选确认/忽略 API 联动，经 sync_record_id 关联）：
  succeeded → applied   （确认建议 → pending_candidates: pending→confirmed）
           → rejected  （忽略 → pending_candidates: pending→rejected）

生命周期（D13）：
  failed ──同 key 再次失败到达──→ 复用记录重置 attempts=0 → pending（重新入队）
  终态（succeeded/no_suggestion/failed/applied/rejected/exhausted）──ended_at 超 7 天──→ 清理删除
```

联动实现：`confirm_pending_candidate` / `reject_pending_candidate`（__init__.py:214/513）内部追加一步，按 `sync_record_id` 更新 `agent_runs` 终态（applied/rejected）。两个维度互不阻塞：LLM 任务可独立重试/重跑，不影响用户确认。

### 3.5 追踪设计（otel 概念，D15/D16/D18）

- span 记录器：`app/services/agent/trace.py`——`start_span(run_id, name, ...)` / `end_span(...)`，写 `agent_steps`（独立 best-effort 事务）
- **会话增量记录（D18）**：`tool_execute` span end 时写入 `delta`（assistant tool_use + user tool_result）；`llm_chat` span end 时写入响应摘要——`agent_steps` 从"纯观测"升级为**可重放会话日志**（jsonl append-only 思想的 DB 落地）
- root span（agent_runs 行）由调度器/场景服务维护（status/stop_reason/tokens/ended_at）
- **断点恢复（D18）**：
  1. 调度器启动后首轮扫描 `status='processing'` 的 run（上次崩溃遗留）
  2. 重建种子 messages：system（静态匹配指令）+ user（从 sync_records 还原）
  3. 按 `agent_steps` 顺序重放：每条 `tool_execute.payload.delta` 追加 `[assistant, user(tool_result)]`
  4. 续跑：剩余轮次 = `max_iterations - 已执行 llm_chat 数`
     - 最后一条是完整 `llm_chat`（响应已存）且未产生 tool/终止 → 直接用已存响应继续（不重新调 LLM）
     - 最后是 `tool_execute`（完整）→ 正常续跑下一轮 chat
     - 极端情况（响应未存）→ 从上一完整点重放后重新 chat（正确性优先，可接受）
- 追踪 API（Phase 3 最小，Phase 4 观测页消费）：
  - `GET /api/agent/runs/{run_id}` → run 信息（status/stop_reason/tokens）
  - `GET /api/agent/runs/{run_id}/steps` → span 列表（按 started_at 排序）
- Phase 3 展示：候选确认页"AI 评估过程"折叠区（见 §4）；完整观测页 Phase 4（§8）

### 3.6 `llm_match_scheduler`

- 位置：`app/services/llm_match_scheduler.py`（顶层，继承 `BaseScheduler`，同构 `bangumi_replay_scheduler`）
- 注册：`scheduler_bootstrap.py` 加 `JobSpec(scheduler_id="llm_match", runner=llm_match_scheduler)`
- 启用条件：`[sync] llm_match_assist=true` 且 LLM 配置存在（否则不启动，日志说明"LLM 配置缺失，匹配增强已禁用"）
- cron：`*/1 * * * *`（每 60s，可在 `[sync]` 配置覆盖）
- 每轮顺序：
  1. **恢复扫描（D18）**：首轮（或每轮）检查 `status='processing'` 的遗留 run → 重建种子 messages → 重放 `agent_steps` 增量 → 续跑（幂等：恢复中崩溃 → 下次再扫）
  2. **清理**：终态且 `ended_at` 超保留期（7 天，可配置）→ DELETE + 日志删除数量（同构 `llm_usage.cleanup_old_llm_usage_logs`）
  3. 统计 pending → 0 则跳过
  4. 逐条：置 processing + started_at → `await llm_assist.run(run)` → 写结果（方法内完成，见 3.8）
- 重试：LLM 调用失败 attempts+1，< 3 重试，= 3 标 failed（`last_error` 记录）

### 3.7 匹配接入点（`_handle_match_failure`）

```
追加：
1. 开关关 / LLM 配置缺失 → 跳过（日志说明）
2. 同 key（title+season+user+source）已存在 failed 记录 → 复用重置（D13 重新入队），否则新增
3. trace 追加 start_step("llm_assist")（status="pending"，reason="已提交 AI 评估"）
   ——必须发生在 _persist_sync_record 之前（trace 入库后不可改）；
   评估结果异步承载于 agent_runs/agent_steps，不回写 trace
4. 写 agent_runs(pending, task_type='match')：run_id + sync_record_id
5. 失败不阻塞主流程（落库异常仅日志）
```

### 3.8 确认闭环（bypass）与写操作时序

- **写入时序（确认）**：`llm_assist.run()` 方法内、方法返回前完成全部写入——校验 subject_id → 写 `pending_candidates`（llm 两列）→ `agent_runs` succeeded + stop_reason → 发待确认通知。调度器本轮即结束，无后续步骤
- 候选确认页 AI 推荐区块：「应用建议」按钮 → `POST /api/pending-candidates/{id}/confirm`（复用现有端点，body 带 `llm_subject_id`）→ `confirm_pending_candidate` 现有逻辑（校验 subject → 写映射 → 补发）+ 联动 `agent_runs → applied`
- 补发成功 → `bangumi_id_found`（已匹配通知，现有逻辑自动触发）

### 3.9 通知

- `notification_registry.py`：`pending_candidate` 增加 `in_app_type="match_pending"`（站内信专用类型，同 sync_failed 模式）+ `in_app_title_template="匹配待确认：{title} {ep_label}"`
- 文案：通知标题/正文体现"已由 Agent 匹配，待用户手动确认"；`anime_not_found` 保留原义（Agent 无建议/未介入时的纯失败）
- `resolve_in_app_type("pending_candidate")` 现有逻辑自动生效（registry:403-412），无需改动通知服务

### 3.10 开关、降级、前后端检查

| 层 | 实现 |
|---|---|
| 配置 | `[sync] llm_match_assist`（bool，默认 false）；`[sync] llm_match_cron`（默认 `*/1 * * * *`）；`[sync] llm_match_retention_days`（默认 7） |
| 后端校验 | 配置保存 API：开启时校验 `get_llm_config()["api_key"]` 非空，否则拒绝并提示"需先配置 LLM"；`GET /api/sync/config` 返回 `llm_available` 标志 |
| 前端 | config 页"匹配增强"开关：仅 `llm_available` 时显示；未配置时显示提示"需先配置 LLM"（参照 dashboard 用量卡片条件渲染先例） |
| 降级 | 开关关/配置缺失/LLM 调用失败 → 不落任务或标 failed，**原失败路径零改动** |

### 3.11 死循环防护（三层，不上 Phase 4 budget 系统）

1. **`max_iterations` 刚性上限**：thinking_level 映射（off=1/low=2/medium=3/high=5），循环结构保证不会超过
2. **`submit_suggestion` 调用即 break**：终止性工具，收到即终止，不可能重复调用
3. **每轮 `max_tokens` 限制**：沿用现有 LLM 配置

### 3.12 KV cache 设计

**轮次内（必做，零成本）——循环轮次间前缀复用**

```
Round N:   [system+user, assistant(tool_use), user(tool_result), ... ]
Round N+1: 前述全部 + 新 [assistant(tool_use), user(tool_result)]
```

每轮消息列表为**前缀追加**（只增不改）：system+user 前缀在所有轮次间逐字节一致 → 后续轮次全部命中缓存。**前提**：序列化顺序稳定（候选排序、字段顺序固定）；工具结果必须截断（防上下文膨胀；位于前缀之后不影响前缀缓存）。

**跨调用（推荐，可开关）——静态前缀复用**

- system（匹配指令+输出约束）+ 工具 schema 在多次失败匹配间完全相同；真实场景（连看番产生连续失败）同段命中率高
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
  └─ 新增徽标：AI 评估中（processing）/ AI 推荐（succeeded+建议）/ 普通（无 AI 介入）
              → 徽标数据源：GET /api/pending-candidates 返回 llm 字段 + 关联 agent_runs 状态

详情弹窗：
  ├─ 现有：请求信息 / 候选列表（确认按钮）/ 手动指定 ID
  ├─ 新增：AI 推荐区块（当 llm_subject_id 有值）
  │    [AI 推荐] 剧场版 花咲くいろは HOME SWEET HOME (ID 49892)
  │    理由：标题语义相近……（llm_reason）
  │    [应用建议]（bypass，跳过手动流程）
  │    应用后：写映射 + 自动补发 → 已匹配通知（bangumi_id_found）
  └─ 新增：AI 评估过程折叠区（GET /api/agent/runs/{run_id}/steps）
       展开：span 简表（LLM 调用 N 轮 / 工具调用列表 / 每轮耗时）
```

状态实时反馈：列表/详情接口合并返回 `agent_runs.status`，前端刷新即见"评估中 → 已推荐 → 已应用/已忽略"流转。

---

## 5. 文件变更清单

| 操作 | 文件 | 说明 |
|---|---|---|
| **LLM 能力层（通用）** | | |
| 修改 | `app/services/llm/models.py` | ToolUseBlock/ToolResultBlock |
| 修改 | `app/services/llm/providers/anthropic.py` | tool wire 1:1 + tools/tool_choice 透传 + cache_control |
| 修改 | `app/services/llm/providers/openai_compat.py` | tool 拆并 + tools/tool_choice 透传 |
| 新增 | `app/services/llm/tools.py` | ToolDefinition/ToolRegistry/execute + JSON Schema |
| 新增 | `app/services/llm/output_parser.py` | J3 结构化解析 + LLMSuggestion |
| **Agent 骨架（通用）** | | |
| 新增 | `app/services/agent/__init__.py` | 包入口 |
| 新增 | `app/services/agent/loop.py` | 轻量循环（max_iterations + 终止工具 + 透明预算 + stop_reason） |
| 新增 | `app/services/agent/trace.py` | otel 概念 span 记录器 + 会话增量持久化（payload_json） + 断点重放 |
| **数据层** | | |
| 新增 | `app/core/database/agent_runs.py` | agent_runs + agent_steps repository（含重新入队/清理） |
| 修改 | `app/core/database/connection.py` | 建表 + pending_candidates 迁移两列 |
| 修改 | `app/core/database/pending_candidates.py` | 查询/写入带 llm 两列 |
| **场景层（match）** | | |
| 新增 | `app/services/matching/llm_assist.py` | 场景接入：上下文构建（从 sync_records 还原）+ 工具注册 + 调用 loop + 方法内写入 |
| 新增 | `app/services/llm_match_scheduler.py` | 调度器（BaseScheduler，含清理） |
| 修改 | `app/services/scheduler_bootstrap.py` | 注册 llm_match |
| 修改 | `app/services/sync_service/__init__.py` | `_handle_match_failure` 接入 / confirm、reject 联动 / 重新入队 |
| 修改 | `app/services/sync_service/orchestrator.py` | 失败分支提交任务 + trace step（persist 前） |
| **API/配置** | | |
| 新增 | `app/api/agent_runs.py` | GET /api/agent/runs/{id} + /steps（追踪查询） |
| 修改 | `app/api/sync.py` | pending-candidates 列表/详情带 llm 字段与任务状态 |
| 修改 | `app/api/config.py`（或 sync config 端点） | llm_match_assist 开关校验 + llm_available |
| 修改 | `app/core/config.py` | `[sync]` 新增键读取 |
| 修改 | `app/core/notification_registry.py` | pending_candidate 补 in_app |
| 修改 | `app/main.py` | 注册 agent_runs router |
| **前端** | | |
| 修改 | `templates/pending_candidates.html` | AI 推荐区块 + 徽标 + 评估过程折叠区 |
| 修改 | `static/js/` | 徽标/应用建议/折叠区交互 |
| 修改 | `templates/config.html` | 开关条件渲染 |
| **测试** | | |
| 新增/修改 | `tests/` | 见 §6 场景 |

> 总计：新增 ~9 文件，修改 ~14 文件。

---

## 6. BDD 测试场景

### 场景 M1 失败落任务
- **Given** `llm_match_assist=true` 且 LLM 已配置
- **When** 同步请求匹配失败（含候选）
- **Then** `agent_runs` 新增 pending 行（task_type='match'，含 run_id + sync_record_id）
- **And** trace 含 llm_assist step（status="pending"）；`pending_candidates` 按现有逻辑沉淀（不变）

### 场景 M2 无候选也落任务
- **Given** 匹配失败且 trace 无候选（如 `花开伊吕波剧场版` 类场景）
- **When** 失败处理执行
- **Then** `agent_runs` 仍新增 pending 行（LLM 可补充搜索）

### 场景 M3 开关关闭不介入
- **Given** `llm_match_assist=false`
- **When** 同步请求匹配失败
- **Then** 不写 `agent_runs`，原失败逻辑（error 记录 + anime_not_found + 候选沉淀）完全不变

### 场景 M4 LLM 配置缺失降级
- **Given** `llm_match_assist=true` 但 LLM api_key 为空
- **When** 失败处理执行
- **Then** 不落任务，日志包含"LLM 配置缺失"说明

### 场景 M5 循环执行到成功（有建议）
- **Given** `agent_runs` 有 pending 行
- **When** 调度器调用 `llm_assist.run`（mock LLM：第一轮 tool_use 调 `search_bangumi`，第二轮调 `submit_suggestion`）
- **Then** 循环按轮次推进，第二轮收到 submit_suggestion 即 break，`stop_reason=submit_suggestion`
- **And** 任务 processing → succeeded，`total_tokens` 回填
- **And** `pending_candidates` 新增/更新行（candidates_json 追加新 subject，llm 两列有值）
- **And** 触发 `pending_candidate` 通知（含站内信）

### 场景 M6 end_turn 直接终止
- **Given** mock LLM 首轮返回 end_turn（无工具调用）
- **When** 调度器处理
- **Then** 循环立即终止（`stop_reason=end_turn`），不追加轮次，任务 → no_suggestion

### 场景 M7 预算耗尽无建议
- **Given** mock LLM 每轮都调工具但不调用 submit_suggestion
- **When** 调度器处理
- **Then** 循环达 max_iterations 终止（`stop_reason=exhausted`），任务 → no_suggestion，不追加轮次

### 场景 M8 LLM 调用失败重试
- **Given** mock LLM 连续抛错
- **When** 调度器轮询
- **Then** attempts 递增，≤3 次重试；第 3 次后终态 `failed`，`stop_reason=failed`，`last_error` 记录原因

### 场景 M9 failed 重新入队
- **Given** 同 key 存在 failed 记录（attempts=3）
- **When** 该标题再次匹配失败到达
- **Then** 复用该记录：attempts 重置 0、status 回 pending、时间刷新，不新增行

### 场景 M10 终态清理
- **Given** 存在终态记录且 `ended_at` 超保留期（7 天）
- **When** 调度器每轮清理
- **Then** 记录被删除，日志记录删除数量；未超保留期的不删

### 场景 M11 应用建议（bypass）
- **Given** `pending_candidates` 行带 llm_subject_id，任务状态 succeeded
- **When** 用户 POST confirm（body 带 llm_subject_id）
- **Then** 写入自定义映射 + 自动补发（复用 `_auto_replay_after_confirm`）
- **And** `agent_runs` → applied；`pending_candidates` → confirmed
- **And** 补发成功后触发 `bangumi_id_found`

### 场景 M12 忽略建议
- **Given** 同 M11 前置
- **When** 用户 POST reject
- **Then** `pending_candidates` → rejected；`agent_runs` → rejected

### 场景 M13 工具协议（Anthropic 1:1 + tool_choice）
- **Given** 内部 assistant 消息含 ToolUseBlock，`tool_choice` 指定 submit_suggestion
- **When** `AnthropicProvider._build_request`
- **Then** wire content blocks 与内部模型一一对应；tools 参数含 input_schema；tool_choice 透传（对齐 phase2.1 T1/T6）

### 场景 M14 工具协议（OpenAI 拆并）
- **Given** 内部消息含 ToolUseBlock/ToolResultBlock
- **When** `OpenAICompatProvider._build_request`
- **Then** 拆为 assistant.tool_calls + 多条 role=tool 消息；arguments 为 JSON 字符串（对齐 T3/T4/T5）

### 场景 M15 submit_suggestion 校验失败
- **Given** mock LLM 调用 submit_suggestion 但 subject_id 非法（非数字 / 不存在 / 类型非动画）
- **When** `llm_assist.run` 校验
- **Then** 不落 pending_candidates，任务终态 `no_suggestion`（或带 last_error 说明）

### 场景 M16 结构化解析兜底
- **Given** 循环耗尽，最后响应为带前后缀文本的 JSON
- **When** `output_parser` 提取
- **Then** 提取成功则按建议落库；解析失败返回 None → `no_suggestion`

### 场景 M17 通知站内信
- **Given** 候选确认通知触发
- **When** `notification_service.notify("pending_candidate", ...)`
- **Then** 站内信写入（type=match_pending，标题含"匹配待确认"）

### 场景 M18 前端开关条件渲染
- **Given** LLM 未配置
- **When** 打开 config 页
- **Then** "匹配增强"开关不显示，展示"需先配置 LLM"提示；配置保存 API 拒绝开启并返回原因

### 场景 M19 span 记录（D16）
- **Given** 一次含 2 轮 chat + 3 次工具调用的循环执行
- **When** 调度器处理完成
- **Then** `agent_steps` 新增 5 条 span（2 条 llm_chat + 3 条 tool_execute），含 model/tokens/latency/tool_name/input_summary，parent 归属正确

### 场景 M20 stop_reason 完整性（D14）
- **Given** 各种结束路径（end_turn / submit_suggestion / exhausted / failed）
- **When** 循环终止
- **Then** `agent_runs.stop_reason` 正确记录对应枚举，无空值

### 场景 M21 追踪 API
- **Given** `agent_runs` 有已完成 run
- **When** `GET /api/agent/runs/{run_id}` 与 `/steps`
- **Then** 返回 run 信息（status/stop_reason/tokens）与 span 列表（按时间排序）

### 场景 M22 崩溃恢复（断点重入）
- **Given** 服务重启，`agent_runs` 存在 `processing` 遗留 run（已执行 1 轮 chat + 1 次工具，agent_steps 完整）
- **When** 调度器恢复扫描
- **Then** 重建种子 messages → 重放 delta（assistant + tool_result）→ 从第 2 轮续跑
- **And** 续跑轮数 = max_iterations - 已执行 llm_chat 数；最终正常完成（succeeded/failed），不重复执行已记录的步骤

### 场景 M23 崩溃中间态
- **Given** 服务在 llm_chat 响应已存但工具未执行的间隙崩溃
- **When** 调度器恢复扫描
- **Then** 直接用已存响应继续（不重新调 LLM）；极端情况（响应未存）→ 从上一完整点重放后重新 chat

---

## 7. 验证方式

| 层级 | 方式 |
|---|---|
| 单元 | §6 M1-M23 全部通过；工具协议回归 phase2.1 T1-T7 |
| 集成 | mock LLM：构造"跨季错配"（Re0 场景）与"无候选"（花开伊吕波场景）fixture，验证建议产出 + 候选落库 + 补发；循环防护（M6/M7）与生命周期（M9/M10）；span 完整性（M19/M20）；**断点恢复（M22/M23：模拟 processing 遗留 + 重放续跑）** |
| 手工 | 真实 LLM + 真实失败记录：观察候选页"评估中→已推荐→应用→已匹配"全链路 + 评估过程折叠区 span 展示；对比开关前后的失败处理延迟（webhook 均应立即返回） |
| 性能 | webhook 响应不因 LLM 介入变慢（异步落库即返回）；调度器轮询不堆积；清理不删除未过期记录 |

---

## 8. 边界与 Phase 4/5 预留

**本次不做**：完整 while budget 系统（token/wall-time）、`/api/agent/*` 通用执行接口、诊断场景、知识库、记忆工具、取消语义（C2）完整实现、完整观测页。

**Phase 4 接缝（本次建立）**：
- `app/services/llm/tools.py`（ToolDefinition/registry/execute + JSON Schema）→ Agent 循环直接消费
- `app/services/agent/loop.py` + `trace.py`（stop_reason / span 语义 / 会话增量持久化）→ Phase 4 while budget 循环的骨架、可观测性与**断点重入**基础（D18）
- `agent_runs`/`agent_steps` 通用表 → Phase 4 诊断任务同构（task_type='diagnostic'），观测页数据源
- `output_parser`（J3）→ 诊断报告解析复用
- KV cache 前缀约定 → Phase 4 任意多轮任务沿用

**Phase 4 内容（范围声明）**：
- 完整 Agent 循环（budget.py：token/wall-time + 取消 C2）+ 诊断场景
- **内建"Agent 观测页"**（Web UI + chart.js：概览卡片 + 运行列表 + span 瀑布），复用现有仪表盘先例
- 记忆工具化（`tools/memory.py`：search_memory 返回 full_text 原文片段 / store_memory / export_memory 导出 jsonl）——"原文优先 + 工具化检索"演进（2026-08 讨论备忘）
- 统一 session 模型深化：取消语义、消息历史持久化策略（D18 已建立增量持久化骨架，Phase 4 评估完整消息保留策略）

**Phase 5 内容（评估）**：
- `/metrics` 端点（Prometheus 文本格式，从 agent_runs 聚合）→ 重度自部署用户接 Grafana/Tempo（默认不开）
- knowledge_base 知识沉淀 + 记忆重要性维度（feedback→知识库联动）
- 匹配建议历史反馈沉淀（确认/拒绝 → 后续建议参考）

---

## 9. 子 phase 拆分（评审通过后按序实施，一个 phase 只做一件事）

| 子 phase | 范围 | 依赖 |
|---|---|---|
| 3.0 | **LLM 能力层**：工具协议补齐（3.2.1）+ tools.py + output_parser | 无 |
| 3.1 | **Agent 骨架**：agent/loop.py + agent/trace.py（含 payload_json 会话增量 + 断点重放）+ agent_runs/agent_steps 表/repo + 追踪 API + 循环/span/恢复单测（M13/M14/M19/M20/M21/M22/M23） | 3.0 |
| 3.2 | **match 场景**：llm_assist + llm_match_scheduler + _handle_match_failure 接入 + confirm/reject 联动 + 通知 + 重新入队/清理（M1-M12/M15-M17） | 3.1 |
| 3.3 | **前端与开关**：候选页 AI 推荐 + 徽标 + 评估过程折叠区 + config 开关条件渲染（M18） | 3.2 |
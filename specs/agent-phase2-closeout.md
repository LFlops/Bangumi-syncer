# Phase 2 收尾定稿（Closeout）：记忆三件套

> 所属计划：Bangumi-Syncer Agent 化增量计划
> 性质：Phase 2（2.0.1 + 2.0.2 + 2.0.3）**定稿文档**——定义、审查、决断、验收的单一权威来源
> 状态：✅ 已收尾（2026-08）
> 关联：`agent-phase3-4-decisions.md`（B/C/D 系列待定稿清单）、`agent-phase3-summary-enhanced.md`（Phase 3 骨架）、`agent-phase2.1-tools.md` / `agent-phase2.2-openai-refactor.md` / `agent-phase2.3-feedback.md`（内容有效，执行归期见 §9）

---

## 1. Phase 2 定义（定稿）

> **Phase 2 = 记忆三件套（2.0.1 写入 + 2.0.2 读取 / 2.0.3 清理）**——"给定时任务增加记忆"即 Phase 2 全部范围。
> 原子化的记忆写入（store_and_mark）、FTS5 检索注入、消费标记、prune 归档、改名/清空为已交付能力。

其他原 Phase 2 编号的归期（裁决记录）：

| 原编号 | 内容 | 新归期 | 理由 |
|---|---|---|---|
| 2.1 | 工具协议（tool_use/tool_result） | **Phase 4.x** | 记忆链路纯文本 chat 零依赖；D1 自检走结构化 JSON 文本而非原生 tool calling；唯一消费者是 P4 工具注册表 |
| 2.2 | openai 重构 + reasoning_effort | **本轮已完成**（见 §6） | 不阻塞记忆链路但属 provider 层欠账 |
| 2.3 | 用户反馈（feedback） | **Phase 3.1** | 需 Web 交互（通知条目反馈按钮+弹窗）；代码预留已移除，恢复清单见 `agent-phase3-summary-enhanced.md` §A |

---

## 2. 记忆架构全景（接受 E1 裁决的前提）

当前实现为**两层半**，全部是"任务执行记忆"（短中期），无向量层：

```
任务执行记忆
├─ 热层 agent_working_memory     ← 高频池：FTS5 索引(task_type, summary, outcome)，
│                                  每任务保留最近 1000 条（prune 下沉超出部分）
│    消费方式：get_recent 最近 N 条（连续性）+ search_fts 关键词命中（相关性，不占额度）
│    消费标记：sync_records.consumed_run_id（防窗口重叠重复展开，store_and_mark 原子写入）
├─ 冷层 agent_working_memory_archive ← prune 下沉产物：无索引，search_archive 仅 LIKE 检索
│          ⚠️ 现状：已参与 related 联表反查（跨窗口同剧回忆）；search_archive 留给 P4 Agent 全量查史
└─（未建层）
    ├─ 用户偏好反馈  → Phase 3.1（feedback 条目长期保留、强约束注入）
    ├─ knowledge_base → Phase 4（跨任务诊断知识沉淀）
    └─ 向量语义层    → Phase 5 按需（sqlite-vec + 混合检索 RRF）
```

**分冷热的账本**：热层带 FTS5 索引保证检索性能、1000 条控制候选池噪音；冷层零成本保底（prune 里一条 INSERT），避免"删历史"或"全量入索引"二选一。对当前 summary 场景收益薄——是给 P4 Agent 的"提前存款"，P4 不做则白存。

**E1 命名裁决**：维持 `agent_working_memory` 现名。三层语义不冲突：`agent_working_memory`（任务级长期记忆）/ `agent_runs + agent_steps`（P4，单次运行轨迹）/ `knowledge_base`（P4/P5，跨任务沉淀知识）——工作记忆、轨迹、知识库是层次而非撞车。

---

## 3. 审查修复记录（W 系列）

| # | 问题 | 修复 |
|---|---|---|
| W1 | MemoryService 构造签名带 `sync_records_repo` 但 `self._sync` 从未被读（死参数） | ✅ 构造收敛为单参（消费标记联动在 repo 内部事务完成，见 hy-review20260817 #7） |
| W2 | extractor 摘要调用 `llm.chat()` 未带 job 标识 → 摘要 token 在 llm_usage_logs 落入空 job_name 组，真实成本统计缺一半 | ✅ `extract_and_store` 增加 `job_name` 参数透传；summary service 传 `job_config.name`——主调用与摘要调用同组可对账（自检脚本 §6） |
| W3 | trigram 降级静默（SQLite <3.34 时中文子串检索失效无提示） | ✅ 降级分支补 warning 日志（含当前版本与建议） |

**W3 答疑（SQLite 版本由什么决定）**：`sqlite3` 是 Python **标准库模块**，编译期链接运行环境的 libsqlite3 C 库——**uv.lock 锁不住它**（uv 只管 PyPI 包）。来源：本地 = 系统 Python/CLT（本机 3.53.4）；Docker = 基础镜像 apt 包（`python:3.x-slim` 基于 Debian bookworm 自带 ≥3.40，安全）；Alpine/老发行版可能 <3.34。降级分支是防御性的，现在有日志可查。

---

## 4. E 系列决断记录

### E1. 命名超前占用 → 维持现名（裁决见 §2）

### E2. 记忆检索：FTS/摘要约束 → 联表反查（v7 终稿）✅ 已实施
- 演进链：① 摘要强制含番剧名（早期方案，已废弃）→ ② 单列 titles（已废弃）→ **③ 联表反查（终稿）**：`get_related_titles` 经 `sync_records.consumed_run_id` 反查历史总结（主表+归档 UNION、GROUP BY 去重、日期倒序、空标题短路、任务隔离）。消费标记是"该记录被哪次总结消费过"的显式关联，比 FTS 子串猜测精确且自然覆盖冷层。
- **摘要**（v7）：prompt 仅软约束（"50–100 字为宜，根据内容量自然把握"），**删除 `_SUMMARY_MAX_LEN=200` 硬截断**——成功路径 LLM 输出原文入库零截断；**LLM 摘要失败 → 跳过不写**（与空响应同路径，无截断兜底残留）。
- **FTS 停用**：`search_fts` 标 deprecated——FTS 搜索的唯一输入（标题词）与联表相同而联表更优；物理结构（虚表/触发器/索引）保留，供 Phase 5 混合检索（FTS5+向量 RRF）复用。trigram 告警降为"无功能影响"提示。
- 连带修正：D4 的收益表述已改为"结构化输出提升的是通知渲染与后续解析，非 FTS 召回"。

### E3. 配置终态：memory_limit / related_limit（0=关，0–1000）✅ 已实施
- **背景演进**：早期 1–50 条数 + memory_enabled 开关 → 中途尝试 memory_days（90 天/放开上限，伤害季度低频率用户分析后被否决）→ 最终确定**条数隐式开关**（未发布，直接破坏性重构，无迁移）。
- **配置**：`memory_limit`（0=关；>0=最近 N 条摘要注入，上限 1000 对齐 prune）、`related_limit`（0=关；>0=同剧关联最近 N 条）。`memory_enabled`/`memory_days` 全部废弃删除。
- **消费排除**（用户方案的点睛）：`memory_limit>0` 时窗口内 `consumed_run_id` 非空的记录**连同主明细一并整体剔除**（不是仅从历史小节隐藏——`_build_messages` 收到的就是过滤后的记录集，信息由摘要承继；避免重复总结、连贯性内生）；原 `overlap_note` 软提示删除（硬排除替代）；记录数/统计按排除后计算；全部被消费 → 走"无记录"路径。
- **展示**：`GET /api/summary/jobs/{name}/memory-stats` 返回 total_count/total_chars/avg_chars/memory_limit/related_limit/injected_estimate_tokens（估算口径：条数 × 平均字符 × 0.7 系数，明示估算）。**不做百分比**——API 不暴露模型上下文窗口元数据（/models 仅有 id/created/owned_by），占比无数据基础；文档注明。
- **注入 token 数学**：注入量 = (min(存量, memory_limit) + related_limit) × 平均 ~80 字 × 0.7 ≈ 常态可控；上限 1000 时为极端自担场景，stats 透明可见。
- **空记忆行为**：`get_recent` 返回 [] → 注入段为空 → 不拼接历史小节，正常生成总结——空转无害。

---

## 5. 手工端到端验收指南（Phase 2）

> 自动化单测已覆盖（3564 例）；以下是**真实环境**手工验收路径。配合只读自检脚本
> `scripts/memory_selfcheck.py`（不触发 LLM，输出验收所需全部事实，可反复执行）。

### 准备
1. config.ini `[llm]` 配好 provider（先 openai_compat 再 anthropic 各跑一轮）
2. 建 summary job，`memory_limit=5`（>0 即开启记忆特性；`related_limit=3` 可选）
3. 自检脚本打底：`uv run python scripts/memory_selfcheck.py` → 应全部 OK

### 场景 1｜写入 + 用量归属（W2 验证点）
1. 触发一次真实同步 or `POST /api/summary/jobs/{name}/trigger`
2. 跑自检脚本：§3 出现新记忆条目；§6 该 job_name 下出现两组记录（主调用+摘要调用）→ W2 闭环

### 场景 2｜第二次执行的注入
1. 再次 trigger
2. 通知文案出现延续性表述（如"接着上次…"）即通过；脚本 §3 显示条目数 +1（上次被引用）

### 场景 3｜关键词跨日召回（E2 验证点）
1. 第一天看过《芙莉莲》并 trigger（记忆开启）；第二天
2. `uv run python scripts/memory_selfcheck.py --titles 芙莉莲` → 命中昨日条目（联表反查演示；`--keywords` 已是 deprecated FTS 路径）

### 场景 4｜改名迁移 / 清空
1. Web UI 改 job 名 → 脚本 §7 的 task_id 已变新名
2. 清空记忆按钮（需二次确认）→ §3/§4 归零、§5 消费标记清空

### 场景 5｜Provider 回归（openai 旧功能不受影响）
| 步骤 | 预期 |
|---|---|
| openai_compat + gpt-4o-mini，thinking_level=off | 请求与改造前完全一致（无 reasoning_effort 字段）→ log 无 warning |
| openai_compat + gpt-4o-mini + thinking_level=high | warning "非 o 系列不支持 reasoning_effort"，请求成功 |
| openai_compat + o 系列模型 + high（如有权限） | 请求体含 `"reasoning_effort": "high"` |
| anthropic + claude + thinking_level=low | 正常返回，content 无思考文本混入 |
| anthropic + claude-haiku 系 + high | warning "不支持 extended thinking"，降级 off |
| 配置页"测试连接"按钮，两 provider 各一次 | 均成功 |

> 400 降级重试（方案 C）手工触发难（需端点真实拒绝参数）——由单测覆盖（`test_param_rejection_degrades_and_retries`）。

### 场景 6｜失败路径
- 故意配错 api_key → `summary_llm_failed` 站内信；记忆不写无效条目（脚本 §3 无新增）

### 场景 7｜prune 归档
- 构造 1005 条不现实——由单测覆盖（test_agent_memory prune 系列），手工跳过。

---

## 6. Phase 2.2 实施记录 + 双 provider 异同对照

### 实施内容（2026-08 完成）
- `OpenAICompatProvider.chat()` 拆分为 `_build_request` / `_parse_response` / `_to_wire_message`（与 Anthropic 侧对称）
- `thinking_level → reasoning_effort` 映射：off 不传字段（= 现状行为）；**仅 `^o\d` 命中的 o 系列模型生效**（正则由 `startswith("o")` 收紧，避免 `ollama/…`、`openrouter/…` 网关名误中）；非 o 系列 warning 忽略
- content 为 `list[ContentBlock]` 时取 text block 拼接（OpenAI wire 为字符串）
- `client._build_provider` 双 provider 统一传 thinking_level
- **方案 C（双重保险）**：`LLMClient.chat` 捕获参数类 400（响应文本命中 `unrecognized|unknown parameter|unexpected keyword|invalid request argument|extra fields not permitted`）→ 置位 `provider._extras_disabled` → 立即重试一次（不计入 MAX_RETRIES=2 退避）→ 实例生命周期内不再发送 thinking/reasoning 扩展字段。双 provider 通用（anthropic 的 thinking budget 被网关拒绝同样触发）
- anthropic haiku 降级判断由 `startswith` 改**包含匹配**（兼容 `anthropic/claude-haiku-…` 网关前缀）

### 能力/特性异同对照（当前代码事实）

| 维度 | AnthropicProvider | OpenAICompatProvider | 说明 |
|---|---|---|---|
| wire 端点 | `/v1/messages` | `/v1/chat/completions` | 各自官方规范 |
| 认证 | `x-api-key` + `Authorization: Bearer` 双发 | `Authorization: Bearer` | anthropic 双发兼容 OpenAI 风格网关 |
| system prompt | 抽顶层参数，多条 `\n\n` 合并 | 留在 messages 内原样转发 | 业务层避免多 system（openai 兼容端点风险） |
| content 形态 | blocks 数组（text/thinking/redacted/未知跳过告警） | 纯字符串；list 输入取 text 拼接 | 思考内容均不污染 ChatResponse.content |
| 思考能力 | 原生：budget_tokens 三档（2048/4096/8192）+ max_tokens 自动抬升 + temperature 强制 1 + haiku 降级 | `reasoning_effort` 三档映射，仅 `^o\d` 模型，非 o 忽略告警 | anthropic 侧重（budget 计入 max_tokens 约束） |
| 扩展参数降级 | ✅ 统一由 LLMClient 方案 C 兜底 | ✅ 同上 | `_extras_disabled` 标记位（BaseProvider） |
| stop_reason | 透传（end_turn/tool_use/max_tokens） | 未采集（finish_reason 丢弃） | P4 工具协议时 openai 侧补 |
| refusal 处理 | —（无此概念） | `message.refusal` → ValueError | openai 特有 |
| usage 映射 | input/output → prompt/completion | 同名字段直取 | 统一 Usage 模型 |
| 结构 | `_build_request`/`_parse_response`/`_to_wire_message` 对称 | 同左（本轮对齐） | P4 工具协议在对称结构上加 ToolUseBlock 双向转换 |

### 三方兼容模型与"按模型名限制"（决策记录）
- **论据来源声明**："OpenAI 对未知参数严格 400"（`Unrecognized request argument supplied: xxx`）来自训练语料中的 API 历史行为，**未能本机实时复现验证**——端点行为以实测为准，可用一次性 curl 验证：
  ```bash
  curl -s https://api.openai.com/v1/chat/completions -H "Authorization: Bearer $KEY" \
    -H 'Content-Type: application/json' \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"reasoning_effort":"high"}' | head -c 300
  ```
- **Anthropic 同样严格**：Messages API 对未知顶层字段返回 400 `invalid_request_error`。
- **第三方端点分类**（经验性归纳，版本相关，以实测为准）：Azure OpenAI 较严格；DeepSeek 官方忽略不支持参数（文档明示）；Ollama/vLLM/LiteLLM 普遍宽松忽略；OpenRouter 取决于下游；dashscope/moonshot 兼容模式多数忽略、个别报错。
- **"都传"的风险不是不生效，是硬失败**：定时任务 400 = 整次总结失败；且 thinking_level 是全局配置，用户换模型忘改即踩雷。
- **决策（方案 C 双保险）**：启发式收紧（`^o\d`）挡发送前 + 400 降级重试兜底 + 文档告知"端点应严格控制未知参数；客户端已具备降级重试"。
- **留待 Phase 4**：若自建网关需要显式声明能力，加 provider 配置 `reasoning_effort: auto|force_on|force_off`（逃生门，暂不实施）。

---

## 7. 验收自动化（对应 §5 场景）

| 层 | 方式 |
|---|---|
| 单元 | `tests/core/test_agent_memory.py`（W/C 系列）、`tests/services/memory/*`（检索/提取/服务）、`tests/services/llm/test_client.py`（降级重试）、`tests/services/summary/test_models.py`（0–1000 钳制） |
| 集成 | `tests/e2e/test_summary_memory.py`（前端表单渲染/提交）、`tests/api/test_summary.py` |
| 手工 | §5 指南 + `scripts/memory_selfcheck.py` |

---

## 8. 已知边界（docs 已同步）

- memory_limit / related_limit 有效区间 0–1000（0=关；前端/API/后台三层一致）
- 注入为前置拼接 `## 历史执行上下文` 小节，无需修改自定义 system_prompt（用户指令在后，优先级更高）
- 摘要条数 × 每条约 50–100 字（LLM 完整输出不截断） = 注入 token 线性成本；估算展示在 memory-stats
- **检索不依赖 FTS**（search_fts 停用 deprecated）：同剧关联走联表反查（SQLite 版本仅影响物理索引健康，无功能影响）
- 空记忆时正常执行，无空段落注入
- 冷层已参与 **related 联表反查**（跨窗口同剧回忆）；recent 注入仍只查热层

---

## 9. 遗留引用（后续 phase 提示）

- `agent-phase2.1-tools.md` → Phase 4 工具协议实施时打开
- `agent-phase2.3-feedback.md` → Phase 3.1 实施时打开（恢复清单在 `agent-phase3-summary-enhanced.md` §A）
- `agent-phase2.2-openai-refactor.md` → 已完成，本文档 §6 为实施记录

---

## 10. 当前进展总账（2026-08-27）

> 自 §4 E2/E3 v7 终稿后，完成两轮闭环：① v7 终稿落码（9 批，commit `93ed2b8`）；
> ② hy-260827 检视报告 24 条逐条判断 + 按 TDD 修复 12 项（commit `83f915c`）。
> 当前 3603 测试全绿，计划内 P0/P1 全部完成，P2 风格项审计不修（见下）。

### 10.1 已交付（本轮代码，全部带 BDD 用例测试）

| 能力 | 状态 | 验证 |
|---|---|---|
| `memory_limit`/`related_limit`（0=关，0–1000，三层校验一致） | ✅ 93ed2b8 | test_models 钳制 7 例 + e2e 表单 |
| 消费排除（已消费记录连同主明细剔除，信息由摘要承继） | ✅ | S2/S3 场景测试（全未消费/混合/全消费） |
| related_limit 独立生效（不受 memory_limit 门控） | ✅ 83f915c | M1 测试（mem=0 + rel=3 仍关联） |
| 同剧关联联表反查（主表+归档 UNION、GROUP BY、倒序、短路、隔离） | ✅ 93ed2b8 | 5 联表场景 + selfcheck --titles |
| 摘要零截断 + 失败跳过不写（prompt 软约束 50–100 字） | ✅ 93ed2b8 | S8/S9 测试 |
| memory-stats 端点（绝对量 + 估算，无百分比） | ✅ 93ed2b8 | S10'/S11' + M11 验证 |
| llm test 降延迟（max_tokens=8 + 固定文案） | ✅ 93ed2b8 | S12/S13 |
| FTS 停用（search_fts deprecated，物理保留 P5 复用） | ✅ 93ed2b8 | deprecated 标注 + selfcheck 提示 |

### 10.2 hy-260827 修复项（TDD：红灯→绿灯）

| 项 | 修复 | 测试 |
|---|---|---|
| H1/H1-API | 空内容判定仅 `not content`（双条件误判成功） | 缺陷场景测试 |
| H2 | 坏配置（lookback_days/max_records 非法值）回落默认，不再拖垮调度注册 | `abc`/`1.5` 用例 |
| H3 | o 系列发 reasoning_effort 时强制 temperature=1（对齐 Anthropic） | 3 用例 |
| H4/H4b | closeout 双 E3 矛盾清理（删除旧 1–50 残留段）；§7 测试引用修正 | 文档 |
| M1 | related 独立于 memory_limit 门控 | 1 用例 |
| M7 | refusal（ValueError）终态不重试 | 1 用例（1 次调用零退避） |
| M8 | 参数拒绝识别加 Anthropic 文案 + 放行 422 | 3 用例 |
| M9 | 终态 4xx 不重试；429 优先 Retry-After | 2 用例 |
| M10 | OpenAI finish_reason → stop_reason（对齐 Anthropic） | 3 用例 |
| M11 | stats 估算随 M1 修复自动正确（related 计入） | 1 用例 |
| L6/L8/L4/M4 | thinking 告警降 debug；latency 只计成功请求；魔数注释；find_overlaps deprecated | — |

### 10.3 审计判定为不修（§6 结论）

- **风格类**：L2（user_name 写法）、L3/L5（类型注解）、L7（timeout 双传）、L10（版本头常量）
- **设计取舍**：L9（fire-and-forget 日志）、M5（同步 DB I/O，留 Phase 3+ executor 化）
- M2 语义已文档化（剔除主明细）；M6 随 H3 一并缓解（兼容网关软忽略 max_tokens）

### 10.4 遗留待办（下一轮）

1. **Anthropic cache_* tokens 用量统计**（M10 ③）：llm_usage 的 total_tokens 对开启 prompt caching 的 Anthropic 偏低，影响成本口径
2. **M5 executor 化**（同步 DB 阻塞事件循环）——Phase 3 改造前置，当前体量可接受
3. **tests/e2e** 需在真实浏览器环境跑一轮（本机未执行，仅更新了选择器）
4. **memory-stats UI 落卡**：表单卡片展示累计条数/估算注入量（端点已就绪，前端展示待接）
5. Phase 3.0 启动：B1（D1 两段式形态）先裁决，见 `agent-phase3-4-decisions.md` §10
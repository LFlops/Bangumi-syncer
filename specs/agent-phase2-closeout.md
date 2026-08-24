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
│          ⚠️ 现状：只写不读（P4 之前无消费方；search_archive 留给 P4 Agent 全量查史）
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

### E2. FTS 索引不含 full_text → 摘要约束修召回 ✅ 已实施
- 背景：FTS 只索引 `(task_type, summary, outcome)`，`search_fts` 命中的是一行摘要而非全文——全文入索引体积代价大、边际收益低。
- **裁决**：约束摘要生成"必须列出本次涉及的全部番剧名"，并把摘要字数上限从 50 放宽至 100（重度用户 10 部番 × 日文名 5–8 字，50 字放不下；硬截断 200 兜底，20 部极端场景尾部截断可接受）。全文索引（及其他 RAG 收益）留给 Phase 5 语义层。
- 连带修正：D4 的收益表述已改为"结构化输出提升的是通知渲染与后续解析，非 FTS 召回"。

### E3. memory_limit 三层校验不一致 → 后端对齐（min 1 / max 50）✅ 已实施
- 背景（查证）：前端 `templates/config.html` 已有 `min="1" max="50"`（HTML 软约束）；API Pydantic 裸类型无约束；后台 `from_config_dict` 仅钳下限——手改 config.ini 或直调 API 传 1000 会真实生效。
- **裁决**：后端对齐前端的既有约定——Pydantic `Create/Update` 加 `ge=1, le=50`；`SummaryJobConfig.from_config_dict` 与 `SummaryJobResponse.from_config_dict` 补 `min(50, …)`。50 不是新限制，是把既成事实补到服务端。Response 模型不加约束（存量 config 兼容）。
- **下限语义**（为何 `max(1,…)`）：单一开关原则——开/关由 `memory_enabled` 表达，条数由 `memory_limit` 表达；允许 0 会出现两种"关闭"语义含糊。"没有记忆也可以"的需求已被 `memory_enabled=false`（不注入也不写入）覆盖。
- **上限成本**：每条约 50–100 字摘要，5 条约 150–300 token 注入；50 条约 1.5–3K token——docs 已注明线性成本。
- **空记忆行为**（沿调用链核实）：无记忆时 `get_recent` 返回 `[]` → `format_memory_context` 空串 → `if memory_context:` 为假 → 不拼接历史小节，正常生成总结——空转但无害，无空段落注入。

---

## 5. 手工端到端验收指南（Phase 2）

> 自动化单测已覆盖（3564 例）；以下是**真实环境**手工验收路径。配合只读自检脚本
> `scripts/memory_selfcheck.py`（不触发 LLM，输出验收所需全部事实，可反复执行）。

### 准备
1. config.ini `[llm]` 配好 provider（先 openai_compat 再 anthropic 各跑一轮）
2. 建 summary job，开 `memory_enabled=true`、`memory_limit=5`
3. 自检脚本打底：`uv run python scripts/memory_selfcheck.py` → 应全部 OK

### 场景 1｜写入 + 用量归属（W2 验证点）
1. 触发一次真实同步 or `POST /api/summary/jobs/{name}/trigger`
2. 跑自检脚本：§3 出现新记忆条目；§6 该 job_name 下出现两组记录（主调用+摘要调用）→ W2 闭环

### 场景 2｜第二次执行的注入
1. 再次 trigger
2. 通知文案出现延续性表述（如"接着上次…"）即通过；脚本 §3 显示条目数 +1（上次被引用）

### 场景 3｜关键词跨日召回（E2 验证点）
1. 第一天看过《芙莉莲》并 trigger；第二天
2. `uv run python scripts/memory_selfcheck.py --keywords 芙莉莲` → 命中昨日条目

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
| 单元 | `tests/core/test_agent_memory.py`（W/C 系列）、`tests/services/memory/*`（检索/提取/服务）、`tests/services/llm/test_client.py`（降级重试）、`tests/services/summary/test_models.py`（1–50 钳制） |
| 集成 | `tests/e2e/test_summary_memory.py`（前端表单渲染/提交）、`tests/api/test_summary.py` |
| 手工 | §5 指南 + `scripts/memory_selfcheck.py` |

---

## 8. 已知边界（docs 已同步）

- memory_limit 有效区间 1–50（前端/API/后台三层一致）
- 注入为前置拼接 `## 历史执行上下文` 小节，无需修改自定义 system_prompt（用户指令在后，优先级更高）
- 摘要条数 × 每条约 50–100 字 = 注入 token 线性成本
- FTS 关键词检索依赖 SQLite ≥3.34（trigram）；降级时中文子串检索受限且有 warning 日志
- 空记忆时正常执行，无空段落注入
- 冷层归档当前只写不读（P4 启用）

---

## 9. 遗留引用（后续 phase 提示）

- `agent-phase2.1-tools.md` → Phase 4 工具协议实施时打开
- `agent-phase2.3-feedback.md` → Phase 3.1 实施时打开（恢复清单在 `agent-phase3-summary-enhanced.md` §A）
- `agent-phase2.2-openai-refactor.md` → 已完成，本文档 §6 为实施记录
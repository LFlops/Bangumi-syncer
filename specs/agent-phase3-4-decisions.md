# Agent 化路径调整：总结增强（Phase 3）→ 通用 Agent（Phase 4）· 设计总览与决策清单

> 所属计划：Bangumi-Syncer Agent 化增量计划（原"三步"调整为"四步"）
> 性质：设计总览 + 决策清单（供逐条评审对齐，每条带确认状态）
> 关联：
> - `agent-phase2-memory.md` / 2.0.1–2.0.3（已完成，总结记忆闭环的既有基础）
> - `agent-phase2.1-tools.md` / 2.2（已完成，tool_use/tool_result 协议与 provider 对齐）
> - `agent-phase2.3-feedback.md`（**spec 已有、代码未实施**——排期裁决见 §10 A1，位置见 §1.1.1）
> - `agent-phase3-agent.md`（原 Phase 3 实施文档——**降级为 Phase 4 参考资料**，见 §1.2）
> - `agent-phase3-assistant.md`（不用 LangGraph ADR——**继续有效**）
> - `agent-framework-comparison.md`（框架调研——结论继续有效）

---

## 0. 背景与调整理由

原"三步增量计划"把 Phase 3 定义为"AI 能力对外溢出"，首个落地场景为**日志分析 Agent**（`agent-phase3-agent.md`）。

本次调整：**Phase 3 改为增强现有定时总结任务（summary），Phase 4 再落地通用 Agent 架构与问题诊断**。

调整理由（对现有链路 `app/services/summary/service.py` 调研后确认）：

| # | 理由 | 说明 |
|---|---|---|
| R1 | 总结链路仍是"单次调用" | `execute_job` 只有一次生成调用 + 一次摘要回调，增强空间大且**无需新范式即可见效** |
| R2 | 收益闭环最短 | 总结增强建立在 Phase 1–2 已完成的双 provider / 记忆基础上（反馈 2.3 尚未实施，已裁决排入 3.1，见 §10 A1），用户可立即感知效果 |
| R3 | 通用 Agent 需要信任基础 | 诊断 Agent 让用户接受的前提是 LLM 基础设施已被总结场景充分验证 |
| R4 | 避免一次跨度过大 | 原 Phase 3 打包 ~14 个新文件 + 4 个子系统，风险集中在单次交付 |

---

## 1. 总体路径（四步递进）

```
现状      ：单次调用   summarize（一个 LLM 调用生成总结 + 摘要回调）
Phase 3   ：多次调用   草稿 → 自检 → 精修（方法级拆分，无框架，总结场景验证）
Phase 4   ：循环化调用 think → act → observe + tool_registry + budget + trace + knowledge_base（诊断等场景）
Phase 5   ：知识沉淀闭环 / RAG 演进（按需评估，见 agent-phase3-agent.md §RAG）
```

每层关系：Phase 3 的方法级拆分与 Phase 4 的循环是**平行关系，不是嵌套关系**——循环不是线性管道的升级，两者是独立的编排范式；Phase 3 沉淀的薄约定（命名范式、数据访问、结构化解析）为 Phase 4 提供可消费的组件。

### 1.1 各 Phase 边界

| Phase | 目标 | 交付物 | 不做 |
|---|---|---|---|
| **Phase 3** | 总结质量升级 + 方法级编排约定 | 总结增强项（§2）+ 复用约定（§3）+ 交互契约定稿（§4） | 不做 Agent 循环、不做知识沉淀、不做日志分析 |
| **Phase 4** | 通用 Agent 架构 + 问题诊断 | Agent 循环 + 工具集 + budget/trace + knowledge_base + `/api/agent/*` | 不做前端交互细节（API 先行） |
| **Phase 5** | 知识沉淀闭环 / 语义检索 | feedback→知识库联动、sqlite-vec 混合检索（按需） | 不阻塞 3/4 |

### 1.1.1 Phase 3 内部拆分草案（对齐 Phase 2 的 2.0.x 模式）

| 子 phase | 内容 | 说明 |
|---|---|---|
| **3.0** | 设计定稿收尾 + J1/J2 约定落地（service 方法级拆分）+ 低风险增强项先行（D5 兑底 / D6 重试 / D7 status） | 无新交互依赖 |
| **3.1** | feedback 通道落地（原 2.3 实施：API + 通知条目"反馈"按钮 + 弹窗 + 注入强约束） | **A1 裁决位置**——需 Web 交互，不阻塞 3.0 |
| **3.2+** | 效果类增强项（D1 两段式 / D3 关键词 / D4 结构化 / D8 分解），可借助 3.1 的反馈验证效果 | 顺序随对齐细化 |

> 拆分原则同 Phase 2：一个子 phase 只做一件事；本表为草案，§10 对齐后固化到 `agent-phase3-summary-enhanced.md`。

### 1.2 原 spec 文件处置

| 文件 | 处置 |
|---|---|
| `agent-phase3-agent.md` | **降级为 Phase 4 参考资料**，内容不删除；其中总结增强相关部分由本文档承接 |
| `agent-phase3-assistant.md`（ADR） | 继续有效：Phase 4 自建 Agent 循环，不用 LangGraph |
| `agent-framework-comparison.md` | 继续有效：循环是已解决的问题，hard part 是工程外壳（上下文/安全/成本） |

---

## 2. Phase 3 范围：总结增强项（决策 D 系列）

> 现状基线：`execute_job` = 固定时间窗查询 → 规则关键词记忆注入（recent 5 + FTS5 命中）→ 单次生成 → 摘要回调（复用缓存前缀）→ 通知发送。

### 2.1 效果杠杆（Phase 3 主战场，SummaryService 内可做）

#### D1. 两段式生成：草稿 → 自检 → 精修
- **问题**：单次生成会遗漏记录、结构随意、未匹配条目不可见。
- **推荐**：`generate` 出粗稿 → `refine` 步骤让 LLM 对照 records 做结构化自检（输出 `missing / unmatched / duplicate` 三个清单）→ 带检查结果精修。
- **承载**：Phase 2.1 的 tool_result 协议 + Phase 2.2 的结构化输出能力。
- **状态**：□ 待确认

#### D2. 未匹配条目引导
- **问题**：`bgm_title` 为空的记录不特别标注，总结不知道哪些番剧没匹配成功。
- **推荐**：未匹配记录单独列表，总结中提示"以下条目未匹配，建议添加映射"，连接 mappings / pending_candidates 数据。
- **零 Agent**：纯数据丰富，无循环。
- **状态**：□ 待确认

#### D3. 关键词提取升级
- **问题**：`_build_memory_context` 的 keywords 为规则提取（今日 bgm_title 前 5 去重），无语义、跨主题联想弱，直接影响 FTS5 记忆检索命中率。
- **推荐**：LLM 提取关键词（复用餐缓前缀，与摘要调用合并成本）或"规则保底 + LLM 提语义"混合策略。
- **状态**：□ 待确认

#### D4. 结构化输出
- **问题**：总结为自由文本，full_text 无结构，FTS5 检索质量与后续渲染都受限。
- **推荐**：总结按模板输出（观感段 / 数据段 / 建议段），与 D1 的自检清单格式约束配套。
- **状态**：□ 待确认

### 2.2 信任杠杆

#### D5. 规则兜底总结
- **问题**：LLM 全部重试耗尽时，现状只发失败通知（`_dispatch_notification` 空响应分支）。
- **推荐**：LLM 失败时用模板拼统计（今日 N 条记录 / M 部番剧 / K 次失败）发基础总结，附"LLM 暂不可用"提示。
- **状态**：□ 待确认

#### D6. 差异重试
- **问题**：`LLMClient.MAX_RETRIES=2` + 固定退避 `[1, 3]`，未按错误类型差异化。
- **推荐**：429 / 5xx / 超时三类错误差异化退避（429 尊重 Retry-After；5xx 线性退避；超时快速重试一次）。
- **状态**：□ 待确认

### 2.3 数据角度

#### D7. status 意识
- **问题**：`_query_records` 只按时间窗取记录，不问 status。
- **推荐**：把"同步失败 / 重试中"记录纳入上下文，总结可提示"你有 N 条记录在重试队列"。
- **状态**：□ 待确认

#### D8. 多用户分解
- **问题**：multi-user 模式一次性混在一起生成，靠 prompt"按用户分开"自觉。
- **推荐**：先按 user_name 分片总结，再聚合（多用户场景质量稳定）。
- **状态**：□ 待确认

### 2.4 Phase 3 明确不做

- 完整 Agent 循环（think→act→observe）
- knowledge_base 知识沉淀
- 日志分析 / 问题诊断
- `/api/agent/*` 通用接口

---

## 3. 复用约定（决策 J 系列：Phase 3 建立，Phase 4 消费）

> 目的：避免 Phase 3 做成不可读的 summary 胶水。分两级：**J1/J2 为编码约定**（方法级拆分与命名、薄数据访问——不建框架，YAGNI：等 Phase 4 出现真实第二消费方再从需求里长抽象）；**J3/J4/J5 为真实组件或格式约定**（有明确消费方的才落地为独立模块）。

#### J1. 方法级拆分约定（不建框架）
- D1 两段式生成在 SummaryService 内拆为私有方法：`_generate_draft` / `_self_review` / `_refine`，沿用 Phase 2.0.2 拆分先例（`_query_records` / `_build_messages` / `_dispatch_notification`）。
- 命名范式约定 `gather_*` / `build_*` / `generate_*` / `refine_*` / `deliver_*`——为可读性与未来对齐，**不为未来建抽象**。
- **否决**：PipelineStep 框架 / 注册表 / 类型化 step 传递（只有 summary 一个消费方，线性步骤用方法级表达足够；Phase 4 循环是 while-loop，并非线性管道的升级）。
- **状态**：□ 待确认

#### J2. 数据访问薄约定（不建基类/注册表）
- 约定"数据源 = 带参数的只读查询函数"：总结的 records 查询、Phase 4 诊断的 errors/health 查询是同一种形态。
- **不新建基类/注册表**——Phase 4 工具直接调用查询函数即可；届时出现真实多数据源编排需求再抽象。
- **状态**：□ 待确认

#### J3. 结构化输出解析工具（真实组件，有第二消费方）
- LLM 返回 JSON / 标记文本的对齐与校验（容错、重试、失败降级）。
- 消费方：D1 自检清单解析（Phase 3）+ 诊断报告解析（Phase 4）——**唯一在设计期就有明确双消费方的组件**，作为独立模块落地。
- **状态**：□ 待确认

#### J4. 自检组件
- D1 的"检查→修正"步骤独立成组件，Phase 4 中扩展为 Agent 的评估能力。
- 落地形态：Phase 3 先并入 SummaryService 私有方法；出现第二消费方（Phase 4 评估步骤）再独立为模块。
- **状态**：□ 待确认

#### J5. 产物格式约定（ReportTemplate）
- 总结产物 / 诊断报告的 markdown 分节 + 元数据格式约定。
- **仅约定格式，不建渲染框架**；Phase 4 诊断报告套用同一格式。
- **状态**：□ 待确认

---

## 4. 交互契约（决策 C 系列：设计期定稿，拆到最后补会反改核心）

> 依据调研：Agent 类功能中仅以下 4 项"数据/功能契约"强依赖交互方式；其余（动态追踪渲染、确认 UI、管理页面）为纯前端，拆到最后做无影响（API 响应结构在 API 设计时定死即可）。

#### C1. trace / step 数据模型（schema 即交互契约）
- **推荐**：step 记录字段含：thinking 文本、tool_call（名称/入参/用时/状态）、tool_result（**截断摘要 + 原始全文双列**）、token 明细、失败原因。
- **理由**：UI 最后做可以，但 schema 最后改代价高（可视化要耗时/token，截断策略影响结果可读性）。
- **否决**：只存最终报告不存 steps（诊断不可信、不可复盘）。
- **状态**：□ 待确认

#### C2. 取消语义（功能依赖，非纯 UI）
- **推荐**：Agent 循环内置取消信号（协作取消 / cancellation token），API 提供 `POST /runs/{id}/cancel`，状态含 `cancelled`。
- **理由**：前端"停止按钮"是必须交互，若 loop 无取消钩子则最后补要改循环本体。
- **状态**：□ 待确认

#### C3. 写操作权限边界（安全先于 UI）
- **推荐**：工具分 read / write 两级注册，写工具（如 set_watched）独立审计；UI 展示写操作标识 + 触发前二次确认（对应用户体系）。
- **状态**：□ 待确认

#### C4. 传输方式（API 契约）
- **推荐**：API 层抽象"输出流"：`GET /runs/{id}`（快照）+ `GET /runs/{id}/events`（SSE 可选）。前端可轮询可流式，后端契约不绑定具体传输。
- **理由**：spec 现定 202+轮询；项目已有 SSE 先例（`sync-retry.js`）；契约层解耦则两者皆可。
- **状态**：□ 待确认

> 注：此 4 项与 Phase 3 无强依赖（Phase 3 不引入循环/工具），但建议在 Phase 3 开发期间一并定稿，Phase 4 实施时按契约实现。

---

## 5. Phase 4 范围边界（现在只设计接缝，功能设计留白）

| 现在定（接缝） | 留白（Phase 4 启动时设计） |
|---|---|
| task_type 命名空间：`summary` / `diagnostic` / … | 诊断工具清单（get_recent_errors 等） |
| 数据访问约定（J2） | knowledge_base schema 细节 |
| 产物模型 ReportTemplate（J5） | `/api/agent/*` 端点细节（接口方向见 C 系列） |
| 交互契约（C1–C4） | 诊断报告的知识沉淀确认流程实现 |

**原则**：Phase 3 结束前不写 Phase 4 的功能 spec；只保证抽象接缝存在且可扩展。

---

## 6. 决策清单（逐条评审用）

| 编号 | 决策 | 层级 | 依赖 | 状态 |
|---|---|---|---|---|
| D1 | 两段式生成（草稿→自检→精修） | Phase 3 | Phase 1/2.2 | □ |
| D2 | 未匹配条目引导 | Phase 3 | 现有数据 | □ |
| D3 | 关键词提取升级 | Phase 3 | Phase 2.0.2 | □ |
| D4 | 结构化输出 | Phase 3 | Phase 1 | □ |
| D5 | 规则兜底总结 | Phase 3 | 现有通知 | □ |
| D6 | 差异重试 | Phase 3 | LLMClient | □ |
| D7 | status 意识 | Phase 3 | sync_records | □ |
| D8 | 多用户分解 | Phase 3 | 现有查询 | □ |
| J1 | 方法级拆分约定（不建框架） | Phase 3 约定 / P4 平行 | D1 | □ |
| J2 | 数据访问薄约定（不建基类） | Phase 3 约定 / P4 消费 | — | □ |
| J3 | 结构化输出解析工具（组件） | Phase 3 建立 / P4 消费 | D1/D4 | □ |
| J4 | 自检组件（先并入 service） | Phase 3 建立 / P4 扩展 | D1 | □ |
| J5 | 产物格式约定 ReportTemplate | Phase 3 约定 / P4 消费 | D4 | □ |
| C1 | trace schema（耗时/token/截断双列） | Phase 4 实现 | — | □ |
| C2 | 取消语义（cancel hook + API） | Phase 4 实现 | — | □ |
| C3 | 工具 read/write 分级 + 审计 | Phase 4 实现 | — | □ |
| C4 | API 输出流抽象（快照+SSE） | Phase 4 实现 | — | □ |

评审规则（对齐 `agent-phase2-memory-review-decisions.md` 惯例）：每条默认按推荐方案；显式否决才改。

---

## 7. 风险

| 风险 | 缓解 |
|---|---|
| Phase 3 方法级拆分命名混乱 → Phase 4 难以阅读/对齐 | J1 命名约定列为 Phase 3 交付物，review 把关 |
| D1 两段式增加 LLM 调用次数 → 成本上升 | 复用缓存前缀（extractor 已示范）；预算上限纳入 D6 成本控制 |
| D3 关键词 LLM 化 → 抽取失败影响检索 | 规则保底混合策略，抽取失败回退规则 |
| C 系列定稿过早 → 与 Phase 4 实际不符 | C 系列仅定"契约方向"，实现细节留白 |
| 原 specs 文档与新规划并存 → 读者困惑 | §1.2 处置表 + 本文档顶部关联标记 |

---

## 8. 文件变更清单（草案，评审后细化）

| 操作 | 文件 | 说明 |
|---|---|---|
| 修改 | `app/services/summary/models.py` | `SummaryJobConfig` 增加增强项开关（如 `two_pass`、`structured_output`） |
| 修改 | `app/services/summary/service.py` | 方法级拆分（J1 命名约定）+ 增强项实现 |
| 修改 | `app/services/summary/scheduler.py` | 配置透传 |
| 新增 | `app/services/llm/output_parser.py` | J3 结构化输出解析（组件，诊断共用） |
| 新增 | `app/services/summary/reviewer.py` | J4 自检组件（消费方 <2 时先并入 service 私有方法） |
| — | J2 薄约定 | 不新增文件，体现在 service 查询方法命名 |
| 修改 | `app/services/memory/extractor.py` / `retriever.py` | D1 自检清单 / D3 关键词来源 |
| 新增 | `specs/agent-phase3-summary-enhanced.md` | Phase 3 实施细节（评审后拆分，参照 2.0.x 模式） |
| 新增 | `specs/agent-phase4-agent.md` | Phase 4 实施细节（Phase 4 启动时写，不在本次范围） |
| 修改 | `agent-phase3-agent.md` | 顶部标记"已降级为 Phase 4 参考资料" |

---

## 9. 验证方式

| 层级 | 方式 |
|---|---|
| 单元 | 既有 `tests/services/memory/*` `tests/services/summary/*` 扩展：D1 自检清单解析、D3 混合关键词、D6 重试分派 |
| 集成 | `test_summary_job` 预览端点验证拆分后各方法输出；规则兜底（D5）用 mock LLM 全失败路径 |
| 手工 | 配置真实 LLM，观察两段式输出的"自检清单→精修"差异；对比开关前后的总结质量 |
| Phase 4 预留 | C1 schema 单测（step 字段完整性、截断策略）；J3/J4 组件以 summary 为消费方验证，Phase 4 复用 |

---

## 10. 待定稿问题清单（自查产出，逐条对齐用）

> 产生背景：文档自查（2026-08）。分级：**A**=事实性错误（须改）/ **B**=设计矛盾·意图不明（须拍板）/ **C**=定义缺失（附推荐方案，确认后并入正文）/ **D**=可接受留白（备忘）。
> 裁决记录格式：`✅ 已裁决（结论）`；未裁决保持 `⏳`。条目定稿后并入正文对应章节。

### A 级 · 事实性问题

#### A1. R2 将"反馈"列为既有基础，但 Phase 2.3 尚未实现 ✅ 已裁决
- 问题：代码中 `outcome` 仅 success（`memory/models.py` 注释），`retriever.py` 留有"Phase 2.3 引入后…"挂钩——2.3 只有 spec 无实现，R2 原文却把"反馈"当作可用基础。
- **裁决（更新）：Phase 2 重新定义为仅含记忆三件套（2.0.1–2.0.3，已完成）**；feedback 落地排在 Phase 3.0 之后（即 §1.1.1 的 3.1）——需 Web 交互（通知条目"反馈"按钮 + 弹窗），不宜阻塞 3.0 核心交付；效果类增强项（D1/D3/D4）排其后可借助反馈验证效果。
- 同批裁决：原 2.1（工具协议）→ **Phase 4.x**（唯一消费者是 P4 工具注册表；D1 两段式走 JSON 文本 + J3 解析器，不依赖原生 tool calling）；原 2.2（openai 重构 + reasoning_effort）→ **不阻塞 Phase 2，已在本轮直接补齐实施**（见 §11.3）。
- 已执行：feedback 相关代码预留已从 Phase 2 移除，恢复清单见 `agent-phase3-summary-enhanced.md` §feedback。

#### A2. Phase 编号重定义 ✅ 已裁决

| 原编号 | 新归属 | 理由 |
|---|---|---|
| Phase 2 = 2.0.1+2.0.2+2.0.3 | **Phase 2 定稿**（记忆三件套，已完成） | "增加记忆"即 Phase 2 全部范围 |
| 2.1 工具协议 | **Phase 4.x** | 记忆链路纯文本 chat 零依赖；D1 自检走结构化 JSON 文本而非原生 tool calling；唯一消费者是 P4 工具注册表 |
| 2.2 openai 重构 | **本轮补齐完成**（provider 结构对称 + reasoning_effort） | 不阻塞记忆链路但属 provider 层欠账；实施记录与双 provider 异同对照见 §11.3 |
| 2.3 feedback | **Phase 3.1** | 见 A1；代码预留已移除，恢复清单在 phase3 文档 |

> 原 `specs/agent-phase2.1-tools.md` / `agent-phase2.2-openai-refactor.md` / `agent-phase2.3-feedback.md` 三份 spec 内容仍有效，仅执行时机调整；文件重命名随下轮 specs 整理一并处理。

### B 级 · 设计矛盾 / 意图不明 ⏳ 待逐条裁决

#### B1. D1 "两段式"实为三次 LLM 调用，且自检主体未定
- 业务背景（以单次总结为例）：cron 取到当日 12 条观看记录 → 现状一次 LLM 调用生成。已知翻车方式：**missing**（漏提个别记录）、**duplicate**（同番重复描述）、**unmatched**（`bgm_title` 为空即未匹配，总结不知情）。
- D1 原方案：草稿调用 → **LLM 自检调用**（对照草稿与原始记录，输出三类清单）→ 精修调用 = **3 次调用**，非"两段式"。
- 矛盾点：unmatched 规则可判（`bgm_title == ""`），交 LLM 属浪费；missing/duplicate 才需要 LLM 对照。
- 备选：(a) 规则预检 unmatched + LLM 只检 missing/duplicate（仍 3 次）；(b) **自检与精修合并**——第二次调用直接输出"修正终稿 + 附发现的问题清单"＝真两段式共 2 次；(c) 放弃 D1。
- 建议：(b)，配 J3 解析器容错。

#### B2. D3 关键词提取时机与成本说法矛盾
- keywords 用于**生成前**的记忆检索；"复用餐缓前缀与摘要调用合并"里的摘要是**生成后**的调用，两者无法共享同一次请求。
- 若 LLM 提取，链路最多 6 次串行调用（关键词→检索→草稿→自检→精修→摘要），首 token 延迟显著。
- 备选：(a) 维持规则提取；(b) **上一次 job 的摘要调用顺带产出下期关键词**（跨 job 复用，零新增调用，首次无历史回退规则）；(c) 接受一次前置调用。
- 建议：(b)。

#### B3. D4 结构化输出与用户自定义 system_prompt 冲突未定义
- `system_prompt` 用户可编辑（默认含"300 字以内"等格式指令）；结构化模板是系统强约束，冲突时优先级未定。
- 备选：(a) 模板指令追加在用户 prompt 之后并声明优先；(b) 开启结构化时忽略用户格式类指令；(c) 二者互斥（配置校验拒绝同开）。
- 建议：(a) + 配置说明注明。

#### B4. D5 兑底总结的通知语义未定
- 兑底属"降级的成功"：走成功渠道（webhook/email）还是失败通道（含站内信）？
- 建议：成功渠道正常发送 + 站内信 `inbox_type='summary_fallback'` 提示降级原因；notification_registry 新增类型。

#### B5. D6 差异重试的影响面未定
- `LLMClient` 为全局单例，改动影响 summary / extractor / 未来 agent 全部调用方。
- 建议：全局生效（重试属传输层而非业务层），退避参数保持常量、不做配置化（YAGNI）。

#### B6. J1 命名范式与方法名脱节
- 五前缀约定 `gather_* / build_* / generate_* / refine_* / deliver_*` 与 J1 自己列的方法 `_generate_draft / _self_review / _refine` 不对应（`_self_review` 不属于任何前缀）。
- 备选：(a) 方法改名对齐范式（如 `_review_draft`）；(b) 删五前缀约定，仅保留"沿用 2.0.2 拆分先例"。
- 建议：(b)——范式本身也是轻微过度设计，方法名达意即可。

#### B7. J4 与 §8 文件变更清单矛盾
- J4 正文"先并入 SummaryService 私有方法"；§8 却列了新增 `app/services/summary/reviewer.py`。
- 建议：以 J4 为准，§8 删 reviewer.py 行（出现第二消费方再立文件）。

### C 级 · 定义缺失（附推荐方案，确认后并入正文）⏳

#### C1. Phase 3 缺验收标准
- 现状各 D 项只有"推荐方案"，无"做到什么程度算成功"；§9 手工验证太弱。
- 建议补验收列：D1=构造测试集能捕获 ≥1 个真实遗漏案例；D5=mock LLM 全挂时通知仍含结构化统计；D8=双用户 fixture 下输出按用户分段。

#### C2. 配置开关与向后兼容未定义
- §8 只提了 `two_pass`、`structured_output` 两个键，完整开关清单/默认值/老 config.ini 行为均未定。
- 建议：单一总开关 `enhanced`（默认 false）+ 细分开关默认跟随总开关；`from_config_dict` 对缺失键回填默认，老配置零迁移。

#### C3. Phase 4 / Phase 5 的 knowledge_base 边界重叠
- §1.1 中 Phase 4 交付物含 knowledge_base，Phase 5 又有"知识沉淀闭环"。
- 建议：明确"Phase 4 建表 + 写入/查询工具（诊断自用），Phase 5 做 feedback→知识库联动与语义检索升级"。

#### C4. status 取值域未确认（D5/D7 共同依赖）
- "K 次失败""N 条重试中"依赖 `sync_records.status` 枚举，文档未确认现状取值。
- 建议：实施前查 schema 定死枚举映射后再写死文案。

### D 级 · 可接受留白（备忘，无需裁决）

| 项 | 说明 |
|---|---|
| C2 取消后语义 | 已耗 token 是否入账、cancelled run 能否重跑——Phase 4 设计时定 |
| C3 审计存储 | 写操作审计表设计留白 Phase 4 |
| C4 SSE 鉴权 | 沿用 sync-retry 的 cookie 方案，属实现细节 |
| D2 数据来源 | pending_candidates 查询入口现状实施时确认 |
| 原 phase3-agent.md 子设计沿用度 | budget/trace 等设计的沿用声明放到实施 spec 逐项处理 |
---

## 11. Phase 2 记忆实现 · 规范性审查（2026-08，按"Phase 2 = 记忆三件套"新定义验收）

### 11.1 已修复（本轮）

| # | 问题 | 修复 |
|---|---|---|
| W1 | `MemoryService` 构造签名带 `sync_records_repo` 但 `self._sync` 从未被读（死参数；消费标记联动实际在 repo 内部事务完成） | ✅ 构造收敛为单参（含 hy-review #7 注释） |
| W2 | extractor 摘要调用 `llm.chat()` 未带 job 标识 → 摘要 token 在 llm_usage 落入空 job_name 组，总结任务真实成本（主调用+摘要）统计口径缺一半 | ✅ `extract_and_store` 增加 `job_name` 参数透传至摘要调用，summary service 传 `job_config.name` |
| W3 | trigram 降级静默发生（SQLite <3.34 时中文子串检索失效无提示） | ✅ 降级分支补 warning 日志 |

**W3 答疑（SQLite 版本由什么决定）**：`sqlite3` 是 Python **标准库模块**，编译期链接运行环境的 libsqlite3 C 库——**uv.lock 锁不住它**（uv 只管 PyPI 包）。版本来源：本地 = 系统 Python / CLT 自带（本机 3.53.4）；Docker = 基础镜像的 apt 包（`python:3.x-slim` 基于 Debian bookworm 自带 ≥3.40，安全）；Alpine/老发行版可能 <3.34。因此降级分支是防御性的，现在有日志可查。

### 11.2 待明确（业务背景与权衡）

#### E1. "agent_" 命名超前占用
- **背景**：表名 `agent_working_memory`、repo 名 `AgentMemoryRepository` 出现时项目还没有 Agent——它们实际存的是"定时任务执行记忆"。Phase 4 真 Agent 将引入 agent_runs/agent_steps（运行追踪），届时"agent"前缀会同时表示两种东西：任务记忆 vs 运行轨迹。
- **权衡**：(a) 维持现名 + 文档注明语义边界——零迁移成本，记忆表语义上确实是"未来 Agent 的长期记忆层"，与 agent_runs 的"短期轨迹"可以构成层次而非冲突；(b) 迁移改名（task_memory）——一次 DROP/RENAME + 全链路改引用，收益仅是命名洁癖。
- **推荐 (a)**：在 Phase 4 spec 里明确三层语义——`agent_working_memory`（任务级长期记忆）/ `agent_runs+steps`（单次运行轨迹）/ `knowledge_base`(跨任务沉淀知识)，命名不撞车。

#### E2. FTS 索引列不含 full_text → D4 收益论断修正
- **背景**：触发器只索引 `(task_type, summary, outcome)`，`search_fts` 命中的是一行摘要（≤50 字），不是全文。两个后果：(a) 若历史摘要恰好没提某番剧名，今日关键词捞不回该条；(b) 文档 §2 D4 曾写"结构化输出提升 FTS 检索质量"——检索目标不是 full_text，**此收益不成立**。
- **权衡**：(a) 把 full_text 加入索引——召回最全，但索引体积 ×N 且摘要已含关键信息，边际收益低；(b) **约束摘要生成必须包含番剧名**（改 _SUMMARY_PROMPT："必须列出涉及的所有番剧名"）——零存储成本修召回缺口；(c) 维持现状接受漏召回。
- **推荐 (b)**，并同步修正 §2 D4 表述为"结构化输出提升的是通知渲染与后续解析，非 FTS 召回"。

#### E3. memory_limit 有下限无上限
- **背景**：`max(1, ...)` 只防 0/负数，配 1000 条会注入爆 token（每条约 50 字摘要 + 分隔，1000 条 ≈ 数万 token）。
- **权衡**：(a) 加硬上限（如 ≤20）并在 from_config_dict 钳制——防呆但限制高级用户；(b) 上限放宽（如 ≤50）+ 文档注明成本自担；(c) 不设限信任用户。
- **推荐 (b)**：memory_limit 本意是"最近 N 次连续性"，超过 20 已无连续性意义；钳到 ≤50 防呆即可。

### 11.3 Phase 2.2 实施记录（本轮完成）+ 双 provider 异同对照

实施内容：`OpenAICompatProvider.chat()` 拆分为 `_build_request` / `_parse_response`（与 Anthropic 侧对称）；新增 `thinking_level → reasoning_effort` 映射（off 不传字段=现状行为；仅模型名以 o 开头生效，其余 warning 忽略）；content 为 list[ContentBlock] 时取 text block 拼接防御；`client._build_provider` 双 provider 统一传 thinking_level。测试：`test_provider_openai_compat.py` 新增 TestBuildRequest/TestParseResponse 共 7 例；旧断言"openai 不受 thinking_level 影响"更新为新语义。

#### 能力/特性异同对照（当前代码事实）

| 维度 | AnthropicProvider | OpenAICompatProvider | 说明 |
|---|---|---|---|
| wire 端点 | `/v1/messages` | `/v1/chat/completions` | 各自官方规范 |
| 认证 | `x-api-key` + `Authorization: Bearer` 双发 | `Authorization: Bearer` | anthropic 双发兼容 OpenAI 风格网关 |
| system prompt | 抽顶层参数，多条 `\n\n` 合并 | 留在 messages 内原样转发 | 业务层避免多 system（openai 兼容端点风险） |
| content 形态 | blocks 数组（text/thinking/redacted/未知跳过告警） | 纯字符串；list 输入取 text 拼接 | 思考内容均不污染 ChatResponse.content |
| 思考能力 | 原生：budget_tokens 三档映射 + max_tokens 自动抬升 + temperature 强制 1 + haiku 降级 | reasoning_effort 四档映射，仅 o 系列，非 o 忽略告警 | anthropic 侧更重（budget 计入 max_tokens 约束处理） |
| stop_reason | 透传（end_turn/tool_use/max_tokens） | 未采集（finish_reason 丢弃） | P4 工具协议需要时 openai 侧补 |
| refusal 处理 | —（无此概念） | message.refusal → ValueError | openai 特有 |
| usage 映射 | input/output → prompt/completion | 同名字段直取 | 统一 Usage 模型 |
| 结构 | `_build_request`/`_parse_response`/`_to_wire_message` 对称 | 同左（本轮对齐） | Phase 4 工具协议可在对称结构上加 ToolUseBlock 双向转换 |

> 遗留差异（P4 前不必处理）：openai 侧 finish_reason 未采集；tool_use block 在 anthropic 侧仍是"跳过+warning"（正式解析在 P4.x 工具协议 phase）。

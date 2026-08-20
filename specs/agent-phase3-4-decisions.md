# Agent 化路径调整：总结增强（Phase 3）→ 通用 Agent（Phase 4）· 设计总览与决策清单

> 所属计划：Bangumi-Syncer Agent 化增量计划（原"三步"调整为"四步"）
> 性质：设计总览 + 决策清单（供逐条评审对齐，每条带确认状态）
> 关联：
> - `agent-phase2-memory.md` / 2.0.1–2.0.3（已完成，总结记忆闭环的既有基础）
> - `agent-phase2.1-tools.md` / 2.2（已完成，tool_use/tool_result 协议与 provider 对齐）
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
| R2 | 收益闭环最短 | 总结增强建立在 Phase 1–2 已完成的双 provider / 记忆 / 反馈基础上，用户可立即感知效果 |
| R3 | 通用 Agent 需要信任基础 | 诊断 Agent 让用户接受的前提是 LLM 基础设施已被总结场景充分验证 |
| R4 | 避免一次跨度过大 | 原 Phase 3 打包 ~14 个新文件 + 4 个子系统，风险集中在单次交付 |

---

## 1. 总体路径（四步递进）

```
现状      ：单次调用   summarize（一个 LLM 调用生成总结 + 摘要回调）
Phase 3   ：管道化调用 gather → compose → generate → refine → deliver（总结场景验证）
Phase 4   ：循环化调用 think → act → observe + tool_registry + budget + trace + knowledge_base（诊断等场景）
Phase 5   ：知识沉淀闭环 / RAG 演进（按需评估，见 agent-phase3-agent.md §RAG）
```

每层复用上一层：管道化是循环化的"单步执行器"，DataProvider 是工具的"数据源"。

### 1.1 各 Phase 边界

| Phase | 目标 | 交付物 | 不做 |
|---|---|---|---|
| **Phase 3** | 总结质量升级 + 沉淀管道抽象 | 总结增强项（§2）+ 架构接缝（§3）+ 交互契约定稿（§4） | 不做 Agent 循环、不做知识沉淀、不做日志分析 |
| **Phase 4** | 通用 Agent 架构 + 问题诊断 | Agent 循环 + 工具集 + budget/trace + knowledge_base + `/api/agent/*` | 不做前端交互细节（API 先行） |
| **Phase 5** | 知识沉淀闭环 / 语义检索 | feedback→知识库联动、sqlite-vec 混合检索（按需） | 不阻塞 3/4 |

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

## 3. 架构接缝（决策 J 系列：Phase 3 建立，Phase 4 复用）

> 目的：避免 Phase 3 做成 summary 胶水、Phase 4 重构 342 行单体 service。以下抽象在 Phase 3 顺带建立，**接口现在定，Phase 4 填消费方**。

#### J1. PipelineStep 抽象
- gather / compose / generate / refine / deliver 五步管道，step 间以类型化数据传递。
- Phase 4 时 Agent 的"单步执行器"复用本抽象。
- **状态**：□ 待确认

#### J2. DataProvider 抽象
- 统一"带参数的只读数据源"接口：记录查询、队列状态、映射统计。
- 总结用 records provider；Phase 4 诊断用 errors/health provider，**接口一致**。
- **状态**：□ 待确认

#### J3. 结构化输出解析工具
- LLM 返回 JSON / 标记文本的对齐与校验（容错、重试、失败降级）。
- Phase 4 的诊断报告、自检清单共用。
- **状态**：□ 待确认

#### J4. 自检组件
- D1 的"检查→修正"步骤独立成组件，Phase 4 中扩展为 Agent 的评估能力。
- **状态**：□ 待确认

#### J5. 产物模型（ReportTemplate）
- 总结产物 / 诊断报告的通用渲染结构（markdown 分节 + 元数据）。
- Phase 4 诊断报告直接套用。
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
| DataProvider 接口（J2） | knowledge_base schema 细节 |
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
| J1 | PipelineStep 抽象 | Phase 3 建立 / P4 复用 | — | □ |
| J2 | DataProvider 抽象 | Phase 3 建立 / P4 复用 | — | □ |
| J3 | 结构化输出解析工具 | Phase 3 建立 / P4 复用 | D1/D4 | □ |
| J4 | 自检组件 | Phase 3 建立 / P4 复用 | D1 | □ |
| J5 | 产物模型 ReportTemplate | Phase 3 建立 / P4 复用 | D4 | □ |
| C1 | trace schema（耗时/token/截断双列） | Phase 4 实现 | — | □ |
| C2 | 取消语义（cancel hook + API） | Phase 4 实现 | — | □ |
| C3 | 工具 read/write 分级 + 审计 | Phase 4 实现 | — | □ |
| C4 | API 输出流抽象（快照+SSE） | Phase 4 实现 | — | □ |

评审规则（对齐 `agent-phase2-memory-review-decisions.md` 惯例）：每条默认按推荐方案；显式否决才改。

---

## 7. 风险

| 风险 | 缓解 |
|---|---|
| Phase 3 只做增强、不沉淀 J 系列抽象 → Phase 4 重构代价大 | J 系列列为 Phase 3 交付物，非可选项 |
| D1 两段式增加 LLM 调用次数 → 成本上升 | 复用缓存前缀（extractor 已示范）；预算上限纳入 D6 成本控制 |
| D3 关键词 LLM 化 → 抽取失败影响检索 | 规则保底混合策略，抽取失败回退规则 |
| C 系列定稿过早 → 与 Phase 4 实际不符 | C 系列仅定"契约方向"，实现细节留白 |
| 原 specs 文档与新规划并存 → 读者困惑 | §1.2 处置表 + 本文档顶部关联标记 |

---

## 8. 文件变更清单（草案，评审后细化）

| 操作 | 文件 | 说明 |
|---|---|---|
| 修改 | `app/services/summary/models.py` | `SummaryJobConfig` 增加增强项开关（如 `two_pass`、`structured_output`） |
| 修改 | `app/services/summary/service.py` | 管道化重构 + 增强项实现 |
| 修改 | `app/services/summary/scheduler.py` | 配置透传 |
| 新增 | `app/services/summary/pipeline.py` 或 `app/services/pipeline/` | J1 管道抽象 |
| 新增 | `app/services/pipeline/providers.py` 或同类 | J2 DataProvider |
| 修改 | `app/services/memory/extractor.py` / `retriever.py` | D1 自检清单 / D3 关键词来源 |
| 新增 | `specs/agent-phase3-summary-enhanced.md` | Phase 3 实施细节（评审后拆分，参照 2.0.x 模式） |
| 新增 | `specs/agent-phase4-agent.md` | Phase 4 实施细节（Phase 4 启动时写，不在本次范围） |
| 修改 | `agent-phase3-agent.md` | 顶部标记"已降级为 Phase 4 参考资料" |

---

## 9. 验证方式

| 层级 | 方式 |
|---|---|
| 单元 | 既有 `tests/services/memory/*` `tests/services/summary/*` 扩展：D1 自检清单解析、D3 混合关键词、D6 重试分派 |
| 集成 | `test_summary_job` 预览端点验证管道 step 输出；规则兜底（D5）用 mock LLM 全失败路径 |
| 手工 | 配置真实 LLM，观察两段式输出的"自检清单→精修"差异；对比开关前后的总结质量 |
| Phase 4 预留 | C1 schema 单测（step 字段完整性、截断策略）；J1/J2 抽象以 summary 为第一个实现方做多态验证 |
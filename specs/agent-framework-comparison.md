# Agent 框架调研对比：Claude Code / WorkBuddy / LangGraph vs 本项目

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 性质：架构调研记录（2026-08 查证）
> 关联：`agent-phase3-assistant.md`（不用 LangGraph 的 ADR）、`agent-phase3-agent.md`（Phase 3 实施）

## 调研问题

主流 Agent 工具（Claude Code、WorkBuddy）是否使用 LangGraph？架构差异？与当前项目相比的特点？

## 一、Claude Code（Anthropic）

**核心架构**（源码泄露后多方拆解确认）：

- **核心是 `while(true)` ReAct 循环**（`queryLoop()` async generator）——**没有状态图、没有任务队列、没有预设管线**
- 关键数据：**仅 ~1.6% 代码是 AI 决策逻辑，98.4% 是"操作外壳"**——权限系统、上下文压缩、工具执行、安全守卫
- Subagent **递归调用同一个 `query()` 函数**（不是独立工作流）——主循环能力（压缩/恢复/流式工具执行）自动适用于子代理
- **消息不可变性**（不改 API 返回消息）→ prompt 缓存命中率提升、长会话成本降 ~80%
- 5 阶段上下文压缩管线（预算削减/截断/微压缩/上下文折叠/自动压缩）管理 ~200K 上下文
- deny-first 权限系统（7 种模式 + 独立 ML 分类器评估工具安全）

**2026 新增**：
- `/goals`：内置评估器——独立评估模型（默认 Haiku）检查用户定义的完成条件，解决 agent"过早宣称完成"
- Ultracode：动态工作流（模型自写编排代码、fan-out 至 1000 subagents + 对抗评审）

## 二、WorkBuddy（腾讯云企业 Agent 平台）

**架构**："模型 + Harness"——**Harness 是自研基础设施**，不用 LangGraph：

- **9 维框架**（开源 workbuddy-harness）：身份层 / 记忆层 / 技能层 / 学习层 / 调度层 / 融合层 / 安全层 / 评估层（+ 引擎运行时）
- 5 层逻辑架构：基础设施（混合云+本地双模）→ Agent 底座（NLU/任务规划/工具调用/执行监控）→ 能力服务（RAG/知识图谱/评估/安全沙箱）→ 业务应用（CodeBuddy 研发 / WorkBuddy 办公）→ 交互层
- **ReAct 循环**（judge → act → observe）+ 上下文工程（五动作：写入/选择/更新/移除/压缩）
- 工具四层：感知（Read/Glob/Grep/WebSearch）→ 行动（Write/Edit/Bash）→ 协调（TaskCreate/Agent/Skill）→ 沟通（AskUserQuestion，认知心理学设计：最多 4 问 × 4 选项）
- 工具接入层类似 **MCP 模式**（可插拔工具与数据源）
- 记忆系统：memory-decay（指数衰减）/ memory-git / memory-graph（关系图、智能去重）
- 多模型路由（按任务类型选模型平衡成本/延迟/质量）；并行 subagent；7×24 数字员工（异步沙箱 + checkpoint 快速复制）
- 评估框架：30 基准用例、A/B 对比、回归检测

## 三、LangGraph

- **Directed Cyclic Graph**（State / Nodes / Edges）——控制流**编译时确定**（图拓扑固定）
- **Killer feature：checkpoint**——每个节点持久化、中断/恢复、time-travel、并行分支、human-in-the-loop
- 适合：**可恢复的生产工作流**（durable/resumable/parallel）
- 不适合：简单工具循环（overkill）

## 四、业界共识（2026 多方分析）

> **所有主流框架（Claude Code / LangGraph / OpenAI Agents SDK / Vercel AI SDK / smolagents）收敛到同一个 while-loop 核心**——真正差异在工程外壳（上下文管理、安全、成本控制）。

- agent loop 本身"是已解决的问题"；hard part 是可靠性：上下文管理（工具响应可占 ~67.6% token）、压缩、迭代上限、循环检测、成本预算、错误分类
- LangGraph 的图模型在需要 durable/resumable/parallel 时才有价值

## 五、与当前项目的对比

| 维度 | Claude Code / WorkBuddy | 本项目 Phase 3 |
|---|---|---|
| 循环 | 自研 ReAct while 循环 | **一致**（自研，已定稿，见 agent-phase3-assistant.md） |
| 工具系统 | 通用（文件/终端/办公/感知/协调/沟通） | **领域特定**（Bangumi 同步/日志诊断/通知的业务服务包装） |
| 交互模式 | **交互式**（用户在场：Claude 权限系统、WorkBuddy AskUserQuestion） | **后台无人值守**（定时任务）——预算模型（ThinkingBudget）取代用户批准 |
| Subagent | Claude 递归 query() / WorkBuddy 并行 subagent | delegate 工具模式（一致） |
| 记忆 | WorkBuddy memory-decay/graph；Claude CLAUDE.md | agent_working_memory 短/中/长期分层（更轻但结构相似） |
| 追踪/评估 | LangSmith 云 / WorkBuddy Eval 框架 / Claude /goals | TraceRecorder → SQLite（自部署）+ BDD/LLM-as-judge 规划 |
| 上下文工程 | Claude 5 阶段压缩管线 | 未到该规模（memory_limit 控制注入量先行） |
| Prompt 缓存 | Claude 消息不可变 → 缓存命中（成本降 ~80%） | `_summarize` 复用总结上下文命中缓存（呼应实践） |

**结论**：业界主流实践**证实本项目的架构判断**——自研循环 + 工程外壳是标准路径。本项目特点：
1. **领域特定**：工具集是业务服务包装，非通用文件/终端
2. **后台无人值守**：预算（ThinkingBudget）取代用户批准——与 WorkBuddy"7×24 数字员工"场景同源但轻量
3. **自部署**：追踪/记忆全本地（SQLite），不依赖云（LangSmith）生态

## Sources

- [Claude Code 动态工作流拆解（腾讯云）](https://cloud.tencent.com.cn/developer/article/2685109)
- [Dive-into-Claude-Code 架构分析（GitHub）](https://github.com/VILA-Lab/Dive-into-Claude-Code/blob/main/docs/architecture.md)
- [Claude Code 1000 Subagents 分析（Towards AI）](https://pub.towardsai.net/claude-code-now-spawns-1-000-subagents-and-it-quietly-killed-my-langgraph-stack-e73a4a1c1912)
- [Claude Code /goals 评估器（VentureBeat）](https://venturebeat.com/orchestration/claude-codes-goals-separates-the-agent-that-works-from-the-one-that-decides-its-done)
- [WorkBuddy Harness 九维框架（GitHub）](https://github.com/zhuang-HE/workbuddy-harness)
- [WorkBuddy 核心架构拆解](https://cloud.tencent.cn/developer/article/2651866)
- [WorkBuddy Enterprise 平台架构](https://cloud.tencent.com.cn/developer/article/2685449)
- [WorkBuddy 工具系统与 AskUserQuestion 设计哲学](https://cloud.tencent.com.cn/developer/article/2703129)
- [WorkBuddy Harness 工程复盘](https://hub-assets-cache.baai.ac.cn/view/56311)

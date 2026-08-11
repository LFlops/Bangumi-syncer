# Phase 3 架构决策记录：Agent 循环不用 LangGraph，自建实现

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 性质：架构决策记录（ADR）——记录"为什么不用框架"的理由，供 Phase 3 实施与后续评审参考
> 关联：`agent-phase3-agent.md`（Agent 化实施文档）

## 决策背景

Phase 3 需要 Agent 循环（think → act → observe）、共享状态、轨迹记录、SubAgent 等能力。评估候选：LangGraph（提供 StateGraph 图执行、State 共享状态、LangSmith 轨迹、Subgraph、Checkpointer），或自建循环（loop/budget/trace/tool_registry/sub_agent 模块，见 phase3-agent.md）。

关键问题：自建是否属于"重复造轮子"？复杂任务场景（后续 MCP 支持）是否应提前引入框架？

## 决策

**不用 LangGraph，自建 Agent 循环。** 已设计的模块（loop.py / budget.py / trace.py / tool_registry.py / sub_agent.py）继续按 `agent-phase3-agent.md` 实施。

## 理由

### 1. 决定性因素：Phase 1 内部中立模型与 langchain-core 冲突

- Phase 1 已实现内部中立模型：`Message` / `ContentBlock`（text/thinking/tool_use/tool_result）、`ThinkingLevel`、双 provider 适配（`BaseProvider` + `_PROVIDER_MAP`）——这是已落地的资产
- LangGraph 底层依赖 **langchain-core** 的消息体系（`AIMessage`/`ToolMessage`/`SystemMessage`）与工具抽象（`BaseTool`）——与本项目模型**不兼容**
- 引入 LangGraph 的两种后果：
  - 推翻 Phase 1 成果换 langchain 模型——本项目定制的 thinking 解析、未知 block 容错、ThinkingLevel 预算等需求 langchain 模型不原生支持
  - 或加**双模型转换层**（内部 ↔ langchain 持续适配）——长期维护成本
- 两者都是长期负债，单这一条已足够否决

### 2. LangGraph 的多 provider 机制与我们的抽象重复

- LangGraph 本身不关心 provider——依赖 langchain 模型抽象层（`ChatOpenAI` / `ChatAnthropic` / `init_chat_model` 按模型名路由）
- 我们的 `BaseProvider` + `_PROVIDER_MAP`（`app/services/llm/client.py`）是**同一角色的抽象层**——已自建，引入 LangGraph 即重复建设

### 3. LangGraph 能力与需求对照（全部不匹配）

| LangGraph 能力 | 本项目需求 | 结论 |
|---|---|---|
| StateGraph 图执行 | 线性 think → act → observe 循环 | ✗ 图的价值在复杂 DAG（分支/嵌套），线性循环用不上 |
| State 共享状态 | `AgentRun` dataclass（单进程内存） | ✗ 简单数据不需要框架抽象 |
| LangSmith 轨迹 | 自部署 + `TraceRecorder` → SQLite | ✗ LangSmith 是云生态，自部署不匹配 |
| Checkpointer 恢复 | 定时任务无长任务恢复需求 | ✗ YAGNI |
| Subgraph | `delegate_to_xxx` 工具模式（subagent 封装为工具） | ✗ 委派模式更简单 |
| 模型/工具集成 | 无缝消费 Phase 1 的 Message/ContentBlock | ✗ 致命——双模型冲突 |

### 4. 复杂任务场景（MCP 后）不需要框架——关键洞察

- **复杂来自"工具多样 + LLM 动态决策"，不是"图拓扑"**。通用 agent 的"图"是**运行时动态**的——LLM 每次选工具 = 动态边
- LangGraph 的 StateGraph 要求**预定义节点/边拓扑**——对通用 agent 反而是约束（边是无限的，无法预枚举）
- MCP 支持加的是**工具种类**（文件系统、数据库等）——自建循环天然支持（多一个 `ToolDefinition`）
- LangGraph 的优势场景是**预定义拓扑的固定工作流**（"先检索 → 再生成 → 再验证"流水线）——那是"工作流"不是"agent"；真出现此需求，在自建循环上加轻量工作流层即可，不需要整个框架

### 5. LangGraph 的坏处

- **依赖重量**：langgraph + langchain-core 引入整个抽象层——与本项目"零新增依赖、httpx 直连"的风格冲突
- **抽象不匹配**：预算控制（ThinkingBudget）、追踪（AgentTrace）、工具注册（ToolDefinition）都是定制需求——框架内实现要靠回调/钩子适配，框架的抽象边界未必匹配
- **版本风险**：langgraph 0.x → 1.x 有破坏性变更，API 演进快
- **调试**：框架层堆栈深，循环出错时定位难
- **自部署生态**：核心开源但周边（LangSmith）是云服务

### 6. "重复造轮子"的判断标准

**"轮子能直接装"才叫造轮子。** LangGraph 的轮子需要：
- 适配层（模型转换）——持续成本
- 定制钩子（预算/追踪）——框架边界约束
- 自建持久化（不用 LangSmith）——框架价值减半

改造后成本 **>** 自建 ~200 行线性循环 + 已设计模块（budget/trace/tool_registry/sub_agent）。且 Phase 1 已把协议层做好（tool_use/tool_result content blocks），循环层只是消费它们——自建是完全贴合的选择。

## 自建方案模块（见 phase3-agent.md）

```
app/services/agent/
├── loop.py           # Agent 循环（think → act → observe，线性）
├── budget.py         # ThinkingBudget：迭代/token/时间三重预算
├── trace.py          # TraceRecorder：步骤追踪 → SQLite（自部署）
├── tool_registry.py  # ToolDefinition 注册 + 执行
├── sub_agent.py      # delegate_to_xxx 工具模式（subagent 封装为工具）
└── tools/            # bangumi / sync / system / memory 工具
```

## 何时重新评估

1. 出现**预定义拓扑的固定工作流**需求（多阶段流水线）——在自建循环加轻量工作流层（YAGNI 原则，真出现再做）
2. 需要 **checkpoint 恢复超长任务**（多日断点续跑）——评估自建状态持久化即可
3. 多 agent 协作拓扑复杂化到无法用 delegate 工具表达——届时再评估框架，而非提前引入

## 结论

**不是"等复杂任务场景再引入 LangGraph"，而是复杂任务场景（MCP/多工具/多步）恰好是自建循环最擅长的**——框架的显式图是为确定性工作流设计的，与 agent 的动态本质错配。当前决策：自建。

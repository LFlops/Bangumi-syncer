---
title: 🤖 Agent 运行时与场景协议
order: 6
---

# 🤖 Agent 运行时与场景协议

LLM 匹配增强（以及未来的其它 Agent 场景）基于一层**通用运行时**：
`app/services/agent/` 承载与业务无关的 run 编排、恢复续跑状态机与观测设施；
具体业务（工具、种子、落库、通知）以「场景」形式接入。本文档给出分层、
`ScenarioHooks` 协议、扩展新场景的步骤与关键约束。

## 分层总览

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 循环 | `agent/loop.py` | 通用 LLM 循环（工具调用、终止工具、预算注入） |
| 观测 | `agent/trace.py`、`agent/recorder.py` | span 读写与 `TraceRecorder`（chat/tool span、seed 行、budget 钩子、全轮 tokens 累计） |
| 预算 | `agent/budget.py` | 轮次预算策略（`task_type` + `thinking_level` → `max_iterations`） |
| 运行时 | `agent/runtime.py` | `run` 编排与 `continue_run` 恢复状态机（原子抢占 / 异常分流 / replay 分派 / 补执行 / 终局分派） |
| 场景协议 | `agent/scenario.py` | `ScenarioHooks`：场景差异点契约 |
| 注册表 | `agent/registry.py` | `get_scenario(task_type)`：调度器等通用组件按 task_type 取得场景运行入口 |
| 场景实现 | `matching/llm_assist.py` | 匹配场景：工具注册 / seed 构建 / 条目校验 / 候选落库 / 通知 / 业务键 |

## ScenarioHooks 协议（`agent/scenario.py`）

| 字段 | 说明 |
| --- | --- |
| `task_type` | 任务类型（观测/日志前缀用，如 `"match"`） |
| `terminal_tool` | 终止工具名（循环末轮强制指向它；恢复分派据此识别终局） |
| `register_tools(registry, ctx)` | 注册场景工具，返回 `ToolDefinition` 列表 |
| `build_seed(ctx)` | 构建种子消息（system + user） |
| `build_chat_fn(thinking_level)` | 构建默认 chat 函数（场景决定 job 归属与思考强度透传） |
| `resolve_thinking_level()` | 从场景集中配置读取思考强度（恢复路径使用） |
| `resolve_max_iterations(thinking_level)` | 解析轮次预算（含显式覆盖；单一来源由场景保证） |
| `handle_terminal(dbm, run_id, result, ctx, *, total_tokens, notification_service)` | 终局处理：校验 / 落库 / 通知，返回终态 status |

`ctx` 为场景上下文（runtime **原样透传、不感知其结构**）；匹配场景使用
`_MatchContext(sync_record, bgm)`。

## 运行流程

### run（正常路径）

1. 原子抢占 run（`atomic_claim`，失败返回 `"skipped"`）；
2. per-run `ToolRegistry` + 场景工具注册（并发隔离，不写模块单例）；
3. `build_seed` → 写 seed span；`resolve_max_iterations` 解析预算；
4. `loop.py` 编排（chat span / tool span / 预算消息由 recorder 记录）；
5. LLM 异常分流：`LLMCallError(retryable=False)` → `failed(llm_error)`；
   可重试 → `increment_attempts`（≥3 由仓储单点转 failed）；
6. 终局交 `handle_terminal`。

### continue_run（恢复路径，唯一公开续跑入口）

1. 场景解析 thinking_level / max_iterations；
2. `trace.replay` 从 `agent_steps` 重建消息与终局响应；预算耗尽 → `no_suggestion(exhausted)`；
3. 按 `last_response` 分派：
   - `None`（轮次已完整记录）→ 续跑 loop；
   - `end_turn` → `mark_no_suggestion`；
   - `terminal_tool` → 场景 `handle_terminal`（tokens 取 replay 累计）；
   - 含 tool_use → 补执行缺失只读工具（写 tool span 自包含）→ 续跑 loop；
4. 异常在函数内部消化（不向调度器抛出）。

## 新增一个 Agent 场景

1. 实现 `ScenarioHooks` 的 8 个钩子（工具 / seed / chat / 预算 / 终局 等）；
2. 提供工厂 `get_scenario_runtime() -> ScenarioRuntime`（`agent/registry.py` 中的类型）；
3. 在 `agent/registry.py::_SCENARIO_PROVIDERS` 登记
   `task_type -> (场景模块路径, 工厂函数名)`（惰性导入，通用层不反向依赖场景）；
4. 调度/触发入口按 `get_scenario(task_type).run / .continue_run` 调用，
   不 import 场景模块内部。

## 约束与约定

- **恢复续跑收敛为单一入口 `continue_run`**：调度器只调用公开入口，不触碰场景
  私有符号（有源码级守卫测试）。
- **调度器不依赖场景模块**：经 `agent.registry.get_scenario(task_type)` 获取
  `ScenarioRuntime`（组合模式）；新增任务类型时调度骨架可复用。
- **场景钩子实现内部以场景模块全局名引用依赖**（而非闭包硬绑定），保持可
  mock / patch / 替换（既有测试依赖该约定）。
- **失败语义**（run / continue 一致）：确定性 LLM 失败 → 立即 `failed`；
  可重试失败 → 累计 attempts；其它异常 → 日志 + attempts。
- 运行时日志前缀由 `hooks.task_type` 派生（如 `[match]`）。
- 事务 / 落库 / 通知属场景职责；runtime 只透传 `ctx` 与 `notification_service`。

## 相关文件与测试

| 主题 | 位置 |
| --- | --- |
| 运行时 / 场景协议 / 注册表 | `app/services/agent/runtime.py`、`scenario.py`、`registry.py` |
| 匹配场景实现 | `app/services/matching/llm_assist.py` |
| 场景测试 | `tests/services/matching/test_llm_assist.py` |
| 调度器与守卫 | `app/services/llm_match_scheduler.py`、`tests/services/test_llm_match_scheduler.py` |
| 待办（终局策略拆分等） | `remain/agent_phase3_deferred_design.md`（本地归档，不入库） |

> 历史：`llm_assist.py` 曾混居通用状态机与匹配业务（1583 行）；commit `2cda3bb`
> 将 `TraceRecorder` 迁入 `agent/recorder.py`，`5110e46` 抽取 `agent/runtime.py`
> 与 `ScenarioHooks` 协议，场景层收缩为「匹配业务 + 薄封装」。

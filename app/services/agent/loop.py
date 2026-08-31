"""轻量 Agent 循环（spec §3.2.4）。

通用骨架核心：固定 ``max_iterations`` 上限的 for 循环，每轮经注入的 ``chat_fn`` 调 LLM，
按 spec 伪代码处理终止、聚合、分段并行、透明预算与 stop_reason。

设计要点：
- **不直接依赖 LLMClient**：LLM 调用经注入的 ``chat_fn(messages, tools=, tool_choice=)``，
  便于测试 mock 与场景层（llm_assist）注入真实客户端。
- **工具执行经注入的 ``tool_calls_fn``**：循环把整批 tool_calls 一次性交给执行器
  （``tool_registry.execute_batch`` 做分段并行 gather/串行），按**独立结果槽位**逐条回填
  tool_result（重复 tool_use_id 时首个保留真实结果、后续为 duplicate 错误块，F10/M26）。
- **终止工具优先**：本轮含 ``tool_choice_terminal`` 工具 → 捕获参数即 break，同轮其他工具不执行。
- **透明预算**：每轮追加 ``[剩余轮次：N]``；末轮（remaining==1 起手）强制 ``tool_choice=terminal``（I-4）。
- **span 钩子**：每轮 chat 前后调用 ``span_recorder.start_span/end_span``（name=llm_chat），
  并为该轮**每个**工具执行创建 ``tool_execute`` span（start 于 llm_chat span 之后、sequence=工具序号、
  parent=当前 llm_chat span_id → 执行 → end 写入 ``replay_delta={tool_result: {...}}`` + payload 摘要，
  满足 spec D16/M19：每轮 LLM + 每次工具各一条 span）。``record_budget_message`` 并入**同轮最后一个**
  ``tool_execute`` span 的 replay_delta（I-2）；若该轮无工具则回退 llm_chat span（保留可重放性）。
  ``span_recorder=None`` 时整体跳过（可空实现）。

不引入 Phase 4 的 token/wall-time 预算系统，只做轮次上限（spec §3.2.4 末段）。
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable

from app.services.llm.models import (
    ChatResponse,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)


@dataclass
class RunResult:
    """一轮 Agent 会话的终态结果。

    - ``stop_reason``：end_turn / submit_suggestion / exhausted（D14 统一结束原因）
    - ``text``：end_turn 时 LLM 的纯文本输出
    - ``suggestion``：submit_suggestion 捕获的参数 dict（subject_id/reason）
    - ``last_response``：末轮 ChatResponse（exhausted 兜底供 output_parser 解析）
    """

    stop_reason: str
    text: str = ""
    suggestion: dict | None = None
    last_response: ChatResponse | None = None


# 注入的函数类型（仅做文档化提示，运行时不强制）
ChatFn = Callable[..., Awaitable[ChatResponse]]
ToolCallsFn = Callable[[list[ToolUseBlock]], Awaitable[dict[str, Any]]]


def _extract_tool_calls(resp: ChatResponse) -> list[ToolUseBlock]:
    """从 ChatResponse.blocks 中提取全部 ToolUseBlock（同轮工具调用顺序固定）。"""
    return [b for b in resp.blocks if isinstance(b, ToolUseBlock)]


def _input_summary(inp: dict) -> str:
    """输入摘要：仅记录参数名与类型（不记录参数值，spec G-4）。"""
    return ", ".join(f"{k}:{type(v).__name__}" for k, v in (inp or {}).items())


def _align_results(tool_calls: list[ToolUseBlock], results: Any) -> list[Any]:
    """把执行器返回值对齐为与 ``tool_calls`` 一一对应的结果槽位列表。

    - 执行器返回 ``BatchResults``（带 ``ordered`` 槽位）→ 按槽位取，重复 tool_use_id
      的每条 tool_use 各取自身结果（首个真实结果不被 duplicate 错误块覆盖，F10/M26）
    - 普通 ``dict[tool_use_id, result]``（旧契约 / 注入的简易执行器）→ 回退按 id 取值
    """
    ordered = getattr(results, "ordered", None)
    if isinstance(ordered, list) and len(ordered) == len(tool_calls):
        if all(oid == tc.id for (oid, _), tc in zip(ordered, tool_calls)):
            return [result for _, result in ordered]
    getter = getattr(results, "get", None)
    if getter is None:
        return [None] * len(tool_calls)
    return [getter(tc.id) for tc in tool_calls]


async def run(
    *,
    chat_fn: ChatFn,
    tools_schemas: list[dict],
    tool_calls_fn: ToolCallsFn,
    max_iterations: int,
    tool_choice_terminal: str,
    seed_messages: list[Message],
    span_recorder: Any | None = None,
) -> RunResult:
    """运行轻量 Agent 循环，返回 ``RunResult``。

    参数（除 spec 约定的 seed_messages 外，全部经注入解耦，零 LLMClient 依赖）：
    - ``chat_fn``：``(messages, tools=, tool_choice=) -> ChatResponse`` 的异步可调用对象
    - ``tools_schemas``：传给 provider 的 tools 参数（schema 列表）
    - ``tool_calls_fn``：批量执行器，返回 ``BatchResults``（``ordered`` 与 tool_calls 一一对应的
      结果槽位；同时兼容 ``{tool_use_id: ToolResultBlock | TerminalCapture}`` 的 dict 视图）。
      普通 dict 亦可（按 id 取值，重复 id 场景无法区分槽位）
    - ``max_iterations``：轮次上限（由 budget 策略计算后传入，循环无感知映射来源）
    - ``tool_choice_terminal``：终止性工具名（submit_suggestion）
    - ``seed_messages``：调用方构建的种子消息（system + user）
    - ``span_recorder``：可选 span 记录器，None 时跳过钩子
    """
    messages: list[Message] = list(seed_messages)
    remaining = max_iterations

    for iteration in range(max_iterations):
        # I-4：末轮（remaining==1 起手）强制 terminal 收尾；其余轮不指定 tool_choice
        tool_choice = tool_choice_terminal if remaining == 1 else None

        # span：chat 前 start_span
        span_id = (
            span_recorder.start_span(name="llm_chat", iteration=iteration, sequence=0)
            if span_recorder is not None
            else None
        )
        resp: ChatResponse | None = None
        try:
            resp = await chat_fn(messages, tools=tools_schemas, tool_choice=tool_choice)
        finally:
            if span_recorder is not None:
                span_recorder.end_span(span_id, status="ok", response=resp)

        # ① end_turn → 终止（M6）
        if resp.stop_reason == "end_turn":
            return RunResult(
                stop_reason="end_turn", text=resp.content, last_response=resp
            )

        # ② 空响应 / 无 tool_calls 兜底终止（避免裸 success 卡死）
        tool_calls = _extract_tool_calls(resp)
        if not tool_calls:
            return RunResult(
                stop_reason="end_turn", text=resp.content, last_response=resp
            )

        # ③ 先将本轮全部 tool_use blocks 聚合为【一条】assistant 消息追加（F1 修正）
        messages.append(
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(id=tc.id, name=tc.name, input=tc.input)
                    for tc in tool_calls
                ],
            )
        )

        # ④ 终止工具优先：含 tool_choice_terminal → 捕获即 break（其他工具不执行）
        terminal_tc = next(
            (tc for tc in tool_calls if tc.name == tool_choice_terminal), None
        )
        if terminal_tc is not None:
            return RunResult(
                stop_reason="submit_suggestion",
                suggestion=terminal_tc.input,
                last_response=resp,
            )

        # ⑤ 分段并行执行（循环把整批交给 tool_calls_fn，由 execute_batch 内部 gather/串行）
        results = await tool_calls_fn(tool_calls)

        # ⑥ 逐条：创建 tool_execute span（spec D16/M19）→ 追加 tool_result
        #    span start 在拿到结果后（started_at 有微小误差，可接受），但语义完整：
        #    span 存在 + replay_delta 含完整 tool_result，供断点重放（trace.replay）重建。
        tool_execute_span_ids: list[str] = []
        aligned = _align_results(tool_calls, results)
        for seq, (tc, result) in enumerate(zip(tool_calls, aligned)):
            if result is None or not isinstance(result, ToolResultBlock):
                # 防御：缺失结果或非 ToolResultBlock（如极少数 TerminalCapture 泄漏）跳过
                continue
            if span_recorder is not None:
                te_span_id = span_recorder.start_span(
                    name="tool_execute",
                    iteration=iteration,
                    sequence=seq,
                    parent_id=span_id,
                )
                span_recorder.end_span(
                    te_span_id,
                    status="ok",
                    tool_name=tc.name,
                    input_summary=_input_summary(tc.input),
                    payload_json={
                        "tool_name": tc.name,
                        "tool_use_id": result.tool_use_id,
                        "is_error": result.is_error,
                    },
                    replay_delta={
                        "tool_result": {
                            "tool_use_id": result.tool_use_id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                    },
                )
                tool_execute_span_ids.append(te_span_id)
            messages.append(Message(role="user", content=[result]))

        # ⑦ 透明预算：递减并注入剩余轮次；预算消息并入**同轮最后一个 tool_execute**
        #    span 的 replay_delta（I-2）。若该轮无工具（防御），回退 llm_chat span 以保持可重放。
        remaining -= 1
        budget_message = f"[剩余轮次：{remaining}]"
        messages.append(Message(role="user", content=budget_message))
        if span_recorder is not None and span_id is not None:
            budget_target = (
                tool_execute_span_ids[-1] if tool_execute_span_ids else span_id
            )
            span_recorder.record_budget_message(budget_target, budget_message)

    # 预算刚性耗尽（兜底由场景层 output_parser 解析 last_response）
    return RunResult(stop_reason="exhausted", last_response=resp)

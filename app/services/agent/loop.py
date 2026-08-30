"""轻量 Agent 循环（spec §3.2.4）。

通用骨架核心：固定 ``max_iterations`` 上限的 for 循环，每轮经注入的 ``chat_fn`` 调 LLM，
按 spec 伪代码处理终止、聚合、分段并行、透明预算与 stop_reason。

设计要点：
- **不直接依赖 LLMClient**：LLM 调用经注入的 ``chat_fn(messages, tools=, tool_choice=)``，
  便于测试 mock 与场景层（llm_assist）注入真实客户端。
- **工具执行经注入的 ``tool_calls_fn``**：循环把整批 tool_calls 一次性交给执行器
  （``tool_registry.execute_batch`` 做分段并行 gather/串行），按原始顺序回填 tool_result。
- **终止工具优先**：本轮含 ``tool_choice_terminal`` 工具 → 捕获参数即 break，同轮其他工具不执行。
- **透明预算**：每轮追加 ``[剩余轮次：N]``；末轮（remaining==1 起手）强制 ``tool_choice=terminal``（I-4）。
- **span 钩子**：每轮 chat 前后调用 ``span_recorder.start_span/end_span``，并 ``record_budget_message``；
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
    - ``tool_calls_fn``：``(tool_calls) -> {tool_use_id: ToolResultBlock | TerminalCapture}`` 批量执行器
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

        # ⑥ 按原始顺序逐条追加 tool_result（每条携带对应 tool_use_id）
        for tc in tool_calls:
            result = results.get(tc.id)
            if result is None or not isinstance(result, ToolResultBlock):
                # 防御：缺失结果或非 ToolResultBlock（如极少数 TerminalCapture 泄漏）跳过
                continue
            messages.append(Message(role="user", content=[result]))

        # ⑦ 透明预算：递减并注入剩余轮次；span 记录并入本轮回话
        remaining -= 1
        budget_message = f"[剩余轮次：{remaining}]"
        messages.append(Message(role="user", content=budget_message))
        if span_recorder is not None and span_id is not None:
            span_recorder.record_budget_message(span_id, budget_message)

    # 预算刚性耗尽（兜底由场景层 output_parser 解析 last_response）
    return RunResult(stop_reason="exhausted", last_response=resp)

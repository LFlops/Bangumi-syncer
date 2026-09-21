"""轻量 Agent 循环。

通用骨架核心：固定 ``max_iterations`` 上限的 for 循环，每轮经注入的 ``chat_fn`` 调 LLM，
按既定流程处理终止、聚合、分段并行、透明预算与 stop_reason。

设计要点：
- **不直接依赖 LLMClient**：LLM 调用经注入的 ``chat_fn(messages, tools=, tool_choice=)``，
  便于测试 mock 与场景层（llm_assist）注入真实客户端。
- **工具执行经注入的 ``tool_calls_fn``**：循环把整批 tool_calls 一次性交给执行器
  （``tool_registry.execute_batch`` 做分段并行 gather/串行），按**独立结果槽位**逐条回填
  tool_result（重复 tool_use_id 时首个保留真实结果、后续为 duplicate 错误块）。
- **终止工具优先**：本轮含 ``tool_choice_terminal`` 工具 → 捕获参数即 break，同轮其他工具不执行。
- **透明预算**：每轮递减后仅当仍有后续轮次（remaining>0）才追加预算消息；
  remaining>1 为朴素 ``[剩余轮次：N]``，remaining==1 为强化文案
  ``FINAL_ROUND_BUDGET_MESSAGE``（明确要求给出结论、禁止再检索）。末轮（remaining==1 起手）
  强制 ``tool_choice=terminal``。末轮不再产生剩余 0 的幻影预算消息。
- **兜底收尾**：for 循环自然结束（即将返回 exhausted）时追加**一次**收尾 LLM 调用
  （``FINAL_RECOVERY_MESSAGE``，tools 仅 terminal schema、tool_choice=terminal），
  best-effort 争取明确结论；不执行任何非终止工具，异常时降级为 exhausted，仅调用一次。
- **预算钩子**：非末轮预算消息与收尾提示生成后调用 ``recorder.record_budget(...)``，
  由 recorder 内部决定并入哪条 span（同轮最后 tool_execute 或回退 llm_chat）。
  ``recorder=None`` 时整体跳过（可空实现）。

不引入 token/wall-time 预算系统，只做轮次上限。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.services.llm.models import (
    ChatResponse,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)

# 末轮（remaining==1）强化提示：思考模型在强制 tool_choice=terminal 被供应商降级为
# auto 时容易不提交，明确要求给出结论（含放弃）并禁止再检索。
FINAL_ROUND_BUDGET_MESSAGE = (
    "[剩余轮次：1（最后一轮）] 必须调用 submit_suggestion 给出结论："
    "若已确定推荐条目则提交 subject_id 与理由；若确实无法确定，也要调用并说明放弃理由。"
    "不得再调用其他检索工具。"
)

# 兜底收尾：for 循环自然结束（即将 exhausted）时追加一次收尾调用，best-effort
# 争取一个明确结论；该消息须经 recorder 预算通道记录，保证恢复重放一致性。
FINAL_RECOVERY_MESSAGE = (
    "[最终收尾] 轮次预算已耗尽。请立即调用 submit_suggestion 给出结论："
    "若已确定则提交 subject_id 与理由；若确实无法确定也请调用并说明放弃理由。"
)


@dataclass
class RunResult:
    """一轮 Agent 会话的终态结果。

    - ``stop_reason``：end_turn / submit_suggestion / exhausted（统一结束原因）
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


def _align_results(tool_calls: list[ToolUseBlock], results: Any) -> list[Any]:
    """把执行器返回值对齐为与 ``tool_calls`` 一一对应的结果槽位列表。

    - 执行器返回 ``BatchResults``（带 ``ordered`` 槽位）→ 按槽位取，重复 tool_use_id
      的每条 tool_use 各取自身结果（首个真实结果不被 duplicate 错误块覆盖）
    - 普通 ``dict[tool_use_id, result]``（旧契约 / 注入的简易执行器）→ 回退按 id 取值
    """
    ordered = getattr(results, "ordered", None)
    if isinstance(ordered, list) and len(ordered) == len(tool_calls):
        if all(oid == tc.id for (oid, _), tc in zip(ordered, tool_calls, strict=True)):
            return [result for _, result in ordered]
        logger.debug("ordered 槽位与 tool_calls 不一致，回退 dict 取值")
    getter = getattr(results, "get", None)
    if getter is None:
        return [None] * len(tool_calls)
    return [getter(tc.id) for tc in tool_calls]


def _inject_veto_hint(
    messages: list[Message], terminal_tc: ToolUseBlock, hint: str
) -> None:
    """veto 暂缓：注入与 terminal tool_use 配对的 tool_result（闭合会话协议）。

    OpenAI/Anthropic 协议要求 assistant 的 tool_use 后必须跟配对 tool_result，
    不能只注入 user 文本。
    """
    messages.append(
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=terminal_tc.id, content=hint, is_error=False
                )
            ],
        )
    )


def _record_veto_tool(
    recorder: Any | None,
    terminal_tc: ToolUseBlock,
    hint: str,
    *,
    sequence: int,
) -> None:
    """把 veto 伪执行落 tool_execute span，保证 replay 重建的消息与 live 一致。

    ``sequence`` 与 ``execute_batch`` 的约定一致：工具在 ``tool_calls`` 中的下标。
    ``recorder`` 为 None 时跳过（可空实现）。
    """
    if recorder is None:
        return
    span_id = recorder.start_tool(terminal_tc, sequence=sequence)
    if span_id is None:
        # 记录器选择不落 span（如可空实现）：仍需可观测，避免静默
        logger.debug(
            "veto terminal 工具 %s 未获得 span_id，跳过 replay 落 span",
            terminal_tc.name,
        )
        return
    recorder.end_tool(
        span_id,
        result=ToolResultBlock(
            tool_use_id=terminal_tc.id, content=hint, is_error=False
        ),
    )


async def run(
    *,
    chat_fn: ChatFn,
    tools_schemas: list[dict],
    tool_calls_fn: ToolCallsFn,
    max_iterations: int,
    tool_choice_terminal: str,
    seed_messages: list[Message],
    recorder: Any | None = None,
    veto_terminal: Callable[[dict], str | None] | None = None,
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
    - ``recorder``：可选预算记录器（鸭子类型 ``record_budget(str)``），None 时跳过钩子
    - ``veto_terminal``：可选终止提交软护栏。非 None 时，命中终止工具的轮次会先询问
      该回调（入参=terminal input）；返回提示文案则**不终止**、注入配对 tool_result 后
      继续一轮（仅拦一次，且末轮不拦）。返回 None / 回调为 None 时行为与现状一致。
    """
    messages: list[Message] = list(seed_messages)
    remaining = max_iterations
    resp: ChatResponse | None = None
    vetoed = False

    for _iteration in range(max_iterations):
        # 末轮（remaining==1 起手）强制 terminal 收尾；其余轮不指定 tool_choice
        tool_choice = tool_choice_terminal if remaining == 1 else None

        resp = await chat_fn(messages, tools=tools_schemas, tool_choice=tool_choice)

        # ① end_turn → 终止
        if resp.stop_reason == "end_turn":
            return RunResult(
                stop_reason="end_turn", text=resp.content, last_response=resp
            )

        # ② 无 tool_calls → 按 stop_reason 细分终止原因
        tool_calls = _extract_tool_calls(resp)
        if not tool_calls:
            if resp.stop_reason == "max_tokens":
                # 生成长度超限（非故障，但属特殊终态）
                return RunResult(
                    stop_reason="max_tokens", text=resp.content, last_response=resp
                )
            if resp.stop_reason == "" and not resp.blocks and not resp.content:
                # 空壳响应（LLM 调用失败后被错误传递到 loop，或 provider 异常）
                # → 显式标记 llm_error，不再伪装 end_turn
                return RunResult(stop_reason="llm_error", last_response=resp)
            # 其余（stop_sequence / stop_reason 未知但有 content 等）维持 end_turn 兼容
            return RunResult(
                stop_reason="end_turn", text=resp.content, last_response=resp
            )

        # ③ 先将本轮全部 tool_use blocks 聚合为【一条】assistant 消息追加（F1 修正）。
        # 同时保留响应中的非工具块（Text/Thinking）：思考模型的 thinking 块必须随
        # tool_use 一并回传（Anthropic/DeepSeek 约束，否则真实端点 400：
        # "content[].thinking ... must be passed back"）；OpenAI 兼容层在 provider
        # 侧按各自协议处理（thinking 块跳过）。
        assistant_blocks: list = list(resp.blocks) or [
            ToolUseBlock(id=tc.id, name=tc.name, input=tc.input) for tc in tool_calls
        ]
        messages.append(Message(role="assistant", content=assistant_blocks))

        # ④ 终止工具优先：含 tool_choice_terminal → 捕获即 break（其他工具不执行）
        terminal_tc: ToolUseBlock | None = None
        terminal_idx = -1
        for idx, tc in enumerate(tool_calls):
            if tc.name == tool_choice_terminal:
                terminal_tc = tc
                terminal_idx = idx
                break
        if terminal_tc is not None:
            # 软护栏（veto）：非末轮 + 本 run 尚未拦过时，先询问一次场景回调。
            # 末轮强收尾优先（remaining==1 不拦）；只拦一次避免无限暂缓。
            hint = ""
            if veto_terminal is not None and remaining > 1 and not vetoed:
                hint = veto_terminal(terminal_tc.input or {}) or ""
            if not hint:
                return RunResult(
                    stop_reason="submit_suggestion",
                    suggestion=terminal_tc.input,
                    last_response=resp,
                )
            # 暂缓：不终止、不执行其他工具，注入配对 tool_result 后消耗一轮预算继续。
            vetoed = True
            _inject_veto_hint(messages, terminal_tc, hint)
            _record_veto_tool(recorder, terminal_tc, hint, sequence=terminal_idx)
            remaining -= 1
            continue

        # ⑤ 分段并行执行（循环把整批交给 tool_calls_fn，由 execute_batch 内部 gather/串行）
        results = await tool_calls_fn(tool_calls)

        # ⑥ 逐条：追加 tool_result
        aligned = _align_results(tool_calls, results)
        for tc, result in zip(tool_calls, aligned, strict=True):
            if result is None or not isinstance(result, ToolResultBlock):
                # 防御：缺失结果或非 ToolResultBlock（如极少数 TerminalCapture 泄漏）跳过
                logger.warning(
                    "tool result 缺失或非 ToolResultBlock（tool=%s, type=%s），跳过",
                    tc.name,
                    type(result).__name__,
                )
                continue
            messages.append(Message(role="user", content=[result]))

        # ⑦ 透明预算：递减后仅当仍有后续轮次（remaining>0）才注入剩余轮次。
        #    末轮递减到 0 时循环随即结束，若仍构造预算消息会写入 trace 参与 replay，
        #    但该消息从未发给 LLM —— 形成「幻影预算消息」，故不注入也不记录。
        remaining -= 1
        if remaining > 0:
            budget_message = (
                FINAL_ROUND_BUDGET_MESSAGE
                if remaining == 1
                else f"[剩余轮次：{remaining}]"
            )
            messages.append(Message(role="user", content=budget_message))
            if recorder is not None:
                recorder.record_budget(budget_message)

    # 预算刚性耗尽：追加一次收尾 LLM 调用（best-effort，仅一次，不执行非终止工具），
    # 尽量在返回 exhausted 前拿到明确结论。
    return await _final_recovery(
        chat_fn=chat_fn,
        tools_schemas=tools_schemas,
        tool_choice_terminal=tool_choice_terminal,
        messages=messages,
        recorder=recorder,
        last_response=resp,
    )


async def _final_recovery(
    *,
    chat_fn: ChatFn,
    tools_schemas: list[dict],
    tool_choice_terminal: str,
    messages: list[Message],
    recorder: Any | None,
    last_response: ChatResponse | None,
) -> RunResult:
    """耗尽后的兜底收尾调用（最多一次）。

    - 追加 ``FINAL_RECOVERY_MESSAGE`` 并经 recorder 预算通道记录（重放一致性）
    - tools 过滤为**仅** terminal schema，``tool_choice=terminal``
    - 含 terminal tool_call → submit_suggestion；否则 exhausted（last_response=收尾响应）
    - chat_fn 抛异常 → best-effort 捕获返回 exhausted（保留循环内最后一次响应），不重试
    """
    messages.append(Message(role="user", content=FINAL_RECOVERY_MESSAGE))
    if recorder is not None:
        recorder.record_budget(FINAL_RECOVERY_MESSAGE)

    terminal_schemas = [
        s
        for s in tools_schemas
        if isinstance(s, dict) and s.get("name") == tool_choice_terminal
    ]
    try:
        recovery_resp = await chat_fn(
            messages, tools=terminal_schemas, tool_choice=tool_choice_terminal
        )
    except Exception as e:
        # best-effort：收尾失败不应让 run 更糟，保留循环内最后一次可用响应
        logger.warning("收尾调用失败（best-effort 降级为 exhausted）: %s", e)
        return RunResult(stop_reason="exhausted", last_response=last_response)

    terminal_tc = next(
        (
            tc
            for tc in _extract_tool_calls(recovery_resp)
            if tc.name == tool_choice_terminal
        ),
        None,
    )
    if terminal_tc is not None:
        return RunResult(
            stop_reason="submit_suggestion",
            suggestion=terminal_tc.input,
            last_response=recovery_resp,
        )
    return RunResult(stop_reason="exhausted", last_response=recovery_resp)

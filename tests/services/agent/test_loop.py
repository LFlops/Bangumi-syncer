"""TDD 红阶段：轻量循环 ``app/services/agent/loop.py``（场景 M6/M7/M26/M28 + 分段并行 + 终止 + 预算）。

覆盖（spec §3.2.4）：
- end_turn / 空响应（无 tool_calls）→ stop_reason=end_turn（M6）
- assistant 聚合消息先于 tool_result（协议顺序，F1 修正）
- submit_suggestion 捕获即 break（stop_reason=submit_suggestion），同轮其他工具不执行（M7 终止工具优先）
- 分段并行：循环将整批 tool_calls 一次性交给注入的 tool_calls_fn（execute_batch 内部做 gather/串行）
- 透明预算：每轮追加 ``[剩余轮次：N]``；remaining==0 的末轮 tool_choice=terminal（I-4）
- 畸形 tool_use（execute_batch 返回 is_error 的 ToolResultBlock）→ 循环继续不崩溃
- max_iterations 耗尽 → stop_reason=exhausted + last_response（M26）
- span 钩子：span_recorder 在每轮 chat 前后被调用（start/end），None 时跳过
- 不直接依赖 LLMClient：LLM 调用经注入的 chat_fn（可 mock）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.services.agent.loop import RunResult, run
from app.services.llm.models import (
    ChatResponse,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _resp(
    stop_reason: str, tool_calls: list[ToolUseBlock] | None = None
) -> ChatResponse:
    """构造一条 ChatResponse，blocks 携带 tool_use blocks（如有）。"""
    blocks = list(tool_calls or [])
    return ChatResponse(content="", blocks=blocks, stop_reason=stop_reason)


def _tool_use(tid: str, name: str, input_: dict | None = None) -> ToolUseBlock:
    return ToolUseBlock(id=tid, name=name, input=input_ or {})


def _seed() -> list[Message]:
    """最小种子：system + user。"""
    return [
        Message(role="system", content="你是匹配助手"),
        Message(role="user", content="请匹配：花开伊吕波剧场版"),
    ]


def _ok_result(tc: ToolUseBlock, content: str = "ok") -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=tc.id, content=content, is_error=False)


# ---------------------------------------------------------------------------
# 1. end_turn 终止
# ---------------------------------------------------------------------------


async def test_run_end_turn_returns_end_turn_with_text():
    chat_fn = AsyncMock(return_value=_resp("end_turn", None))
    chat_fn.return_value.content = "已分析完毕，无建议"

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[{"name": "search_bangumi"}],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert isinstance(result, RunResult)
    assert result.stop_reason == "end_turn"
    assert result.text == "已分析完毕，无建议"
    assert result.last_response is not None
    chat_fn.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. 空响应 / 无 tool_calls 兜底终止
# ---------------------------------------------------------------------------


async def test_run_no_tool_calls_falls_back_to_end_turn():
    # stop_reason=tool_use 但 blocks 里没有任何 ToolUseBlock
    chat_fn = AsyncMock(return_value=_resp("tool_use", []))

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "end_turn"
    chat_fn.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. assistant 聚合消息先于 tool_result
# ---------------------------------------------------------------------------


async def test_assistant_aggregate_message_precedes_tool_results():
    # 第一轮返回两个 read 工具调用，第二轮 end_turn
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))  # messages 是第一个位置参数
        if len(calls) == 1:
            return _resp(
                "tool_use",
                [
                    _tool_use("t1", "search_bangumi"),
                    _tool_use("t2", "get_subject_detail"),
                ],
            )
        return _resp("end_turn", None)

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock(
        return_value={
            "t1": _ok_result(_tool_use("t1", "search_bangumi")),
            "t2": _ok_result(_tool_use("t2", "get_subject_detail")),
        }
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    # 第二轮调用时，messages 中应已含：assistant(聚合两个 tool_use) + 两条 tool_result
    second_round_messages = calls[1]
    roles_contents = [
        (
            m.role,
            [b.type for b in m.content] if isinstance(m.content, list) else m.content,
        )
        for m in second_round_messages
    ]

    # 找到 assistant 聚合消息（content 为两个 tool_use）的索引
    assistant_idx = next(
        i
        for i, (role, content) in enumerate(roles_contents)
        if role == "assistant" and content == ["tool_use", "tool_use"]
    )
    # 找到 tool_result 的 user 消息索引
    tool_result_indices = [
        i
        for i, (role, content) in enumerate(roles_contents)
        if role == "user"
        and isinstance(content, list)
        and any(b == "tool_result" for b in content)
    ]

    assert assistant_idx != -1
    assert tool_result_indices, "应当存在 tool_result 消息"
    # 聚合 assistant 必须早于所有 tool_result
    assert all(assistant_idx < idx for idx in tool_result_indices)


# ---------------------------------------------------------------------------
# 4. submit_suggestion 捕获即 break，同轮其他工具不执行
# ---------------------------------------------------------------------------


async def test_submit_suggestion_breaks_and_captures_without_executing_others():
    submit = _tool_use(
        "ts", "submit_suggestion", {"subject_id": "49892", "reason": "标题语义相近"}
    )
    read = _tool_use("tr", "search_bangumi", {"title": "花开伊吕波"})
    # 同轮同时含 read 与 submit：终止工具优先，read 不应执行
    chat_fn = AsyncMock(return_value=_resp("tool_use", [read, submit]))
    tool_calls_fn = AsyncMock()

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "submit_suggestion"
    assert result.suggestion == {"subject_id": "49892", "reason": "标题语义相近"}
    # 同轮其他工具不执行：execute_batch 不应被调用
    tool_calls_fn.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. 分段并行：整批交给 tool_calls_fn（顺序保留）
# ---------------------------------------------------------------------------


async def test_segmented_parallel_passes_full_batch_in_order():
    a = _tool_use("a", "search_bangumi")
    b = _tool_use("b", "get_subject_detail")
    c = _tool_use("c", "get_related_subjects")
    chat_fn = AsyncMock(
        side_effect=[_resp("tool_use", [a, b, c]), _resp("end_turn", None)]
    )
    tool_calls_fn = AsyncMock(
        return_value={
            "a": _ok_result(a),
            "b": _ok_result(b),
            "c": _ok_result(c),
        }
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    # 循环把整批（含分段）一次性交给 tool_calls_fn，由 execute_batch 内部做 gather/串行
    tool_calls_fn.assert_awaited_once()
    passed = tool_calls_fn.call_args[0][0]
    assert [tc.name for tc in passed] == [
        "search_bangumi",
        "get_subject_detail",
        "get_related_subjects",
    ]
    assert [tc.id for tc in passed] == ["a", "b", "c"]


async def test_segmented_parallel_preserves_mixed_read_write_order():
    # 循环对 read/write 无感知（分段是 execute_batch 内部职责），这里仅验证整批顺序透传。
    # 注意：中间工具绝不能是 tool_choice_terminal（否则触发终止优先 break）。
    r = _tool_use("r", "search_bangumi")
    w = _tool_use("w", "check_subject")  # 代表非终止的写/读工具，仅用于验证顺序
    r2 = _tool_use("r2", "get_subject_detail")
    chat_fn = AsyncMock(
        side_effect=[_resp("tool_use", [r, w, r2]), _resp("end_turn", None)]
    )
    tool_calls_fn = AsyncMock(
        return_value={
            "r": _ok_result(r),
            "w": _ok_result(w),
            "r2": _ok_result(r2),
        }
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    passed = tool_calls_fn.call_args[0][0]
    assert [tc.id for tc in passed] == ["r", "w", "r2"]


# ---------------------------------------------------------------------------
# 6. 透明预算：每轮追加 [剩余轮次：N]；末轮强制 terminal
# ---------------------------------------------------------------------------


async def test_transparent_budget_appends_remaining_and_forces_terminal_on_last_round():
    calls: list[tuple[list[Message], dict]] = []

    def _side_effect(*args, **kwargs):
        calls.append((list(args[0]), dict(kwargs)))
        # 每轮都返回工具调用，迫使循环跑满 max_iterations=2
        return _resp("tool_use", [_tool_use("t", "search_bangumi")])

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "exhausted"

    # 首轮 tool_choice=None；末轮（remaining==1 起手）tool_choice=terminal
    assert calls[0][1]["tool_choice"] is None
    assert calls[1][1]["tool_choice"] == "submit_suggestion"

    # 第二轮请求收到的 messages 末尾应携带第一轮留下的预算消息 [剩余轮次：1]
    second_messages = calls[1][0]
    last_msg = second_messages[-1]
    assert last_msg.role == "user"
    assert last_msg.content == "[剩余轮次：1]"


# ---------------------------------------------------------------------------
# 7. 畸形 tool_use（is_error 的 ToolResultBlock）→ 循环继续不崩溃
# ---------------------------------------------------------------------------


async def test_malformed_tool_use_is_error_block_continues_loop():
    bad = _tool_use("bad", "unknown_tool", {"foo": "bar"})
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        if len(calls) == 1:
            return _resp("tool_use", [bad])
        return _resp("end_turn", None)

    chat_fn = AsyncMock(side_effect=_side_effect)
    # execute_batch 对未知工具返回 is_error 的 ToolResultBlock
    tool_calls_fn = AsyncMock(
        return_value={
            "bad": ToolResultBlock(
                tool_use_id="bad", content="工具执行失败: ToolError", is_error=True
            )
        }
    )

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    # 不应崩溃，应继续到下一轮并正常 end_turn
    assert result.stop_reason == "end_turn"
    # 错误块应被作为 tool_result 追加进 messages（第二轮可见）
    second_messages = calls[1]
    error_blocks = [
        b
        for m in second_messages
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.is_error
    ]
    assert error_blocks, "is_error 的 ToolResultBlock 应被追加"


# ---------------------------------------------------------------------------
# 8. 耗尽：max_iterations 内始终调工具不终止 → exhausted + last_response
# ---------------------------------------------------------------------------


async def test_max_iterations_exhausted_returns_last_response():
    chat_fn = AsyncMock(
        return_value=_resp("tool_use", [_tool_use("t", "search_bangumi")])
    )
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "exhausted"
    assert result.last_response is not None
    assert result.last_response.stop_reason == "tool_use"
    assert chat_fn.await_count == 2


# ---------------------------------------------------------------------------
# 9. span 钩子：每轮 chat 前后调用 start_span/end_span；None 时跳过
# ---------------------------------------------------------------------------


async def test_span_recorder_hooks_called_per_round():
    span_recorder = MagicMock()
    span_recorder.start_span.return_value = "span-1"

    chat_fn = AsyncMock(
        return_value=_resp("tool_use", [_tool_use("t", "search_bangumi")])
    )
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        span_recorder=span_recorder,
    )

    # 3 轮，每轮一次 chat → 一次 start_span / end_span + 一次预算消息记录
    assert span_recorder.start_span.call_count == 3
    assert span_recorder.end_span.call_count == 3
    assert span_recorder.record_budget_message.call_count == 3


async def test_span_recorder_none_is_noop():
    # span_recorder=None 不应抛错（可空实现）
    chat_fn = AsyncMock(return_value=_resp("end_turn", None))

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        span_recorder=None,
    )

    assert result.stop_reason == "end_turn"

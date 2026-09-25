"""轻量循环 ``app/services/agent/loop.py`` 测试（分段并行 + 终止 + 预算）。

覆盖：
- end_turn / 空响应（无 tool_calls）→ stop_reason=end_turn
- assistant 聚合消息先于 tool_result（协议顺序）
- submit_suggestion 捕获即 break（stop_reason=submit_suggestion），同轮其他工具不执行（终止工具优先）
- 分段并行：循环将整批 tool_calls 一次性交给注入的 tool_calls_fn（execute_batch 内部做 gather/串行）
- 透明预算：仅当递减后 remaining>0 才追加 ``[剩余轮次：N]``；remaining==1 起手的末轮 tool_choice=terminal
- 畸形 tool_use（execute_batch 返回 is_error 的 ToolResultBlock）→ 循环继续不崩溃
- max_iterations 耗尽 → stop_reason=exhausted + last_response
- 预算钩子：recorder.record_budget 每个非末轮恰好调用一次且参数为 "[剩余轮次：N]"；None 时跳过；
  末轮（递减后 remaining==0）不注入、不记录（幻影消息修复）
- 防御分支：result 非 ToolResultBlock 时记 warning
- 不直接依赖 LLMClient：LLM 调用经注入的 chat_fn（可 mock）
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.services.agent import loop as loop_module
from app.services.agent.loop import RunResult, run
from app.services.llm.models import (
    ChatResponse,
    Message,
    TextBlock,
    ThinkingBlock,
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
# 6. 透明预算：每轮（递减后 remaining>0）追加 [剩余轮次：N]；末轮强制 terminal
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

    # 第二轮请求收到的 messages 末尾应携带第一轮留下的末轮强化提示
    second_messages = calls[1][0]
    last_msg = second_messages[-1]
    assert last_msg.role == "user"
    assert last_msg.content == loop_module.FINAL_ROUND_BUDGET_MESSAGE


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
# 7b. G2：重复 tool_use_id → 循环按独立槽位（ordered）逐条回填，
#     首个 tool_result 为真实结果，第二个为 duplicate 错误块
# ---------------------------------------------------------------------------


async def test_duplicate_tool_use_ids_backfill_each_slot_independently():
    from app.services.llm.tools import BatchResults

    dup_a = _tool_use("same", "search_bangumi")
    dup_b = _tool_use("same", "search_bangumi")
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        if len(calls) == 1:
            return _resp("tool_use", [dup_a, dup_b])
        return _resp("end_turn", None)

    chat_fn = AsyncMock(side_effect=_side_effect)
    ok = ToolResultBlock(tool_use_id="same", content="real", is_error=False)
    dup = ToolResultBlock(
        tool_use_id="same", content="duplicate tool_use_id", is_error=True
    )
    tool_calls_fn = AsyncMock(return_value=BatchResults([("same", ok), ("same", dup)]))

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "end_turn"
    second_messages = calls[1]
    blocks = [
        b
        for m in second_messages
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    # 每个 tool_use 各有一条 tool_result（协议闭合），且首个保留真实结果
    assert len(blocks) == 2
    assert blocks[0].content == "real"
    assert blocks[0].is_error is False
    assert blocks[1].content == "duplicate tool_use_id"
    assert blocks[1].is_error is True


async def test_plain_dict_tool_results_still_supported():
    """向后兼容：tool_calls_fn 返回普通 dict（无 ordered）时按 id 取值。"""
    a = _tool_use("t1", "search_bangumi")
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        if len(calls) == 1:
            return _resp("tool_use", [a])
        return _resp("end_turn", None)

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock(return_value={"t1": _ok_result(a, content="plain")})

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "end_turn"
    blocks = [
        b
        for m in calls[1]
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert len(blocks) == 1
    assert blocks[0].content == "plain"


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
    # 2 轮循环 + 1 次兜底收尾调用
    assert chat_fn.await_count == 3


# ---------------------------------------------------------------------------
# 9. 预算钩子：recorder.record_budget 每轮恰好调用一次; None 时跳过
# ---------------------------------------------------------------------------


class _FakeBudgetRecorder:
    """记录 record_budget 调用，便于断言预算消息。"""

    def __init__(self) -> None:
        self.budget_calls: list[str] = []

    def record_budget(self, budget_message: str) -> None:
        self.budget_calls.append(budget_message)


async def test_budget_record_budget_called_per_non_final_round_with_remaining():
    """非末轮记录 "[剩余轮次：N]"，末轮记录强化文案；收尾再记录一次收尾提示。"""
    recorder = _FakeBudgetRecorder()

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
        recorder=recorder,
    )

    # 3 轮中前 2 轮递减后 remaining>0（2、1）→ 各记录一次；末轮递减后 remaining==0
    # 的预算消息从未发给 LLM，不得记录；循环结束后收尾提示记录一次
    assert recorder.budget_calls == [
        "[剩余轮次：2]",
        loop_module.FINAL_ROUND_BUDGET_MESSAGE,
        loop_module.FINAL_RECOVERY_MESSAGE,
    ]
    assert "[剩余轮次：0]" not in recorder.budget_calls


async def test_budget_recorder_none_is_noop():
    """recorder=None 时不应抛错（可空实现）。"""
    chat_fn = AsyncMock(return_value=_resp("end_turn", None))

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        recorder=None,
    )


async def test_budget_appended_to_messages_even_without_recorder():
    """预算消息仍追加进 messages（与 recorder 无关的领域语义）。"""
    calls: list[tuple[list[Message], dict]] = []

    def _side_effect(*args, **kwargs):
        calls.append((list(args[0]), dict(kwargs)))
        return _resp("tool_use", [_tool_use("t", "search_bangumi")])

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        recorder=None,
    )

    # 第二轮请求收到的 messages 末尾应携带第一轮留下的末轮强化提示
    second_messages = calls[1][0]
    last_msg = second_messages[-1]
    assert last_msg.role == "user"
    assert last_msg.content == loop_module.FINAL_ROUND_BUDGET_MESSAGE


async def test_budget_no_phantom_message_on_final_round():
    """起手 remaining==1 的末轮模型返回非终止工具 → 不注入/记录 [剩余轮次：0]。

    末轮执行完毕后循环随即结束，该预算消息从未发给 LLM，不应进入 messages 或 trace。
    """
    recorder = _FakeBudgetRecorder()
    observed: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        # 传引用（不 copy）：循环后续 append 会反映到同一列表，便于检查终态
        observed.append(args[0])
        return _resp("tool_use", [_tool_use("t", "search_bangumi")])

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=1,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        recorder=recorder,
    )

    assert result.stop_reason == "exhausted"
    # 末轮递减后 remaining==0 不记录剩余轮次；循环后收尾提示记录一次
    assert recorder.budget_calls == [loop_module.FINAL_RECOVERY_MESSAGE]
    phantom = [
        m.content
        for m in observed[0]
        if m.role == "user"
        and isinstance(m.content, str)
        and m.content.startswith("[剩余轮次")
    ]
    assert phantom == [], f"末轮不应生成幻影预算消息，实际 {phantom}"


# ---------------------------------------------------------------------------
# 10. 防御分支：result 非 ToolResultBlock 时记 warning
# ---------------------------------------------------------------------------


async def test_defense_branch_logs_warning_on_non_tool_result(caplog):
    """result 非 ToolResultBlock（如 TerminalCapture 泄漏）→ 跳过并记 warning。"""
    import logging

    a = _tool_use("a", "search_bangumi")

    chat_fn = AsyncMock(return_value=_resp("tool_use", [a]))
    # 返回 None 结果（模拟 TerminalCapture 泄漏到 aligned 槽位）
    tool_calls_fn = AsyncMock(return_value={})

    with caplog.at_level(logging.WARNING):
        await run(
            chat_fn=chat_fn,
            tools_schemas=[],
            tool_calls_fn=tool_calls_fn,
            max_iterations=1,
            tool_choice_terminal="submit_suggestion",
            seed_messages=_seed(),
        )

    # 应打出 warning 日志
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("tool result 缺失或非 ToolResultBlock" in r.message for r in warnings)


# ---------------------------------------------------------------------------
# 12. 终止分类：空壳响应 → llm_error；max_tokens → max_tokens
# ---------------------------------------------------------------------------


async def test_run_empty_shell_response_returns_llm_error():
    """空壳响应（stop_reason=""、无 blocks、content 为空）→ stop_reason='llm_error'（不再 end_turn）。"""
    from app.services.llm.models import ChatResponse

    # 模拟 LLMCallError 后被包装成的空壳（client 不再返回此物，但 loop 仍需防御）
    chat_fn = AsyncMock(
        return_value=ChatResponse(content="", blocks=[], stop_reason="")
    )

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "llm_error"


async def test_run_max_tokens_response_returns_max_tokens():
    """无 tool_calls 且 stop_reason='max_tokens' → stop_reason='max_tokens'。"""
    from app.services.llm.models import ChatResponse

    chat_fn = AsyncMock(
        return_value=ChatResponse(
            content="truncated...", blocks=[], stop_reason="max_tokens"
        )
    )

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "max_tokens"


async def test_run_end_turn_unchanged():
    """stop_reason='end_turn' 路径不变。"""
    chat_fn = AsyncMock(return_value=_resp("end_turn", None))
    chat_fn.return_value.content = "已分析完毕"

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[{"name": "search_bangumi"}],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "end_turn"
    assert result.text == "已分析完毕"


async def test_run_tool_calls_path_unchanged():
    """有 tool_calls 的路径不受影响（stop_reason='tool_use' 含 tool_calls）。"""
    chat_fn = AsyncMock(
        side_effect=[
            _resp("tool_use", [_tool_use("t1", "search_bangumi")]),
            _resp("end_turn", None),
        ]
    )
    tool_calls_fn = AsyncMock(
        return_value={"t1": _ok_result(_tool_use("t1", "search_bangumi"))}
    )

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "end_turn"
    assert chat_fn.await_count == 2


# ---------------------------------------------------------------------------
# 13. loop.py 不 import 任何 trace 符号（S2.4）
# ---------------------------------------------------------------------------


def test_loop_no_trace_imports():
    """loop.py 不应 import trace 相关符号（零 span 逻辑）。"""
    import ast

    with open("app/services/agent/loop.py") as f:
        tree = ast.parse(f.read())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "trace" not in alias.name, (
                    f"loop.py 不应 import trace 模块: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module and "trace" in node.module:
                raise AssertionError(
                    f"loop.py 不应 from ... import trace: {node.module}"
                )


# ---------------------------------------------------------------------------
# N. 思考模型兼容：assistant 聚合消息保留 Text/Thinking 块（随 tool_use 回传）
# ---------------------------------------------------------------------------


async def test_assistant_message_preserves_text_and_thinking_blocks():
    """响应含 text+thinking+tool_use 时，assistant 消息保留全部块（顺序不变）。

    思考模型（Anthropic thinking 模式 / DeepSeek pro）要求 thinking 块随
    tool_use 在后续请求回传，否则真实端点 400。
    """
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        if len(calls) == 1:
            return ChatResponse(
                content="先搜索",
                blocks=[
                    TextBlock(text="先搜索"),
                    ThinkingBlock(thinking="我需要先搜索", signature="sig-1"),
                    _tool_use("t1", "search_bangumi"),
                ],
                stop_reason="tool_use",
            )
        return _resp("end_turn", None)

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock(
        return_value={"t1": _ok_result(_tool_use("t1", "search_bangumi"))}
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assistant_msg = next(m for m in calls[1] if m.role == "assistant")
    types = [b.type for b in assistant_msg.content]
    assert types == ["text", "thinking", "tool_use"]
    thinking_block = next(b for b in assistant_msg.content if b.type == "thinking")
    assert thinking_block.signature == "sig-1"


# ---------------------------------------------------------------------------
# 14. 末轮强化提示：remaining==1 的预算消息改为强化文案；非末轮不含
# ---------------------------------------------------------------------------


async def test_final_round_budget_message_is_strengthened():
    """末轮（remaining==1）注入的预算消息含「最后一轮 / submit_suggestion / 不得再检索」。"""
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        return _resp("tool_use", [_tool_use("t", "search_bangumi")])

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    # max_iterations=2：第一轮递减后 remaining==1 → 注入强化提示，第二轮请求可见
    last_msg = calls[1][-1]
    assert last_msg.role == "user"
    assert isinstance(last_msg.content, str)
    assert "最后一轮" in last_msg.content
    assert "submit_suggestion" in last_msg.content
    assert "不得再" in last_msg.content


async def test_non_final_round_budget_message_not_strengthened():
    """非末轮（remaining>1）仍是朴素文案，不含强化提示语义。"""
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        return _resp("tool_use", [_tool_use("t", "search_bangumi")])

    chat_fn = AsyncMock(side_effect=_side_effect)
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
    )

    # 第一轮递减后 remaining==2 → 第二轮请求末尾是朴素预算消息
    second_last = calls[1][-1]
    assert second_last.role == "user"
    assert second_last.content == "[剩余轮次：2]"
    assert "最后一轮" not in second_last.content


# ---------------------------------------------------------------------------
# 15. 兜底收尾调用（for 循环自然结束后最多一次）
# ---------------------------------------------------------------------------


def _exhausting_chat(max_iterations: int, extra: list[ChatResponse]):
    """构造 chat_fn：前 max_iterations 轮返回非终止工具，之后按 extra 依次返回。"""
    calls: list[tuple[list[Message], dict]] = []
    seq = list(extra)

    def _side_effect(*args, **kwargs):
        calls.append((list(args[0]), dict(kwargs)))
        if len(calls) <= max_iterations:
            return _resp("tool_use", [_tool_use("t", "search_bangumi")])
        if seq:
            return seq.pop(0)
        return _resp("tool_use", [_tool_use("t", "search_bangumi")])

    return AsyncMock(side_effect=_side_effect), calls


async def test_exhausted_final_recovery_submits_suggestion():
    """耗尽后收尾调用返回 submit → stop_reason=submit_suggestion 且参数正确。"""
    submit = _tool_use(
        "s", "submit_suggestion", {"subject_id": "49892", "reason": "收尾确定"}
    )
    recovery = _resp("tool_use", [submit])
    chat_fn, calls = _exhausting_chat(2, [recovery])
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

    assert result.stop_reason == "submit_suggestion"
    assert result.suggestion == {"subject_id": "49892", "reason": "收尾确定"}
    assert result.last_response is recovery
    # 收尾请求的 messages 末尾为收尾提示
    assert calls[2][0][-1].content == loop_module.FINAL_RECOVERY_MESSAGE


async def test_exhausted_final_recovery_still_no_submit_returns_exhausted():
    """耗尽后收尾仍不提交 → exhausted，last_response 为收尾响应。"""
    recovery = _resp("tool_use", [_tool_use("t", "search_bangumi")])
    chat_fn, _calls = _exhausting_chat(2, [recovery])
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
    assert result.last_response is recovery


async def test_exhausted_final_recovery_exception_returns_exhausted_not_crash():
    """收尾调用抛异常 → best-effort 返回 exhausted（last_response 保持循环内最后一次）。"""
    loop_resp = _resp("tool_use", [_tool_use("t", "search_bangumi")])
    call_count = {"n": 0}

    async def _chat(messages, *, tools=None, tool_choice=None):
        call_count["n"] += 1
        if call_count["n"] <= 2:  # max_iterations=2 的循环内两轮
            return loop_resp
        raise RuntimeError("收尾调用失败")

    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    result = await run(
        chat_fn=_chat,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert result.stop_reason == "exhausted"
    assert result.last_response is loop_resp


async def test_exhausted_final_recovery_tools_only_terminal_schema():
    """收尾调用仅传 terminal 工具的 schema，且 tool_choice=terminal；不执行其他工具。"""
    recovery = _resp(
        "tool_use", [_tool_use("s", "submit_suggestion", {"subject_id": "1"})]
    )
    chat_fn, calls = _exhausting_chat(2, [recovery])
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )
    schemas = [
        {"name": "search_bangumi"},
        {"name": "submit_suggestion"},
    ]

    await run(
        chat_fn=chat_fn,
        tools_schemas=schemas,
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    # calls[2] 为收尾调用
    assert calls[2][1]["tools"] == [{"name": "submit_suggestion"}]
    assert calls[2][1]["tool_choice"] == "submit_suggestion"
    # 收尾不执行任何非终止工具：tool_calls_fn 只被循环内两轮调用
    assert tool_calls_fn.await_count == 2


async def test_exhausted_final_recovery_called_exactly_once():
    """收尾调用恰好一次：chat_fn 总调用数 = max_iterations + 1。"""
    recovery = _resp("tool_use", [_tool_use("t", "search_bangumi")])
    chat_fn, _calls = _exhausting_chat(2, [recovery])
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
    )

    assert chat_fn.await_count == 3  # 2 轮循环 + 1 次收尾


async def test_exhausted_final_recovery_message_recorded_via_budget_channel():
    """收尾提示须经 recorder.record_budget 记录（恢复重放一致性）。"""
    recorder = _FakeBudgetRecorder()
    recovery = _resp("tool_use", [_tool_use("t", "search_bangumi")])
    chat_fn, _calls = _exhausting_chat(2, [recovery])
    tool_calls_fn = AsyncMock(
        return_value={"t": _ok_result(_tool_use("t", "search_bangumi"))}
    )

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=2,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        recorder=recorder,
    )

    assert loop_module.FINAL_RECOVERY_MESSAGE in recorder.budget_calls


# ---------------------------------------------------------------------------
# 16. 终止提交软护栏（veto）：一次暂缓 + tool_result 注入 + recorder 落 span
# ---------------------------------------------------------------------------


class _FakeToolRecorder(_FakeBudgetRecorder):
    """记录 start_tool / end_tool 调用（veto 伪执行落 span 断言用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[str, ToolUseBlock, int]] = []
        self.ended: list[tuple[str, ToolResultBlock | None, str]] = []

    def start_tool(self, tool_use: ToolUseBlock, *, sequence: int) -> str:
        span_id = f"span-{len(self.started)}"
        self.started.append((span_id, tool_use, sequence))
        return span_id

    def end_tool(
        self,
        span_id: str,
        *,
        result: ToolResultBlock | None = None,
        error: str = "",
    ) -> None:
        self.ended.append((span_id, result, error))


def _collect_tool_results(messages: list[Message]) -> list[ToolResultBlock]:
    return [
        b
        for m in messages
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]


async def test_veto_defers_first_terminal_and_injects_hint_tool_result():
    """veto 返回提示 → 首次 submit 不终止，注入 tool_result，下一轮放行。"""
    first = _tool_use(
        "ts1", "submit_suggestion", {"subject_id": "1", "reason": "暂推荐"}
    )
    second = _tool_use(
        "ts2", "submit_suggestion", {"subject_id": "2", "reason": "确定"}
    )
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        return _resp("tool_use", [first if len(calls) == 1 else second])

    chat_fn = AsyncMock(side_effect=_side_effect)
    tool_calls_fn = AsyncMock()
    veto_inputs: list[dict] = []

    def _veto(args: dict) -> str | None:
        veto_inputs.append(args)
        return None if args.get("subject_id") == "2" else "请再核对"

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=tool_calls_fn,
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        veto_terminal=_veto,
    )

    assert result.stop_reason == "submit_suggestion"
    assert result.suggestion == {"subject_id": "2", "reason": "确定"}
    # 首次 submit 被暂缓：其他工具不执行
    tool_calls_fn.assert_not_awaited()
    # 第二轮请求携带注入的 veto tool_result（配对 terminal tool_use）
    blocks = _collect_tool_results(calls[1])
    veto_blocks = [b for b in blocks if b.tool_use_id == "ts1"]
    assert veto_blocks, "应注入 terminal tool_use 配对的 tool_result"
    assert veto_blocks[0].content == "请再核对"
    assert veto_blocks[0].is_error is False
    # 首次 submit 被询问过（第二次因「已 veto 过」直接放行，见另一用例）
    assert [v["subject_id"] for v in veto_inputs] == ["1"]


async def test_veto_happens_at_most_once_per_run():
    """veto 只拦一次：第二次 terminal 不再调 veto、直接终止。"""
    submit = _tool_use(
        "s", "submit_suggestion", {"subject_id": "1", "reason": "暂推荐"}
    )
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        return _resp("tool_use", [submit])

    chat_fn = AsyncMock(side_effect=_side_effect)
    veto_calls: list[dict] = []

    def _veto(args: dict) -> str | None:
        veto_calls.append(args)
        return "暂缓"

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        veto_terminal=_veto,
    )

    assert result.stop_reason == "submit_suggestion"
    assert result.suggestion == {"subject_id": "1", "reason": "暂推荐"}
    # veto 仅调用一次（第二次 terminal 因「已 veto 过」直接放行，不再询问）
    assert len(veto_calls) == 1
    assert chat_fn.await_count == 2


async def test_veto_not_applied_on_final_round():
    """末轮（remaining==1 起手）强收尾优先：不 veto，直接终止。"""
    submit = _tool_use(
        "s", "submit_suggestion", {"subject_id": "1", "reason": "暂推荐"}
    )
    chat_fn = AsyncMock(return_value=_resp("tool_use", [submit]))
    veto_calls: list[dict] = []

    def _veto(args: dict) -> str | None:
        veto_calls.append(args)
        return "暂缓"

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=1,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        veto_terminal=_veto,
    )

    assert result.stop_reason == "submit_suggestion"
    assert result.suggestion == {"subject_id": "1", "reason": "暂推荐"}
    # 末轮不 veto：回调根本不被调用
    assert veto_calls == []
    chat_fn.assert_awaited_once()


async def test_veto_none_behaves_like_current_behavior():
    """veto_terminal=None 时行为与现状一致：submit 立即终止。"""
    submit = _tool_use(
        "s", "submit_suggestion", {"subject_id": "1", "reason": "暂推荐"}
    )
    chat_fn = AsyncMock(return_value=_resp("tool_use", [submit]))

    result = await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        veto_terminal=None,
    )

    assert result.stop_reason == "submit_suggestion"
    assert result.suggestion == {"subject_id": "1", "reason": "暂推荐"}
    chat_fn.assert_awaited_once()


async def test_veto_records_injected_tool_result_span_for_replay():
    """veto 伪执行须经 recorder 落 tool_execute span（replay 一致性）。"""
    first = _tool_use("ts1", "submit_suggestion", {"subject_id": "1"})
    second = _tool_use("ts2", "submit_suggestion", {"subject_id": "2"})
    calls: list[list[Message]] = []

    def _side_effect(*args, **kwargs):
        calls.append(list(args[0]))
        return _resp("tool_use", [first if len(calls) == 1 else second])

    chat_fn = AsyncMock(side_effect=_side_effect)
    recorder = _FakeToolRecorder()

    await run(
        chat_fn=chat_fn,
        tools_schemas=[],
        tool_calls_fn=AsyncMock(),
        max_iterations=3,
        tool_choice_terminal="submit_suggestion",
        seed_messages=_seed(),
        recorder=recorder,
        veto_terminal=lambda args: "请再核对",
    )

    # 仅 veto 伪执行落 span（terminal 分支不经 execute_batch）
    assert len(recorder.started) == 1
    _span_id, tool_use, sequence = recorder.started[0]
    assert tool_use.id == "ts1"
    # sequence 与 execute_batch 约定一致：terminal 在 tool_calls 中的下标
    assert sequence == 0
    assert len(recorder.ended) == 1
    ended_result = recorder.ended[0][1]
    assert ended_result is not None
    assert ended_result.tool_use_id == "ts1"
    assert ended_result.content == "请再核对"
    assert ended_result.is_error is False

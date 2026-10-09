"""``runtime.continue_run`` 恢复续跑状态机：终局分派防御测试。

覆盖：
- ``stop_reason`` 声称终止工具但 ``tool_calls`` 无匹配终止工具 → 不得把空 dict
  当作真实终局提交；应记 warning 并降级 exhausted，handle_terminal 不被调用
- 正常路径（存在匹配终止工具调用）行为不变：仍走 handle_terminal
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from app.core.database import (
    DatabaseManager,
    get_database_manager,
    set_database_manager,
)
from app.services.agent import runtime, trace
from app.services.agent.scenario import ScenarioHooks


@pytest.fixture
def dbm(tmp_path: Path) -> Iterator[DatabaseManager]:
    instance = DatabaseManager(str(tmp_path / "continue.db"))
    set_database_manager(instance)
    yield instance
    if instance._connection._conn is not None:
        instance._connection._conn.close()
    set_database_manager(None)


@pytest.fixture
def log_records():
    """捕获自定义 Logger（print 实现）的日志行，产出 ``[(level, line)]``。"""
    from app.core.logging import logger as app_logger

    records: list[tuple[str, str]] = []

    def _listener(line: str, level: str) -> None:
        records.append((level, line))

    app_logger.add_listener(_listener)
    yield records
    app_logger.remove_listener(_listener)


def _write_seed(dbm: DatabaseManager, run_id: str) -> None:
    dbm.agent_runs.create_pending(run_id, "match")
    span_id = trace.start_span(run_id, "seed", 0, 0)
    dbm.agent_runs.update_step(
        span_id,
        replay_delta={"seed_messages": [{"role": "system", "content": "sys"}]},
        ended_at=int(time.time()),
    )


def _write_llm_chat(
    dbm: DatabaseManager,
    run_id: str,
    iteration: int,
    tool_calls,
    stop_reason: str,
    *,
    content: str = "",
    blocks=None,
) -> None:
    response: dict = {
        "stop_reason": stop_reason,
        "content": content,
        "tool_calls": tool_calls,
    }
    if blocks is not None:
        response["blocks"] = blocks
    span_id = trace.start_span(run_id, "llm_chat", iteration, 0)
    trace.end_span(span_id, replay_delta={"response": response})


class _FakeHooks:
    task_type = "match"
    terminal_tool = "submit_suggestion"

    def __init__(self) -> None:
        self.terminal_calls: list[tuple[tuple, dict]] = []
        # 恢复路径不应触发 veto 软护栏（见 runtime.continue_run submit 分支注释）
        self.veto_calls: list[dict] = []

    def resolve_thinking_level(self) -> str:
        return "medium"

    def resolve_max_iterations(self, level: str) -> int:
        return 10

    def veto_terminal(self, suggestion: dict) -> str | None:
        """live 路径的终止提交软护栏；恢复路径不应调用。"""
        self.veto_calls.append(suggestion)
        return "请先核对候选再提交"

    async def handle_terminal(self, run_id, result, ctx, **kwargs):
        """终局回调：新契约**不再**接收 dbm（全局单例由场景自取）。"""
        self.terminal_calls.append(((run_id, result, ctx), kwargs))
        # 模拟真实场景（llm_assist）终态落点：llm_error/max_tokens → failed
        if getattr(result, "stop_reason", None) in ("llm_error", "max_tokens"):
            get_database_manager().agent_runs.mark_failed(
                run_id,
                stop_reason=result.stop_reason,
                last_error=f"循环终止原因: {result.stop_reason}",
            )
        return "succeeded"


def _hooks_of(fake: _FakeHooks) -> ScenarioHooks:
    """显式收窄：_FakeHooks 结构上满足 runtime 所需钩子字段（测试替身）。"""
    return cast("ScenarioHooks", fake)


def test_continue_run_terminal_stop_without_tool_call_degrades_to_exhausted(
    dbm: DatabaseManager, log_records
):
    """stop_reason=终止工具名但 tool_calls 为空 → 降级 exhausted，不提交空建议。"""
    run_id = "run-term-empty"
    _write_seed(dbm, run_id)
    _write_llm_chat(dbm, run_id, 0, [], stop_reason="submit_suggestion")
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    run = dbm.agent_runs.get_run(run_id)
    assert run is not None
    assert run["status"] == "succeeded"
    assert run["stop_reason"] == "exhausted"
    assert hooks.terminal_calls == [], "空建议不得传给 handle_terminal"
    warns = [line for level, line in log_records if level == "WARNING"]
    assert any("submit_suggestion" in line for line in warns)


def test_continue_run_terminal_stop_without_matching_tool_call_degrades_to_exhausted(
    dbm: DatabaseManager,
):
    """stop_reason=终止工具名但 tool_calls 不含该工具 → 同样降级 exhausted。"""
    run_id = "run-term-mismatch"
    _write_seed(dbm, run_id)
    _write_llm_chat(
        dbm,
        run_id,
        0,
        [{"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}}],
        stop_reason="submit_suggestion",
    )
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    run = dbm.agent_runs.get_run(run_id)
    assert run is not None
    assert run["status"] == "succeeded"
    assert run["stop_reason"] == "exhausted"
    assert hooks.terminal_calls == []


def test_continue_run_with_matching_terminal_tool_call_dispatches_normally(
    dbm: DatabaseManager,
):
    """存在匹配终止工具调用 → 正常走 handle_terminal（正常路径零回归）。"""
    run_id = "run-term-ok"
    _write_seed(dbm, run_id)
    submit_input = {"subject_id": "2", "reason": "best match"}
    _write_llm_chat(
        dbm,
        run_id,
        0,
        [{"id": "t9", "name": "submit_suggestion", "input": submit_input}],
        stop_reason="tool_use",
    )
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    assert len(hooks.terminal_calls) == 1
    args, _kwargs = hooks.terminal_calls[0]
    result = args[1]
    assert result.suggestion == submit_input
    assert result.stop_reason == "submit_suggestion"


def test_continue_run_submit_terminal_does_not_replay_veto(
    dbm: DatabaseManager,
):
    """恢复路径的 submit 终局直接 handle_terminal，不重放 veto 软护栏。

    守卫当前**有意**设计：veto 是 live 会话的 best-effort 软护栏（防模型未核对
    即提交），crash 恢复 replay 到已记录的 submit 终局时直接落库，不重放 veto
    （会话内「已拦一次/剩余轮次」状态在崩溃中不可靠）。终局仍统一经
    normalize_stop_reason + handle_terminal 归一化落库。
    """
    run_id = "run-term-no-veto"
    _write_seed(dbm, run_id)
    submit_input = {"subject_id": "2", "reason": "best match"}
    _write_llm_chat(
        dbm,
        run_id,
        0,
        [{"id": "t9", "name": "submit_suggestion", "input": submit_input}],
        stop_reason="tool_use",
    )
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    assert len(hooks.terminal_calls) == 1
    assert hooks.veto_calls == [], "恢复路径不重放 veto 软护栏"


# ---------------------------------------------------------------------------
# stop_reason 终局归一化：与 loop.run 判据对齐（空壳/max_tokens/未知有内容）
# ---------------------------------------------------------------------------


def _assert_failed_with_stop(dbm: DatabaseManager, run_id: str, stop: str) -> None:
    run = dbm.agent_runs.get_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["stop_reason"] == stop


def test_continue_run_empty_shell_response_marks_failed_llm_error(
    dbm: DatabaseManager,
):
    """空壳响应（stop_reason=\"\" 且无 blocks/content）→ 不再空转调 LLM，落 llm_error。

    与 loop.run 归一化一致：空壳不是 end_turn，也不应被当作「待补执行工具轮」
    反复续跑，而是按 llm_error 失败终态处理。
    """
    run_id = "run-empty-shell"
    _write_seed(dbm, run_id)
    _write_llm_chat(dbm, run_id, 0, [], stop_reason="", content="")
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    assert len(hooks.terminal_calls) == 1
    result = hooks.terminal_calls[0][0][1]
    assert result.stop_reason == "llm_error"
    _assert_failed_with_stop(dbm, run_id, "llm_error")


def test_continue_run_max_tokens_response_marks_failed_max_tokens(
    dbm: DatabaseManager,
):
    """max_tokens 无工具调用 → 终态 max_tokens（failed），不再续跑。"""
    run_id = "run-max-tokens"
    _write_seed(dbm, run_id)
    _write_llm_chat(dbm, run_id, 0, [], stop_reason="max_tokens", content="partial")
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    assert len(hooks.terminal_calls) == 1
    result = hooks.terminal_calls[0][0][1]
    assert result.stop_reason == "max_tokens"
    _assert_failed_with_stop(dbm, run_id, "max_tokens")


def test_continue_run_unknown_stop_reason_with_content_is_end_turn(
    dbm: DatabaseManager,
):
    """未知 stop_reason 但有内容（无工具调用）→ 按 loop.run 兼容判据落 end_turn。"""
    run_id = "run-unknown-content"
    _write_seed(dbm, run_id)
    _write_llm_chat(
        dbm, run_id, 0, [], stop_reason="stop_sequence", content="我给出结论"
    )
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    run = dbm.agent_runs.get_run(run_id)
    assert run is not None
    assert run["status"] == "succeeded"
    assert run["stop_reason"] == "end_turn"
    # end_turn 不经 handle_terminal（与既有直落 mark_succeeded 语义一致）
    assert hooks.terminal_calls == []


def test_continue_run_end_turn_still_marks_succeeded(
    dbm: DatabaseManager,
):
    """正常 end_turn 路径零回归：直接 mark_succeeded，不调 LLM/不 handle_terminal。"""
    run_id = "run-end-turn"
    _write_seed(dbm, run_id)
    _write_llm_chat(dbm, run_id, 0, [], stop_reason="end_turn", content="无建议")
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=_hooks_of(hooks), ctx=None))

    run = dbm.agent_runs.get_run(run_id)
    assert run is not None
    assert run["status"] == "succeeded"
    assert run["stop_reason"] == "end_turn"
    assert hooks.terminal_calls == []

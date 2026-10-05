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

import pytest

from app.core.database import DatabaseManager, set_database_manager
from app.services.agent import runtime, trace


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
    dbm: DatabaseManager, run_id: str, iteration: int, tool_calls, stop_reason: str
) -> None:
    span_id = trace.start_span(run_id, "llm_chat", iteration, 0)
    trace.end_span(
        span_id,
        replay_delta={
            "response": {
                "stop_reason": stop_reason,
                "content": "",
                "tool_calls": tool_calls,
            }
        },
    )


class _FakeHooks:
    task_type = "match"
    terminal_tool = "submit_suggestion"

    def __init__(self) -> None:
        self.terminal_calls: list[tuple[tuple, dict]] = []

    def resolve_thinking_level(self) -> str:
        return "medium"

    def resolve_max_iterations(self, level: str) -> int:
        return 10

    async def handle_terminal(self, *args, **kwargs):
        self.terminal_calls.append((args, kwargs))
        return "succeeded"


def test_continue_run_terminal_stop_without_tool_call_degrades_to_exhausted(
    dbm: DatabaseManager, log_records
):
    """stop_reason=终止工具名但 tool_calls 为空 → 降级 exhausted，不提交空建议。"""
    run_id = "run-term-empty"
    _write_seed(dbm, run_id)
    _write_llm_chat(dbm, run_id, 0, [], stop_reason="submit_suggestion")
    hooks = _FakeHooks()

    asyncio.run(runtime.continue_run(run_id, hooks=hooks, ctx=None))

    run = dbm.agent_runs.get_run(run_id)
    assert run is not None
    assert run["status"] == "no_suggestion"
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

    asyncio.run(runtime.continue_run(run_id, hooks=hooks, ctx=None))

    run = dbm.agent_runs.get_run(run_id)
    assert run is not None
    assert run["status"] == "no_suggestion"
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

    asyncio.run(runtime.continue_run(run_id, hooks=hooks, ctx=None))

    assert len(hooks.terminal_calls) == 1
    args, _kwargs = hooks.terminal_calls[0]
    result = args[2]
    assert result.suggestion == submit_input
    assert result.stop_reason == "submit_suggestion"

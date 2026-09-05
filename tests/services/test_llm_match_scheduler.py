"""LLM 匹配调度器测试（恢复 + 去重 + 清理）

通过 mock repo / llm_assist / config_manager 验证调度器行为，不触碰真实 DB 与 LLM。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.agent.trace import ReplayResult
from app.services.llm.models import ChatResponse, Message, ToolResultBlock, ToolUseBlock
from app.services.llm.tools import ToolDefinition, ToolRegistry
from app.services.llm_match_scheduler import LlmMatchScheduler
from app.services.matching import llm_assist

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_config(enabled: bool = True, api_key: str = "k", cron: str = "*/2 * * * *"):
    """构造 config_manager mock：get 按 key 返回，get_llm_config 返回 api_key。"""
    cm = MagicMock()

    def _get(section, key, fallback=None):
        if key == "llm_match_assist":
            return "true" if enabled else "false"
        if key == "llm_match_cron":
            return cron
        # retention_days / recovery_timeout_s 等：返回 fallback 以保证解析安全
        return fallback

    cm.get.side_effect = _get
    cm.get_llm_config.return_value = {"api_key": api_key}
    return cm


def _make_repo() -> MagicMock:
    """构造 agent_runs repo mock（各方法默认返回安全值）。"""
    repo = MagicMock()
    repo.cleanup_terminal.return_value = 0
    repo.list_stale_processing.return_value = []
    repo.list_pending.return_value = []
    repo.increment_attempts.return_value = 1
    repo.refresh_started_at.return_value = True
    return repo


def _make_dbm(repo: MagicMock) -> MagicMock:
    dbm = MagicMock()
    dbm.agent_runs = repo
    return dbm


# ---------------------------------------------------------------------------
# 启用条件
# ---------------------------------------------------------------------------


def test_is_enabled_switch_off_returns_false():
    sched = LlmMatchScheduler()
    cm = _make_config(enabled=False, api_key="")
    with patch("app.services.llm_match_scheduler.config_manager", cm):
        assert sched._is_enabled() is False


def test_is_enabled_switch_on_and_llm_present_returns_true():
    sched = LlmMatchScheduler()
    cm = _make_config(enabled=True, api_key="sk-xxx")
    with patch("app.services.llm_match_scheduler.config_manager", cm):
        assert sched._is_enabled() is True


def test_is_enabled_switch_on_but_llm_missing_returns_false_and_logs():
    sched = LlmMatchScheduler()
    cm = _make_config(enabled=True, api_key="")
    log = MagicMock()
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch("app.services.llm_match_scheduler.logger", log),
    ):
        assert sched._is_enabled() is False
    log.info.assert_called_with("LLM 配置缺失，匹配增强已禁用")


def test_get_driver_config_returns_cron():
    sched = LlmMatchScheduler()
    cm = _make_config(cron="*/5 * * * *")
    with patch("app.services.llm_match_scheduler.config_manager", cm):
        cfg = sched._get_driver_config()
    assert cfg["sync_interval"] == "*/5 * * * *"


def test_disabled_run_sync_job_returns_early():
    sched = LlmMatchScheduler()
    cm = _make_config(enabled=False)
    repo = _make_repo()
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())
    # 禁用时不应做任何 repo 操作
    repo.cleanup_terminal.assert_not_called()
    repo.list_pending.assert_not_called()


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


def test_cleanup_deletes_expired_terminal_and_logs():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    repo.cleanup_terminal.return_value = 3
    log = MagicMock()
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch("app.services.llm_match_scheduler.logger", log),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())
    repo.cleanup_terminal.assert_called_once()
    log.info.assert_any_call("🤖 清理终态过期 agent_run 3 条")


def test_cleanup_no_log_when_nothing_expired():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    repo.cleanup_terminal.return_value = 0
    log = MagicMock()
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch("app.services.llm_match_scheduler.logger", log),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())
    repo.cleanup_terminal.assert_called_once()
    # 删除数为 0 时不打清理日志
    for call in log.info.call_args_list:
        assert "清理终态过期 agent_run" not in call.args[0]


# ---------------------------------------------------------------------------
# 恢复扫描
# ---------------------------------------------------------------------------


def test_recovery_missing_sync_record_marks_failed():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    repo.list_stale_processing.return_value = [{"run_id": "r1", "sync_record_id": 42}]
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value=None),
        patch.object(sched, "_continue_replay", new=AsyncMock()) as cont,
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())

    repo.refresh_started_at.assert_called_once_with("r1")
    repo.mark_failed.assert_called_once_with(
        "r1", stop_reason="error", last_error="sync_record missing"
    )
    cont.assert_not_awaited()


def test_recovery_present_sync_record_continues():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    repo.list_stale_processing.return_value = [{"run_id": "r1", "sync_record_id": 42}]
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value={"id": 42, "title": "x"}),
        patch.object(sched, "_continue_replay", new=AsyncMock()) as cont,
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())

    repo.refresh_started_at.assert_called_once_with("r1")
    cont.assert_awaited_once()


# ---------------------------------------------------------------------------
# 正常处理
# ---------------------------------------------------------------------------


def test_process_pending_calls_llm_assist_run_per_item():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    runs = [
        {"run_id": "a", "sync_record_id": 1},
        {"run_id": "b", "sync_record_id": 2},
        {"run_id": "c", "sync_record_id": 3},
    ]
    repo.list_pending.return_value = runs
    run_mock = AsyncMock(return_value="succeeded")
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value={"id": 1, "title": "t"}),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.llm_match_scheduler.llm_assist_module.run", run_mock),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())

    assert run_mock.await_count == 3


def test_process_pending_respects_batch_limit_five():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    # 即便 repo 返回 7 条，调度器也只处理 5 条
    repo.list_pending.return_value = [
        {"run_id": f"r{i}", "sync_record_id": i} for i in range(7)
    ]
    run_mock = AsyncMock(return_value="succeeded")
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value={"id": 1, "title": "t"}),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.llm_match_scheduler.llm_assist_module.run", run_mock),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())

    repo.list_pending.assert_called_once_with(limit=5)
    assert run_mock.await_count == 5


def test_run_exception_increments_attempts():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    repo.list_pending.return_value = [{"run_id": "a", "sync_record_id": 1}]
    run_mock = AsyncMock(side_effect=Exception("boom"))
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value={"id": 1, "title": "t"}),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.llm_match_scheduler.llm_assist_module.run", run_mock),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())

    repo.increment_attempts.assert_called_once_with("a")


def test_run_exception_attempts_reach_three_marks_failed():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    repo.list_pending.return_value = [{"run_id": "a", "sync_record_id": 1}]
    repo.increment_attempts.return_value = 3
    run_mock = AsyncMock(side_effect=Exception("boom"))
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value={"id": 1, "title": "t"}),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.llm_match_scheduler.llm_assist_module.run", run_mock),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())

    repo.increment_attempts.assert_called_once_with("a")
    repo.mark_failed.assert_called_once()


def test_exception_in_one_run_does_not_stop_others():
    sched = LlmMatchScheduler()
    cm = _make_config()
    repo = _make_repo()
    runs = [
        {"run_id": "a", "sync_record_id": 1},
        {"run_id": "b", "sync_record_id": 2},
    ]
    repo.list_pending.return_value = runs
    # 第一条抛错，第二条成功；两条都应被处理
    run_mock = AsyncMock(side_effect=[Exception("boom"), "succeeded"])
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value={"id": 1, "title": "t"}),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.llm_match_scheduler.llm_assist_module.run", run_mock),
    ):
        import asyncio

        asyncio.run(sched._run_sync_job())

    # 两条都尝试处理（第一条异常被捕获后仍继续）
    assert run_mock.await_count == 2
    repo.increment_attempts.assert_called_once_with("a")


# ---------------------------------------------------------------------------
# F5：正常处理路径透传 thinking_level（统一从集中配置读取）
# ---------------------------------------------------------------------------


def test_process_run_passes_thinking_level_from_config():
    sched = LlmMatchScheduler()
    repo = _make_repo()
    run = {"run_id": "a", "sync_record_id": 1}

    cm = MagicMock()
    cm.get_sync_llm_match_config.return_value = {
        "llm_match_thinking_level": "high",
        "llm_match_max_iterations": "",
    }
    run_mock = AsyncMock(return_value="succeeded")
    with (
        patch("app.services.llm_match_scheduler.config_manager", cm),
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_get_sync_record", return_value={"id": 1, "title": "t"}),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.llm_match_scheduler.llm_assist_module.run", run_mock),
    ):
        asyncio.run(sched._process_run(run))

    run_mock.assert_awaited_once()
    _, kwargs = run_mock.call_args
    assert kwargs.get("thinking_level") == "high"


# ---------------------------------------------------------------------------
# F2：断点恢复消费 last_response（不 mock _continue_replay 本身；trace.replay 可 mock）
# ---------------------------------------------------------------------------


def test_continue_replay_end_turn_dispatches_without_llm():
    """last_response=end_turn → 直接 mark_no_suggestion，不调 LLM（loop_run）。"""
    sched = LlmMatchScheduler()
    repo = _make_repo()
    rr = ReplayResult(
        messages=[Message(role="system", content="s")],
        executed_iterations=1,
        missing_tool_calls=[],
        last_response={"stop_reason": "end_turn", "content": "x", "tool_calls": []},
    )
    loop = AsyncMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.llm_match_scheduler.config_manager") as cm,
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.agent.loop.run", loop),
    ):
        cm.get_sync_llm_match_config.return_value = {
            "llm_match_thinking_level": "medium",
            "llm_match_max_iterations": "",
        }
        asyncio.run(
            sched._continue_replay({"run_id": "r", "sync_record_id": 1}, {"id": 1})
        )

    repo.mark_no_suggestion.assert_called_once_with("r", stop_reason="end_turn")
    loop.assert_not_awaited()


def test_continue_replay_submit_suggestion_dispatches_to_handle_result():
    """last_response=submit_suggestion → 捕获参数走校验落库路径（_handle_result）。"""
    sched = LlmMatchScheduler()
    handle = MagicMock()
    rr = ReplayResult(
        messages=[Message(role="system", content="s")],
        executed_iterations=1,
        missing_tool_calls=[],
        last_response={
            "stop_reason": "submit_suggestion",
            "content": "",
            "tool_calls": [
                {
                    "id": "s1",
                    "name": "submit_suggestion",
                    "input": {"subject_id": "123", "reason": "跨季匹配"},
                }
            ],
        },
    )
    loop = AsyncMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.llm_match_scheduler.config_manager") as cm,
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(_make_repo()),
        ),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.agent.loop.run", loop),
        patch(
            "app.services.llm_match_scheduler.llm_assist_module._handle_result", handle
        ),
    ):
        cm.get_sync_llm_match_config.return_value = {
            "llm_match_thinking_level": "medium",
            "llm_match_max_iterations": "",
        }
        asyncio.run(
            sched._continue_replay({"run_id": "r", "sync_record_id": 1}, {"id": 1})
        )

    handle.assert_called_once()
    result_arg = handle.call_args[0][2]  # _handle_result(dbm, run_id, result, ...)
    assert result_arg.stop_reason == "submit_suggestion"
    assert result_arg.suggestion == {"subject_id": "123", "reason": "跨季匹配"}
    loop.assert_not_awaited()


def test_continue_replay_tool_use_backfills_and_continues_loop():
    """last_response 含 tool_use（缺失工具）→ 补执行缺失工具 + 回填后继续 loop_run。"""
    sched = LlmMatchScheduler()
    missing = {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}}
    rr = ReplayResult(
        messages=[Message(role="system", content="s")],
        executed_iterations=0,
        missing_tool_calls=[missing],
        last_response={
            "stop_reason": "tool_use",
            "content": "go",
            "tool_calls": [missing],
        },
    )
    backfill = AsyncMock()
    loop = AsyncMock()
    from app.services.agent.loop import RunResult

    loop.return_value = RunResult(stop_reason="end_turn")
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.llm_match_scheduler.config_manager") as cm,
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(_make_repo()),
        ),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.agent.loop.run", loop),
        patch.object(sched, "_replay_missing_tool", backfill),
    ):
        cm.get_sync_llm_match_config.return_value = {
            "llm_match_thinking_level": "medium",
            "llm_match_max_iterations": "",
        }
        asyncio.run(
            sched._continue_replay({"run_id": "r", "sync_record_id": 1}, {"id": 1})
        )

    # 缺失工具补执行一次
    backfill.assert_awaited_once()
    # 回填后进入下一轮 loop_run 续跑（1 次 LLM 调用）
    loop.assert_awaited_once()


# ---------------------------------------------------------------------------
# G3：恢复路径 config_override 非正数 → 回退 None（由策略默认接管）
# ---------------------------------------------------------------------------


def _replay_stub_end_turn() -> ReplayResult:
    return ReplayResult(
        messages=[Message(role="system", content="s")],
        executed_iterations=0,
        missing_tool_calls=[],
        last_response={"stop_reason": "end_turn", "content": "x", "tool_calls": []},
    )


def _run_continue_replay_with_config(raw_max: str):
    """以指定 llm_match_max_iterations 跑一次 _continue_replay，返回捕获的 config_override。"""
    sched = LlmMatchScheduler()
    captured = {}

    def _gmi(task_type, thinking_level, config_override=None):
        captured["config_override"] = config_override
        return 3

    log = MagicMock()
    with (
        patch("app.services.agent.trace.replay", return_value=_replay_stub_end_turn()),
        patch("app.services.agent.budget.get_max_iterations", _gmi),
        patch("app.services.llm_match_scheduler.config_manager") as cm,
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(_make_repo()),
        ),
        patch("app.services.llm_match_scheduler.logger", log),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
    ):
        cm.get_sync_llm_match_config.return_value = {
            "llm_match_thinking_level": "medium",
            "llm_match_max_iterations": raw_max,
        }
        asyncio.run(
            sched._continue_replay({"run_id": "r", "sync_record_id": 1}, {"id": 1})
        )
    return captured.get("config_override"), log


def test_continue_replay_zero_config_override_falls_back_to_none():
    override, log = _run_continue_replay_with_config("0")
    assert override is None, "0 应回退 None（避免 max_iterations=0 空跑）"
    assert log.warning.called


def test_continue_replay_negative_config_override_falls_back_to_none():
    override, log = _run_continue_replay_with_config("-3")
    assert override is None, "负值应回退 None"
    assert log.warning.called


def test_continue_replay_positive_config_override_is_passed_through():
    override, _ = _run_continue_replay_with_config("7")
    assert override == 7


# ---------------------------------------------------------------------------
# G4：tool_use 补执行后 remaining<=0 → 必须标记终态（no_suggestion/exhausted），
# 不得直接 return 让 run 滞留 processing
# ---------------------------------------------------------------------------


def test_continue_replay_tool_use_no_remaining_marks_no_suggestion():
    sched = LlmMatchScheduler()
    repo = _make_repo()
    missing = {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}}
    # medium → max_iterations=3；executed=2 → remaining=1；补执行后 -1 → 0
    rr = ReplayResult(
        messages=[Message(role="system", content="s")],
        executed_iterations=2,
        missing_tool_calls=[missing],
        last_response={
            "stop_reason": "tool_use",
            "content": "go",
            "tool_calls": [missing],
        },
    )
    loop = AsyncMock()
    backfill = AsyncMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.llm_match_scheduler.config_manager") as cm,
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch.object(sched, "_replay_missing_tool", backfill),
        patch("app.services.agent.loop.run", loop),
    ):
        cm.get_sync_llm_match_config.return_value = {
            "llm_match_thinking_level": "medium",
            "llm_match_max_iterations": "",
        }
        asyncio.run(
            sched._continue_replay({"run_id": "r", "sync_record_id": 1}, {"id": 1})
        )

    # 不再续跑 loop，但必须落终态（否则 run 永久 processing）
    loop.assert_not_awaited()
    repo.mark_no_suggestion.assert_called_once_with("r", stop_reason="exhausted")


def test_continue_replay_no_remaining_before_replay_marks_no_suggestion():
    """replay 后剩余轮次已耗尽（remaining<=0）同样必须落终态。"""
    sched = LlmMatchScheduler()
    repo = _make_repo()
    rr = ReplayResult(
        messages=[Message(role="system", content="s")],
        executed_iterations=3,  # medium=3 → remaining=0
        missing_tool_calls=[],
        last_response=None,
    )
    loop = AsyncMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.llm_match_scheduler.config_manager") as cm,
        patch(
            "app.services.llm_match_scheduler.get_database_manager",
            return_value=_make_dbm(repo),
        ),
        patch.object(sched, "_build_bgm", return_value=MagicMock()),
        patch("app.services.agent.loop.run", loop),
    ):
        cm.get_sync_llm_match_config.return_value = {
            "llm_match_thinking_level": "medium",
            "llm_match_max_iterations": "",
        }
        asyncio.run(
            sched._continue_replay({"run_id": "r", "sync_record_id": 1}, {"id": 1})
        )

    loop.assert_not_awaited()
    repo.mark_no_suggestion.assert_called_once_with("r", stop_reason="exhausted")


# ---------------------------------------------------------------------------
# F4：缺失工具补执行结果回填 messages（assistant 后有对应 tool_result）
# ---------------------------------------------------------------------------


def test_replay_missing_tool_appends_tool_result_to_messages():
    sched = LlmMatchScheduler()

    def _handler(args):
        return "SEARCH-RESULT"

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="get_subject_detail",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            access="read",
        )
    )

    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="ctx"),
        # assistant：仅含 1 个 tool_use t2（t1 已记录在前）
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="t2", name="get_subject_detail", input={"subject_id": "2"}
                )
            ],
        ),
        # 已记录的 tool_result（t1）
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="t1", content="r1", is_error=False)],
        ),
    ]

    def _count_tool_results(msgs):
        return sum(
            1
            for m in msgs
            if m.role == "user"
            and isinstance(m.content, list)
            and any(isinstance(b, ToolResultBlock) for b in m.content)
        )

    before = _count_tool_results(messages)
    # assistant 的 tool_use 数量（应等于回填后 tool_result 数量）
    assistant_tool_use = sum(
        len(m.content)
        for m in messages
        if m.role == "assistant" and isinstance(m.content, list)
    )
    asyncio.run(
        sched._replay_missing_tool(
            {"id": "t2", "name": "get_subject_detail", "input": {"subject_id": "2"}},
            registry,
            messages,
        )
    )

    after = _count_tool_results(messages)
    # 回填新增 1 条 tool_result → 数量与 assistant 的 tool_use 数量匹配
    assert (
        after == before + 1 == assistant_tool_use + 1 - 0
    )  # t2 补齐后总数=assistant tool_use(1)+原记录(1)
    assert after == 2  # t1（已记录）+ t2（补执行）
    trs = [
        m.content[0]
        for m in messages
        if m.role == "user"
        and isinstance(m.content, list)
        and any(isinstance(b, ToolResultBlock) for b in m.content)
    ]
    assert any(t.tool_use_id == "t2" and t.content == "SEARCH-RESULT" for t in trs), (
        "补执行的 tool_result 应对应缺失的 tool_use t2"
    )


# ---------------------------------------------------------------------------
# G5：非 read（write/terminal/未注册）缺失工具 → 回填占位 tool_result 闭合协议
# （不重放副作用，但必须让每条 tool_use 都有对应 tool_result）
# ---------------------------------------------------------------------------

_SKIP_PLACEHOLDER = "skipped: will be re-invoked in continuation"


def _last_tool_result(messages: list) -> ToolResultBlock | None:
    for m in reversed(messages):
        if m.role == "user" and isinstance(m.content, list) and m.content:
            blk = m.content[0]
            if isinstance(blk, ToolResultBlock):
                return blk
    return None


def _registry_with(name: str, access: str, called: list) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=name,
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: called.append(name) or "X",
            access=access,
        )
    )
    return registry


def test_replay_missing_write_tool_appends_placeholder_tool_result():
    sched = LlmMatchScheduler()
    called: list = []
    registry = _registry_with("write_mapping", "write", called)
    messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="w1", name="write_mapping", input={})],
        )
    ]

    asyncio.run(
        sched._replay_missing_tool(
            {"id": "w1", "name": "write_mapping", "input": {}}, registry, messages
        )
    )

    # 写工具不重放（避免重复副作用）
    assert called == []
    blk = _last_tool_result(messages)
    assert blk is not None, "write 缺失工具也必须回填 tool_result 闭合协议"
    assert blk.tool_use_id == "w1"
    assert blk.is_error is False
    assert blk.content == _SKIP_PLACEHOLDER


def test_replay_missing_terminal_tool_appends_placeholder_tool_result():
    sched = LlmMatchScheduler()
    called: list = []
    registry = _registry_with("submit_suggestion", "terminal", called)
    messages: list = []

    asyncio.run(
        sched._replay_missing_tool(
            {"id": "s1", "name": "submit_suggestion", "input": {"subject_id": "1"}},
            registry,
            messages,
        )
    )

    assert called == []
    blk = _last_tool_result(messages)
    assert blk is not None
    assert blk.tool_use_id == "s1"
    assert blk.content == _SKIP_PLACEHOLDER
    assert blk.is_error is False


def test_replay_missing_unregistered_tool_appends_placeholder_tool_result():
    sched = LlmMatchScheduler()
    registry = ToolRegistry()
    messages: list = []

    asyncio.run(
        sched._replay_missing_tool(
            {"id": "u1", "name": "ghost_tool", "input": {}}, registry, messages
        )
    )

    blk = _last_tool_result(messages)
    assert blk is not None, "未注册工具同样需回填占位，避免 tool_use 悬空"
    assert blk.tool_use_id == "u1"
    assert blk.content == _SKIP_PLACEHOLDER


# ---------------------------------------------------------------------------
# 端到端断点恢复（不 mock _continue_replay / loop_run，真实驱动 trace.replay）
# 累计 LLM.chat 调用次数 = 2（崩溃前 1 + 恢复后 1）
# ---------------------------------------------------------------------------


def test_recover_end_to_end_no_double_llm_call_m22(monkeypatch):
    from app.core.database import database_manager, set_database_manager
    from app.services.llm.tools import ToolResultBlock, get_tool_registry

    set_database_manager(database_manager)

    run_id = "run-m22"
    sr_id = 99
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = {
        "id": sr_id,
        "title": f"标题{sr_id}",
        "ori_title": "花咲くいろは",
        "season": 1,
        "episode": 0,
        "media_type": "episode",
        "release_date": "2012",
        "user_name": "alice",
        "source": "plex",
        "match_trace": {
            "steps": [{"stage": "api_search", "status": "miss", "candidates": []}]
        },
    }

    # 共享 chat spy：首次（崩溃前）返回 tool_use，恢复时返回 end_turn → 累计 2 次
    spy_calls = {"n": 0}

    async def _chat(messages, *, tools=None, tool_choice=None, job_name=None):
        spy_calls["n"] += 1
        if spy_calls["n"] == 1:
            return ChatResponse(
                content="",
                stop_reason="tool_use",
                blocks=[
                    ToolUseBlock(id="t1", name="search_bangumi", input={"title": "foo"})
                ],
            )
        return ChatResponse(content="done", stop_reason="end_turn")

    chat = AsyncMock(side_effect=_chat)
    client = MagicMock()
    client.chat = chat
    monkeypatch.setattr("app.services.llm.get_llm_client", lambda: client)

    class _Bgm:
        def search(self, **kwargs):
            return [{"id": 1, "name": "foo"}]

        def get_subject(self, sid):
            return {"name": f"subject-{sid}", "name_cn": f"条目-{sid}"}

        def get_related_subjects(self, sid):
            return []

    bgm = _Bgm()
    sched = LlmMatchScheduler()
    monkeypatch.setattr(sched, "_build_bgm", lambda s: bgm)

    # execute_batch：初始调用（崩溃前）抛错模拟崩溃；恢复路径不再调用
    reg = get_tool_registry()
    eb_calls = {"n": 0}

    async def _eb(tool_calls):
        eb_calls["n"] += 1
        if eb_calls["n"] == 1:
            raise RuntimeError("crash mid-exec")
        return {
            tc.id: ToolResultBlock(tool_use_id=tc.id, content="ok", is_error=False)
            for tc in tool_calls
        }

    monkeypatch.setattr(reg, "execute_batch", _eb)

    async def _go():
        # 初始正常运行至崩溃（记录 1 条 llm_chat span）
        await llm_assist.run(run_id, sync_record=sr, bgm=bgm)
        # 恢复续跑：真实 trace.replay + 真实 loop_run（仅 _continue_replay 不 mock）
        await sched._continue_replay({"run_id": run_id, "sync_record_id": sr_id}, sr)

    asyncio.run(_go())

    # 累计 LLM.chat 调用 = 2（崩溃前 1 + 恢复后 1），验证恢复未重复重调 LLM
    assert chat.call_count == 2, f"期望累计 2 次 LLM 调用，实际 {chat.call_count}"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "no_suggestion"

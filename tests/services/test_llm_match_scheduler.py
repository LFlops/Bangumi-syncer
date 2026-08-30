"""T12: LLM 匹配调度器测试（M8/M9/M10 + 恢复 + 去重 + 清理）

通过 mock repo / llm_assist / config_manager 验证调度器行为，不触碰真实 DB 与 LLM。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.llm_match_scheduler import LlmMatchScheduler

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
# 清理（M10）
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
# 恢复扫描（D18）
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

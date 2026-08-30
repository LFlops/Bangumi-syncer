"""匹配增强接入 + confirm/reject 联动集成测试（Task T14 / 场景 M1-M4/M11/M12/M12b/M32/M33）

覆盖：
- M1：开关开 + LLM 可用 → 失败落 agent_runs(pending) + trace 补 llm_assist step（persist 前）
- M2：无候选也落任务
- M3：开关关 → 原失败逻辑完全不变（无 agent_runs 调用、无 trace step）
- M4：LLM 配置缺失 → 不落任务 + 日志含 "LLM 配置缺失"
- 去重：同 key 活跃 → 跳过；failed 且 total_attempts<=10 → 重新入队；>10 → 不重入
- M11：confirm（从候选列表外确认建议）→ 写映射 + 补发 + mark_applied
- M12b：无关联 agent_runs（开关关流程）→ confirm/reject no-op 不报错
- M33：不带 llm_subject_id 的既有 confirm → 原逻辑闭环
- API：confirm 端点接收可选 llm_subject_id 并优先使用
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.models.sync import CustomItem
from app.services.sync_service import SyncService
from app.services.sync_service.match_trace import MatchTrace
from app.services.sync_service.orchestrator import SyncOrchestrator

# ----------------------------------------------------------------------
# 测试夹具
# ----------------------------------------------------------------------


def _make_item() -> CustomItem:
    return CustomItem(
        media_type="episode",
        title="测试番剧",
        season=1,
        episode=1,
        release_date="2024",
        user_name="tester",
        sync_action="mark_watching",
    )


def _make_trace(with_candidates: bool = False) -> MatchTrace:
    trace = MatchTrace(
        request_title="测试番剧",
        request_ori_title="",
        request_season=1,
        request_episode=1,
    )
    if with_candidates:
        from app.services.sync_service.match_trace import MatchCandidate

        step = trace.start_step("api_search")
        step.status = "miss"
        step.candidates = [MatchCandidate(subject_id="111", name="某番")]
    return trace


def _make_orchestrator() -> SyncOrchestrator:
    sync = MagicMock()
    sync._format_subject_not_found_message.return_value = "未找到匹配的番剧"
    return SyncOrchestrator(sync)


# ----------------------------------------------------------------------
# M1 / M2 / M3 / M4 + 去重：_handle_match_failure 接入
# ----------------------------------------------------------------------


@pytest.mark.parametrize("with_candidates", [True, False])
@patch("app.services.sync_service.notification_service")
@patch("app.services.sync_service.config_manager")
@patch("app.services.sync_service.database_manager")
def test_handle_match_failure_enqueues_when_enabled(
    mock_db, mock_cfg, mock_notify, with_candidates
):
    """M1/M2：开关开 + LLM 可用 → 落 agent_runs(pending) + trace 含 llm_assist step

    无候选（M2）也落任务；trace step 必须在 _persist_sync_record 之前（persist 被
    mock，其序列化结果即被视为 persist 时刻的 trace 状态）。
    """
    orch = _make_orchestrator()
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = None
    agent_runs.find_failed_by_sync_record.return_value = None
    agent_runs.create_pending.return_value = 1
    mock_cfg.get.return_value = "true"
    mock_cfg.get_llm_config.return_value = {
        "api_key": "sk-test",
        "provider": "openai_compat",
    }
    mock_db.agent_runs = agent_runs

    item = _make_item()
    trace = _make_trace(with_candidates=with_candidates)
    captured: dict = {}

    def fake_persist(t, *a, **k):
        captured["steps"] = t.to_dict()["steps"]
        return 123

    with patch.object(orch, "_persist_sync_record", side_effect=fake_persist):
        orch._handle_match_failure(item, "plex", trace, "err", [""])

    agent_runs.create_pending.assert_called_once()
    kwargs = agent_runs.create_pending.call_args.kwargs
    assert kwargs["task_type"] == "match"
    assert kwargs["sync_record_id"] == 123
    assert kwargs["run_id"]
    # trace step 在 persist 前已存在
    assert any(
        s["stage"] == "llm_assist" and s["status"] == "pending"
        for s in captured["steps"]
    )


@patch("app.services.sync_service.notification_service")
@patch("app.services.sync_service.config_manager")
@patch("app.services.sync_service.database_manager")
def test_handle_match_failure_switch_off_no_enqueue(mock_db, mock_cfg, mock_notify):
    """M3：开关关 → 原失败逻辑完全不变（无 agent_runs 调用、无 trace step）"""
    orch = _make_orchestrator()
    agent_runs = MagicMock()
    mock_cfg.get.return_value = "false"
    mock_cfg.get_llm_config.return_value = {"api_key": "sk-test"}
    mock_db.agent_runs = agent_runs

    item = _make_item()
    trace = _make_trace(with_candidates=True)

    with patch.object(orch, "_persist_sync_record", return_value=123):
        orch._handle_match_failure(item, "plex", trace, "err", [""])

    agent_runs.create_pending.assert_not_called()
    agent_runs.find_active_by_sync_record.assert_not_called()
    assert not any(s["stage"] == "llm_assist" for s in trace.to_dict()["steps"])


@patch("app.services.sync_service.notification_service")
@patch("app.services.sync_service.config_manager")
@patch("app.services.sync_service.database_manager")
def test_handle_match_failure_llm_missing_no_enqueue(
    mock_db, mock_cfg, mock_notify, capsys
):
    """M4：LLM 配置缺失 → 不落任务 + 日志含 'LLM 配置缺失'"""
    orch = _make_orchestrator()
    agent_runs = MagicMock()
    mock_cfg.get.return_value = "true"
    mock_cfg.get_llm_config.return_value = {"api_key": ""}
    mock_db.agent_runs = agent_runs

    item = _make_item()
    trace = _make_trace(with_candidates=True)

    with patch.object(orch, "_persist_sync_record", return_value=123):
        orch._handle_match_failure(item, "plex", trace, "err", [""])

    agent_runs.create_pending.assert_not_called()
    assert "LLM 配置缺失" in capsys.readouterr().out


@patch("app.services.sync_service.notification_service")
@patch("app.services.sync_service.config_manager")
@patch("app.services.sync_service.database_manager")
def test_dedup_active_pending_skips(mock_db, mock_cfg, mock_notify, capsys):
    """去重：同 key 已有活跃(pending)记录 → 跳过创建 + 日志"""
    orch = _make_orchestrator()
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = {
        "run_id": "r0",
        "status": "pending",
    }
    agent_runs.find_failed_by_sync_record.return_value = None
    mock_cfg.get.return_value = "true"
    mock_cfg.get_llm_config.return_value = {"api_key": "sk"}
    mock_db.agent_runs = agent_runs

    item = _make_item()
    trace = _make_trace()

    with patch.object(orch, "_persist_sync_record", return_value=123):
        orch._handle_match_failure(item, "plex", trace, "err", [""])

    agent_runs.create_pending.assert_not_called()
    agent_runs.requeue_failed.assert_not_called()
    assert "已存在" in capsys.readouterr().out


@patch("app.services.sync_service.notification_service")
@patch("app.services.sync_service.config_manager")
@patch("app.services.sync_service.database_manager")
def test_dedup_failed_requeue_when_within_limit(mock_db, mock_cfg, mock_notify):
    """去重：failed 且 total_attempts<=10 → 重新入队复用，不新建"""
    orch = _make_orchestrator()
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = None
    agent_runs.find_failed_by_sync_record.return_value = {
        "run_id": "r2",
        "total_attempts": 3,
    }
    mock_cfg.get.return_value = "true"
    mock_cfg.get_llm_config.return_value = {"api_key": "sk"}
    mock_db.agent_runs = agent_runs

    item = _make_item()
    trace = _make_trace()

    with patch.object(orch, "_persist_sync_record", return_value=123):
        orch._handle_match_failure(item, "plex", trace, "err", [""])

    agent_runs.requeue_failed.assert_called_once_with("r2")
    agent_runs.create_pending.assert_not_called()


@patch("app.services.sync_service.notification_service")
@patch("app.services.sync_service.config_manager")
@patch("app.services.sync_service.database_manager")
def test_dedup_failed_exceeds_limit_no_requeue(mock_db, mock_cfg, mock_notify, capsys):
    """去重：failed 且 total_attempts>10 → 不重新入队、不新建"""
    orch = _make_orchestrator()
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = None
    agent_runs.find_failed_by_sync_record.return_value = {
        "run_id": "r3",
        "total_attempts": 11,
    }
    mock_cfg.get.return_value = "true"
    mock_cfg.get_llm_config.return_value = {"api_key": "sk"}
    mock_db.agent_runs = agent_runs

    item = _make_item()
    trace = _make_trace()

    with patch.object(orch, "_persist_sync_record", return_value=123):
        orch._handle_match_failure(item, "plex", trace, "err", [""])

    agent_runs.requeue_failed.assert_not_called()
    agent_runs.create_pending.assert_not_called()
    assert "重试超限" in capsys.readouterr().out


# ----------------------------------------------------------------------
# M11 / M12b / M33：confirm / reject 联动
# ----------------------------------------------------------------------


def _patch_db_for_confirm(record: dict, agent_runs: MagicMock) -> MagicMock:
    db = MagicMock()
    db.agent_runs = agent_runs
    db.get_pending_candidate_by_id.return_value = record
    db.update_pending_candidate_status.return_value = True
    db.resolve_similar_pending_candidates.return_value = None
    return db


def test_confirm_llm_subject_from_outside_marks_applied():
    """M11：从候选列表外确认建议 → 写映射 + 补发 + agent_runs→applied"""
    svc = SyncService()
    record = {
        "id": 5,
        "status": "pending",
        "request_title": "测试番",
        "request_season": 1,
        "user_name": "u1",
        "source": "plex",
        "sync_record_id": 7,
    }
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = {
        "run_id": "run-x",
        "status": "succeeded",
    }
    agent_runs.mark_applied.return_value = True
    db = _patch_db_for_confirm(record, agent_runs)

    with (
        patch("app.services.sync_service.database_manager", db),
        patch.object(svc, "_validate_subject_id", return_value=(True, "")),
        patch(
            "app.services.sync_service.mapping_service.upsert_single_mapping",
            return_value=True,
        ) as mock_upsert,
        patch.object(svc, "_auto_replay_after_confirm", return_value=""),
    ):
        ok, msg = svc.confirm_pending_candidate(5, "999")  # 999 不在任何候选列表内

    assert ok is True
    # 映射以确认的主体写入（允许列表外 subject_id）
    mock_upsert.assert_called_once_with("测试番", "999", 1)
    # 联动：succeeded → applied
    agent_runs.mark_applied.assert_called_once_with("run-x")


def test_confirm_no_linked_run_is_noop():
    """M12b：无关联 agent_runs（开关关流程）→ confirm 成功但联动 no-op 不报错"""
    svc = SyncService()
    record = {
        "id": 6,
        "status": "pending",
        "request_title": "测试番",
        "request_season": 1,
        "user_name": "u1",
        "source": "plex",
        "sync_record_id": 8,
    }
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = None  # 无关联 run
    db = _patch_db_for_confirm(record, agent_runs)

    with (
        patch("app.services.sync_service.database_manager", db),
        patch.object(svc, "_validate_subject_id", return_value=(True, "")),
        patch(
            "app.services.sync_service.mapping_service.upsert_single_mapping",
            return_value=True,
        ),
        patch.object(svc, "_auto_replay_after_confirm", return_value=""),
    ):
        ok, msg = svc.confirm_pending_candidate(6, "123")

    assert ok is True
    agent_runs.mark_applied.assert_not_called()


def test_confirm_linked_run_not_succeeded_no_applied():
    """联动守卫：关联 run 非 succeeded（如 processing）→ 不 applied"""
    svc = SyncService()
    record = {
        "id": 7,
        "status": "pending",
        "request_title": "测试番",
        "request_season": 1,
        "user_name": "u1",
        "source": "plex",
        "sync_record_id": 9,
    }
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = {
        "run_id": "run-y",
        "status": "processing",
    }
    db = _patch_db_for_confirm(record, agent_runs)

    with (
        patch("app.services.sync_service.database_manager", db),
        patch.object(svc, "_validate_subject_id", return_value=(True, "")),
        patch(
            "app.services.sync_service.mapping_service.upsert_single_mapping",
            return_value=True,
        ),
        patch.object(svc, "_auto_replay_after_confirm", return_value=""),
    ):
        ok, msg = svc.confirm_pending_candidate(7, "123")

    assert ok is True
    agent_runs.mark_applied.assert_not_called()


def test_reject_no_linked_run_is_noop():
    """M12b：无关联 agent_runs → reject 成功但联动 no-op 不报错"""
    svc = SyncService()
    record = {
        "id": 8,
        "status": "pending",
        "request_title": "测试番",
        "request_season": 1,
        "user_name": "u1",
        "source": "plex",
        "sync_record_id": 10,
    }
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = None
    db = MagicMock()
    db.agent_runs = agent_runs
    db.get_pending_candidate_by_id.return_value = record
    db.update_pending_candidate_status.return_value = True

    with patch("app.services.sync_service.database_manager", db):
        ok, msg = svc.reject_pending_candidate(8)

    assert ok is True
    agent_runs.mark_rejected.assert_not_called()


def test_reject_linked_succeeded_marks_rejected():
    """联动：关联 run 为 succeeded → reject 联动 mark_rejected"""
    svc = SyncService()
    record = {
        "id": 9,
        "status": "pending",
        "request_title": "测试番",
        "request_season": 1,
        "user_name": "u1",
        "source": "plex",
        "sync_record_id": 11,
    }
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = {
        "run_id": "run-z",
        "status": "succeeded",
    }
    db = MagicMock()
    db.agent_runs = agent_runs
    db.get_pending_candidate_by_id.return_value = record
    db.update_pending_candidate_status.return_value = True

    with patch("app.services.sync_service.database_manager", db):
        ok, msg = svc.reject_pending_candidate(9)

    assert ok is True
    agent_runs.mark_rejected.assert_called_once_with("run-z")


def test_confirm_legacy_without_llm_subject_still_closes():
    """M33：不带关联 run 的既有 confirm → 原逻辑闭环，向后兼容"""
    svc = SyncService()
    record = {
        "id": 10,
        "status": "pending",
        "request_title": "测试番",
        "request_season": 1,
        "user_name": "u1",
        "source": "plex",
        # 无 sync_record_id：纯手动流
    }
    agent_runs = MagicMock()
    agent_runs.find_active_by_sync_record.return_value = None
    db = _patch_db_for_confirm(record, agent_runs)

    with (
        patch("app.services.sync_service.database_manager", db),
        patch.object(svc, "_validate_subject_id", return_value=(True, "")),
        patch(
            "app.services.sync_service.mapping_service.upsert_single_mapping",
            return_value=True,
        ) as mock_upsert,
        patch.object(svc, "_auto_replay_after_confirm", return_value=""),
    ):
        ok, msg = svc.confirm_pending_candidate(10, "123")

    assert ok is True
    mock_upsert.assert_called_once_with("测试番", "123", 1)
    agent_runs.mark_applied.assert_not_called()


# ----------------------------------------------------------------------
# API：confirm 端点接收可选 llm_subject_id
# ----------------------------------------------------------------------


def _build_sync_app():
    application = FastAPI()
    from app.api import sync as sync_module

    application.include_router(sync_module.router)
    return application


async def test_confirm_endpoint_uses_llm_subject_id():
    """API：body 含 llm_subject_id 时优先作为确认 subject_id 传入"""
    from app.api import deps, sync as sync_module

    captured: dict = {}

    def fake_confirm(candidate_id, subject_id):
        captured["subject_id"] = subject_id
        return True, "ok"

    app = _build_sync_app()
    with patch.object(sync_module, "sync_service") as svc:
        svc.confirm_pending_candidate.side_effect = fake_confirm

        async def _user(request=None, credentials=None):
            return {"id": 1, "username": "u", "is_admin": True}

        app.dependency_overrides[deps.get_current_user_flexible] = _user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/pending-candidates/1/confirm",
                json={"llm_subject_id": "888", "subject_id": "111"},
            )

    assert resp.status_code == 200
    assert captured["subject_id"] == "888"


async def test_confirm_endpoint_falls_back_to_subject_id():
    """API：body 无 llm_subject_id 时沿用候选列表内选择的 subject_id（向后兼容）"""
    from app.api import deps, sync as sync_module

    captured: dict = {}

    def fake_confirm(candidate_id, subject_id):
        captured["subject_id"] = subject_id
        return True, "ok"

    app = _build_sync_app()
    with patch.object(sync_module, "sync_service") as svc:
        svc.confirm_pending_candidate.side_effect = fake_confirm

        async def _user(request=None, credentials=None):
            return {"id": 1, "username": "u", "is_admin": True}

        app.dependency_overrides[deps.get_current_user_flexible] = _user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/pending-candidates/1/confirm", json={"subject_id": "222"}
            )

    assert resp.status_code == 200
    assert captured["subject_id"] == "222"

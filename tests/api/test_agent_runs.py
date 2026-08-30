"""
Agent 追踪 API 测试（Task T10 / 场景 M21, M21b）

覆盖：
- 未认证请求 → 401
- 非本人/非管理员访问他人 run → 403
- 本人访问 → 200 + 字段完整
- 管理员可访问他人 run → 200
- 不存在 run → 404
- /steps 按 (iteration, sequence) 排序，且不含 replay_delta
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import agent_runs as agent_runs_module, deps


@pytest.fixture
def app():
    """仅挂载 agent_runs router 的最小应用（鉴权依赖由各用例覆盖）"""
    application = FastAPI()
    application.include_router(agent_runs_module.router)
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def mock_db(app):
    """mock database_manager（agent_runs repo + sync_records 查询）"""
    with patch.object(agent_runs_module, "database_manager") as mock_dm:
        mock_dm.agent_runs = MagicMock()
        mock_dm.sync_records = MagicMock()
        yield mock_dm


def _client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _override_user(app, user):
    async def _fake(request=None, credentials=None):
        return user

    app.dependency_overrides[deps.get_current_user_flexible] = _fake


async def test_get_agent_run_unauthenticated_returns_401(app, mock_db):
    """未认证（依赖直接抛 401）→ 401"""

    async def _raise_401(request=None, credentials=None):
        raise HTTPException(status_code=401, detail="未提供有效的认证信息")

    app.dependency_overrides[deps.get_current_user_flexible] = _raise_401

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-xyz")

    assert resp.status_code == 401


async def test_get_agent_run_other_user_forbidden_403(app, mock_db):
    """非本人/非管理员访问他人 run → 403"""
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "succeeded",
        "stop_reason": "submit_suggestion",
        "attempts": 1,
        "total_attempts": 1,
        "total_tokens": 1234,
        "started_at": "2026-01-01 00:00:00",
        "ended_at": "2026-01-01 00:01:00",
        "created_at": "2026-01-01 00:00:00",
        "last_error": "",
    }
    mock_db.agent_runs.get_run.return_value = run
    # sync_record 42 归属 bob，当前用户是 alice（非管理员）
    mock_db.sync_records.get_sync_record_by_id.return_value = {"user_name": "bob"}

    await _override_user(app, {"username": "alice"})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1")

    assert resp.status_code == 403


async def test_get_agent_run_owner_allowed_200(app, mock_db):
    """本人访问 → 200 + 字段完整"""
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "succeeded",
        "stop_reason": "submit_suggestion",
        "attempts": 1,
        "total_attempts": 1,
        "total_tokens": 1234,
        "started_at": "2026-01-01 00:00:00",
        "ended_at": "2026-01-01 00:01:00",
        "created_at": "2026-01-01 00:00:00",
        "last_error": "boom",
        # 不应暴露的内部字段
        "payload_json": "SECRET",
        "replay_delta": "SECRET",
    }
    mock_db.agent_runs.get_run.return_value = run
    mock_db.sync_records.get_sync_record_by_id.return_value = {"user_name": "alice"}

    await _override_user(app, {"username": "alice"})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1")

    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "status": "succeeded",
        "stop_reason": "submit_suggestion",
        "task_type": "match",
        "sync_record_id": 42,
        "attempts": 1,
        "total_attempts": 1,
        "total_tokens": 1234,
        "started_at": "2026-01-01 00:00:00",
        "ended_at": "2026-01-01 00:01:00",
        "created_at": "2026-01-01 00:00:00",
        "last_error": "boom",
    }
    # 内部重放字段绝不可出现在观测 API 响应
    assert "payload_json" not in data
    assert "replay_delta" not in data


async def test_get_agent_run_admin_can_access_other_200(app, mock_db):
    """管理员可访问他人 run → 200（覆盖“非管理员才 403”）"""
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "processing",
        "stop_reason": "",
        "attempts": 0,
        "total_attempts": 0,
        "total_tokens": 0,
        "started_at": None,
        "ended_at": None,
        "created_at": "2026-01-01 00:00:00",
        "last_error": "",
    }
    mock_db.agent_runs.get_run.return_value = run
    mock_db.sync_records.get_sync_record_by_id.return_value = {"user_name": "bob"}

    await _override_user(app, {"username": "admin", "is_admin": True})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1")

    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"


async def test_get_agent_run_not_found_404(app, mock_db):
    """不存在 run → 404"""
    mock_db.agent_runs.get_run.return_value = None

    await _override_user(app, {"username": "alice"})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-missing")

    assert resp.status_code == 404


async def test_get_agent_run_steps_sorted_and_no_replay_delta(app, mock_db):
    """/steps 按 (iteration, sequence) 排序，且不含 replay_delta"""
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "succeeded",
    }
    mock_db.agent_runs.get_run.return_value = run
    mock_db.sync_records.get_sync_record_by_id.return_value = {"user_name": "alice"}

    # 故意乱序返回（repo 排序与 router 排序都应保证最终有序）
    unsorted_steps = [
        {
            "span_id": "s3",
            "name": "tool_execute",
            "status": "ok",
            "model": "",
            "tokens": 0,
            "latency_ms": 5,
            "tool_name": "get_detail",
            "input_summary": "id=1",
            "error": "",
            "iteration": 1,
            "sequence": 1,
            "payload_json": '{"a":1}',
            "replay_delta": "INTERNAL_SECRET",
            "started_at": "t1",
            "ended_at": "t2",
        },
        {
            "span_id": "s1",
            "name": "llm_chat",
            "status": "ok",
            "model": "claude",
            "tokens": 100,
            "latency_ms": 200,
            "tool_name": "",
            "input_summary": "",
            "error": "",
            "iteration": 0,
            "sequence": 0,
            "payload_json": '{"prompt":"x"}',
            "replay_delta": "INTERNAL_SECRET",
            "started_at": "t1",
            "ended_at": "t2",
        },
        {
            "span_id": "s2",
            "name": "tool_execute",
            "status": "ok",
            "model": "",
            "tokens": 0,
            "latency_ms": 5,
            "tool_name": "search",
            "input_summary": "q",
            "error": "",
            "iteration": 0,
            "sequence": 1,
            "payload_json": '{"q":1}',
            "replay_delta": "INTERNAL_SECRET",
            "started_at": "t1",
            "ended_at": "t2",
        },
    ]
    mock_db.agent_runs.get_steps.return_value = unsorted_steps

    await _override_user(app, {"username": "alice"})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1/steps")

    assert resp.status_code == 200
    steps = resp.json()
    assert [s["span_id"] for s in steps] == ["s1", "s2", "s3"]

    # 每个 span 不应暴露 replay_delta，但应保留 payload_json（观测摘要）
    for s in steps:
        assert "replay_delta" not in s
        assert "payload_json" in s
        assert s["payload_json"] not in (None, "INTERNAL_SECRET")

"""
Agent 追踪 API 测试

覆盖：
- 未认证请求 → 401
- 非本人/非管理员访问他人 run → 403
- 本人访问 → 200 + 字段完整
- 管理员可访问他人 run → 200
- 不存在 run → 404
- /steps 按 (iteration, sequence) 排序，且不含 replay_delta / payload_json
- display_json：llm_chat 截断、tool 预览、seed 条数、空 delta 容错
- 时间字段 epoch → ISO 8601；0/None → None
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import agent_runs as agent_runs_module, deps
from app.utils.truncate import SHRINKED_MARKER


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
    """本人访问 → 200 + 字段完整 + 时间 ISO 化"""
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "succeeded",
        "stop_reason": "submit_suggestion",
        "attempts": 1,
        "total_attempts": 1,
        "total_tokens": 1234,
        "started_at": 1704067200,
        "ended_at": 1704067260,
        "created_at": 1704067200,
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
        "started_at": "2024-01-01T08:00:00+08:00",
        "ended_at": "2024-01-01T08:01:00+08:00",
        "created_at": "2024-01-01T08:00:00+08:00",
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
    """/steps 按 (iteration, sequence) 排序，且不含 replay_delta / payload_json"""
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
            "replay_delta": '{"tool_result":{"tool_use_id":"tu-3","content":"detail ok","is_error":false}}',
            "started_at": 1704067200,
            "ended_at": 1704067201,
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
            "replay_delta": '{"response":{"stop_reason":"end_turn","content":"你好","tool_calls":[]}}',
            "started_at": 1704067200,
            "ended_at": 1704067202,
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
            "replay_delta": '{"tool_result":{"tool_use_id":"tu-2","content":"搜索结果","is_error":false}}',
            "started_at": 1704067200,
            "ended_at": 1704067201,
        },
    ]
    mock_db.agent_runs.get_steps.return_value = unsorted_steps

    await _override_user(app, {"username": "alice"})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1/steps")

    assert resp.status_code == 200
    steps = resp.json()
    assert [s["span_id"] for s in steps] == ["s1", "s2", "s3"]

    # 每个 span 不应暴露 replay_delta / payload_json，但应有 display_json
    for s in steps:
        assert "replay_delta" not in s, f"replay_delta 不应出现在响应: {s}"
        assert "payload_json" not in s, f"payload_json 不应出现在响应: {s}"
        assert "display_json" in s, f"display_json 应出现在响应: {s}"
        # display_json 必须是合法 JSON 字符串
        parsed = json.loads(s["display_json"])
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# display_json 行为测试
# ---------------------------------------------------------------------------


def _make_step(span_id, name, replay_delta, iteration=0, sequence=0):
    return {
        "span_id": span_id,
        "name": name,
        "status": "ok",
        "model": "",
        "tokens": 0,
        "latency_ms": 5,
        "tool_name": "",
        "input_summary": "",
        "error": "",
        "iteration": iteration,
        "sequence": sequence,
        "replay_delta": replay_delta,
        "started_at": 1704067200,
        "ended_at": 1704067201,
    }


async def _get_steps(app, mock_db, steps):
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "succeeded",
    }
    mock_db.agent_runs.get_run.return_value = run
    mock_db.sync_records.get_sync_record_by_id.return_value = {"user_name": "alice"}
    mock_db.agent_runs.get_steps.return_value = steps
    await _override_user(app, {"username": "alice"})
    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1/steps")
    assert resp.status_code == 200
    return resp.json()


async def test_display_json_llm_chat_small_content_not_truncated(app, mock_db):
    """llm_chat 小 content → display_json 含 stop_reason + content，无 shrinked 标记"""
    big = "x" * 50
    delta = json.dumps(
        {"response": {"stop_reason": "end_turn", "content": big, "tool_calls": []}}
    )
    steps = await _get_steps(app, mock_db, [_make_step("s1", "llm_chat", delta)])

    display = json.loads(steps[0]["display_json"])
    assert display["stop_reason"] == "end_turn"
    assert display["content"] == big
    assert SHRINKED_MARKER not in steps[0]["display_json"]


async def test_display_json_llm_chat_oversized_content_truncated_with_marker(
    app, mock_db
):
    """llm_chat 超长 content → display_json 截断且含 shrinked 标记，仍是合法 JSON"""
    huge = "A" * 5000  # 远超 2KB
    delta = json.dumps(
        {"response": {"stop_reason": "end_turn", "content": huge, "tool_calls": []}}
    )
    steps = await _get_steps(app, mock_db, [_make_step("s1", "llm_chat", delta)])

    raw = steps[0]["display_json"]
    # 截断后仍合法 JSON
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    # 含 shrinked 标记
    assert SHRINKED_MARKER in raw
    # 截断后大小 ≤ 2KB
    assert len(raw.encode("utf-8")) <= 2 * 1024


async def test_display_json_tool_execute_preview_fields(app, mock_db):
    """tool_execute → display_json 含 tool_use_id / content / is_error"""
    delta = json.dumps(
        {"tool_result": {"tool_use_id": "tu-1", "content": "ok结果", "is_error": False}}
    )
    steps = await _get_steps(app, mock_db, [_make_step("s1", "tool_execute", delta)])

    display = json.loads(steps[0]["display_json"])
    assert display["tool_use_id"] == "tu-1"
    assert display["content"] == "ok结果"
    assert display["is_error"] is False


async def test_display_json_seed_only_count(app, mock_db):
    """seed → display_json 只给条数，不给消息内容"""
    messages = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
    ]
    delta = json.dumps({"seed_messages": messages})
    steps = await _get_steps(app, mock_db, [_make_step("s1", "seed", delta)])

    display = json.loads(steps[0]["display_json"])
    assert display == {"seed_messages_count": 2}
    # 原始消息内容不应泄露
    assert "msg1" not in steps[0]["display_json"]
    assert "msg2" not in steps[0]["display_json"]


async def test_display_json_empty_delta_returns_empty_string(app, mock_db):
    """空 replay_delta → display_json 为空字符串，不抛异常"""
    steps = await _get_steps(app, mock_db, [_make_step("s1", "llm_chat", "")])
    assert steps[0]["display_json"] == ""


async def test_display_json_malformed_delta_returns_empty_string(app, mock_db):
    """非法 JSON replay_delta → display_json 为空字符串，不抛异常"""
    steps = await _get_steps(
        app, mock_db, [_make_step("s1", "llm_chat", "{not valid json")]
    )
    assert steps[0]["display_json"] == ""


# ---------------------------------------------------------------------------
# 时间字段 ISO 化测试
# ---------------------------------------------------------------------------


async def test_run_timestamps_epoch_to_iso(app, mock_db):
    """agent_runs 时间字段 epoch → ISO 8601 字符串"""
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "succeeded",
        "stop_reason": "submit_suggestion",
        "attempts": 1,
        "total_attempts": 1,
        "total_tokens": 1234,
        "started_at": 1704067200,
        "ended_at": 1704067260,
        "created_at": 1704067200,
        "last_error": "",
    }
    mock_db.agent_runs.get_run.return_value = run
    mock_db.sync_records.get_sync_record_by_id.return_value = {"user_name": "alice"}
    await _override_user(app, {"username": "alice"})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1")

    assert resp.status_code == 200
    data = resp.json()
    # ISO 格式断言：以数字开头、含 T、含时区偏移
    assert "T" in data["started_at"]
    assert "+" in data["started_at"] or "Z" in data["started_at"]
    # 固定 epoch → 固定 ISO（UTC+8 示例：2024-01-01T08:00:00+08:00）
    assert data["started_at"].startswith("2024-01-01T08:00:00")
    assert data["ended_at"].startswith("2024-01-01T08:01:00")


async def test_run_timestamps_zero_or_none_returns_none(app, mock_db):
    """epoch 为 0/None → 返回 None"""
    run = {
        "run_id": "run-1",
        "task_type": "match",
        "sync_record_id": 42,
        "status": "processing",
        "stop_reason": "",
        "attempts": 0,
        "total_attempts": 0,
        "total_tokens": 0,
        "started_at": 0,
        "ended_at": None,
        "created_at": 0,
        "last_error": "",
    }
    mock_db.agent_runs.get_run.return_value = run
    mock_db.sync_records.get_sync_record_by_id.return_value = {"user_name": "alice"}
    await _override_user(app, {"username": "alice"})

    async with _client(app) as client:
        resp = await client.get("/api/agent/runs/run-1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["started_at"] is None
    assert data["ended_at"] is None
    assert data["created_at"] is None


async def test_steps_timestamps_epoch_to_iso(app, mock_db):
    """agent_steps 时间字段 epoch → ISO 8601"""
    delta = json.dumps({"response": {"stop_reason": "end_turn", "content": "hi"}})
    steps = await _get_steps(
        app, mock_db, [_make_step("s1", "llm_chat", delta, iteration=0, sequence=0)]
    )
    s = steps[0]
    assert "T" in s["started_at"]
    assert s["started_at"].startswith("2024-01-01T08:00:00")

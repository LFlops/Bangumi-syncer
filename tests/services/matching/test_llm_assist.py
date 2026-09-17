"""匹配场景服务 llm_assist 测试（无候选全链路 + 兜底 + 注入防护 + 事务）。

使用全局 database_manager（conftest 已重定向到临时 DB）验证落库与状态机；
LLM 调用、bgm、校验、通知均以 mock 注入，保证单测稳定与行为精确断言。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.database import database_manager, set_database_manager
from app.services.agent.loop import RunResult
from app.services.agent.trace import ReplayResult
from app.services.llm.models import (
    ChatResponse,
    Message,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from app.services.llm.tools import (
    ToolDefinition,
    ToolRegistry,
    reset_tool_registry,
)
from app.services.matching import llm_assist
from app.services.matching.llm_assist import build_seed_messages


@pytest.fixture(autouse=True)
def _align_db_singleton():
    """对齐 get_database_manager() 与测试实例（conftest 仅设模块属性）。"""
    set_database_manager(database_manager)
    yield
    set_database_manager(None)


def _make_sync_record(with_candidates=False, sync_record_id=1):
    if with_candidates:
        match_trace = {
            "steps": [
                {
                    "stage": "api_search",
                    "status": "hit",
                    "candidates": [
                        {
                            "subject_id": "111",
                            "name": "Rule A",
                            "name_cn": "规则A",
                            "score": 0.8,
                        },
                        {
                            "subject_id": "222",
                            "name": "Rule B",
                            "name_cn": "规则B",
                            "score": 0.5,
                        },
                    ],
                }
            ]
        }
    else:
        match_trace = {
            "steps": [{"stage": "api_search", "status": "miss", "candidates": []}]
        }
    # 标题随 sync_record_id 变化，避免 pending_candidates 唯一索引在共享 DB 上冲突
    return {
        "id": sync_record_id,
        "title": f"标题{sync_record_id}",
        "ori_title": "花咲くいろは",
        "season": 1,
        "episode": 0,
        "media_type": "episode",
        "release_date": "2012",
        "user_name": "alice",
        "source": "plex",
        "match_trace": match_trace,
    }


def _search_response(content=""):
    return ChatResponse(
        content=content,
        stop_reason="tool_use",
        blocks=[
            ToolUseBlock(id="t1", name="search_bangumi", input={"title": "花开伊吕波"})
        ],
    )


def _submit_response(subject_id="123", reason="跨季匹配"):
    return ChatResponse(
        content="",
        stop_reason="tool_use",
        blocks=[
            ToolUseBlock(
                id="s1",
                name="submit_suggestion",
                input={"subject_id": subject_id, "reason": reason},
            )
        ],
    )


def _read_candidate(sync_record_id):
    """直接读取 pending_candidates 行（含 llm 两列），因为 repo 查询未 SELECT 这两列。"""
    conn = database_manager._connection._conn
    cur = conn.execute(
        "SELECT id, llm_subject_id, llm_reason, candidates_json, status "
        "FROM pending_candidates WHERE sync_record_id=? ORDER BY id DESC LIMIT 1",
        (sync_record_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "llm_subject_id": row[1],
        "llm_reason": row[2],
        "candidates_json": row[3],
        "status": row[4],
    }


def _assert_candidate_written(sync_record_id, subject_id="123", reason="跨季匹配"):
    row = _read_candidate(sync_record_id)
    assert row is not None, "pending_candidates 行应已写入"
    assert row["llm_subject_id"] == subject_id, "llm_subject_id 应写入"
    assert row["llm_reason"] == reason, "llm_reason 应写入"
    assert row["status"] == "pending"
    candidates = json.loads(row["candidates_json"]) if row["candidates_json"] else []
    assert any(str(c.get("subject_id")) == subject_id for c in candidates)
    return row


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_tool_registry()
    yield
    reset_tool_registry()


# ---------------------------------------------------------------------------
# M5：有候选 + submit_suggestion → 更新既有 pending_candidates 行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_submit_suggestion_updates_existing_candidate(monkeypatch):
    run_id = "run-m5"
    sr_id = 1
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    # 既有规则沉淀行（有候选）
    database_manager.log_pending_candidate(
        request_title="花开伊吕波剧场版",
        request_ori_title="花咲くいろは",
        request_season=1,
        request_episode=0,
        user_name="alice",
        source="plex",
        candidates=[
            {"subject_id": "111", "name": "Rule A", "name_cn": "规则A", "score": 0.8}
        ],
        sync_record_id=sr_id,
    )
    sr = _make_sync_record(with_candidates=True, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    bgm = _make_bgm()
    ns = _make_notify()

    chat = _chat_side_effect([_search_response(), _submit_response()])

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "succeeded"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "succeeded"
    assert run_row["stop_reason"] == "submit_suggestion"

    row = _assert_candidate_written(sr_id)
    # 既有候选保留 + 追加 LLM 推荐
    candidates = json.loads(row["candidates_json"])
    assert any(str(c.get("subject_id")) == "111" for c in candidates)
    assert any(str(c.get("subject_id")) == "123" for c in candidates)

    # 通知 best-effort 触发，且带 is_llm_suggestion
    ns.notify.assert_called_once()
    _, kwargs = ns.notify.call_args
    assert kwargs.get("is_llm_suggestion") is True
    assert kwargs.get("llm_reason") == "跨季匹配"


# ---------------------------------------------------------------------------
# M5b：无候选 → 新建行（candidates_json=[] + llm 两列）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_submit_suggestion_creates_new_row_when_no_candidate(monkeypatch):
    run_id = "run-m5b"
    sr_id = 2
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=False, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    bgm = _make_bgm()
    ns = _make_notify()

    chat = _chat_side_effect(
        [_search_response(), _submit_response("456", "无候选补充")]
    )

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "succeeded"
    row = _assert_candidate_written(sr_id, subject_id="456", reason="无候选补充")
    # 无候选场景：candidates_json 仅含 LLM 推荐（无规则候选）
    candidates = json.loads(row["candidates_json"])
    assert len(candidates) == 1
    assert str(candidates[0]["subject_id"]) == "456"


# ---------------------------------------------------------------------------
# 无候选全链路（LLM 搜索补充 → 建议 → 落库）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_no_candidate_full_link_search_then_submit(monkeypatch):
    run_id = "run-m24"
    sr_id = 3
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=False, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    bgm = _make_bgm(search_result=[{"id": 789, "name": "花咲くいろは HOME SWEET HOME"}])
    ns = _make_notify()

    chat = _chat_side_effect([_search_response(), _submit_response("789", "搜索补充")])

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "succeeded"
    row = _assert_candidate_written(sr_id, subject_id="789", reason="搜索补充")
    candidates = json.loads(row["candidates_json"])
    assert any(str(c.get("subject_id")) == "789" for c in candidates)


# ---------------------------------------------------------------------------
# submit_suggestion subject_id 非法 → no_suggestion + last_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_submit_invalid_subject_id_no_suggestion(monkeypatch):
    run_id = "run-m15"
    sr_id = 4
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    # 既有候选行（应保持不变）
    database_manager.log_pending_candidate(
        request_title=f"rule-{sr_id}",
        request_season=1,
        user_name="alice",
        source="plex",
        candidates=[{"subject_id": "111", "name": "A", "score": 0.8}],
        sync_record_id=sr_id,
    )
    sr = _make_sync_record(with_candidates=True, sync_record_id=sr_id)

    monkeypatch.setattr(
        llm_assist, "_validate_subject_id", lambda sid: (False, "subject_id 非法")
    )
    bgm = _make_bgm()
    ns = _make_notify()

    chat = _chat_side_effect([_submit_response("999999", "非法")])

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "no_suggestion"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "no_suggestion"
    assert run_row["stop_reason"] == "submit_suggestion"
    assert "非法" in (run_row["last_error"] or "")

    # 不落库：既有行 llm 两列仍为空
    row = _read_candidate(sr_id)
    assert row["llm_subject_id"] == ""
    assert row["llm_reason"] == ""
    ns.notify.assert_not_called()


# ---------------------------------------------------------------------------
# 工具执行失败（bgm.search 抛错）→ 循环继续 → 最终 no_suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tool_execution_failure_leads_to_no_suggestion(monkeypatch):
    run_id = "run-m25"
    sr_id = 5
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    # 既有候选行（不应被改写）
    database_manager.log_pending_candidate(
        request_title=f"rule-{sr_id}",
        request_season=1,
        user_name="alice",
        source="plex",
        candidates=[{"subject_id": "111", "name": "A", "score": 0.8}],
        sync_record_id=sr_id,
    )
    sr = _make_sync_record(with_candidates=True, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))

    class _Boom:
        def search(self, **kwargs):
            raise RuntimeError("network down")

        def get_subject(self, sid):
            return {}

        def get_related_subjects(self, sid):
            return []

    ns = _make_notify()
    # 每轮都调 search（失败），共 max_iterations(medium=3) 轮 → exhausted
    chat = _chat_side_effect([_search_response() for _ in range(3)])

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=_Boom(),
        thinking_level="medium",
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "no_suggestion"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "no_suggestion"
    assert run_row["stop_reason"] == "exhausted"
    # 既有候选行未被 LLM 改写
    row = _read_candidate(sr_id)
    assert row["llm_subject_id"] == ""
    ns.notify.assert_not_called()


# ---------------------------------------------------------------------------
# 耗尽兜底：exhausted + 文本 JSON → 成功落库；文本无 JSON → no_suggestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_exhausted_with_json_fallback_succeeds(monkeypatch):
    run_id = "run-exh-ok"
    sr_id = 6
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=False, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    bgm = _make_bgm()
    ns = _make_notify()

    # 每轮返回非终止工具调用 + 末轮 content 含 JSON（loop 跑满 medium=3 轮）
    chat = _chat_side_effect(
        [
            _search_response('{"subject_id": "321", "reason": "兜底"}'),
            _search_response('{"subject_id": "321", "reason": "兜底"}'),
            _search_response('{"subject_id": "321", "reason": "兜底"}'),
        ]
    )

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "succeeded"
    _assert_candidate_written(sr_id, subject_id="321", reason="兜底")
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["stop_reason"] == "exhausted"


@pytest.mark.asyncio
async def test_run_exhausted_without_json_no_suggestion(monkeypatch):
    run_id = "run-exh-bad"
    sr_id = 7
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=False, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    bgm = _make_bgm()
    ns = _make_notify()

    # 末轮 content 无 JSON
    chat = _chat_side_effect(
        [
            _search_response("无意义的文本"),
            _search_response("无意义的文本"),
            _search_response("无意义的文本"),
        ]
    )

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "no_suggestion"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["stop_reason"] == "exhausted"


# ---------------------------------------------------------------------------
# 注入防护：system 含不可信声明、user 被分隔符隔离
# ---------------------------------------------------------------------------


def test_build_seed_messages_injection_guard_and_isolation():
    sr = _make_sync_record(with_candidates=True, sync_record_id=1)
    candidates = llm_assist._extract_candidates(sr)
    messages = build_seed_messages(sr, candidates, llm_assist.DEFAULT_SYSTEM_TEMPLATE)

    assert len(messages) == 2
    system = messages[0]
    user = messages[1]
    assert system.role == "system"
    assert "不可信" in system.content
    assert user.role == "user"
    # 用户输入被 --- 分隔符隔离
    assert user.content.count("---") >= 2
    # 用户提供的标题出现在隔离区内
    assert "标题1" in user.content
    # 候选摘要出现在 user 区（来自 trace）
    assert "规则A" in user.content


# ---------------------------------------------------------------------------
# 事务原子性：_persist_llm_candidate 内第二条 UPDATE 抛错时整体回滚
# ---------------------------------------------------------------------------


def test_persist_llm_candidate_atomic_rollback_on_failure(monkeypatch):
    """_persist_llm_candidate 内 agent_runs UPDATE 抛错 → 整体回滚，run 不 succeeded、候选 llm 列不被改写。"""
    import sqlite3

    run_id = "run-atomic"
    sr_id = 8
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    database_manager.log_pending_candidate(
        request_title=f"rule-{sr_id}",
        request_season=1,
        user_name="alice",
        source="plex",
        candidates=[{"subject_id": "111", "name": "A", "score": 0.8}],
        sync_record_id=sr_id,
    )
    sr = _make_sync_record(with_candidates=True, sync_record_id=sr_id)

    # 在事务内的第二条语句（agent_runs UPDATE）抛错，验证整体回滚。
    # sqlite3.Connection 为内置类型不可直接 monkeypatch，改用连接包装器，
    # 并经 monkeypatch 自动恢复，避免污染后续测试连接。
    real_conn = database_manager._connection._conn

    class _Conn:
        def __init__(self, real):
            self._real = real

        def execute(self, *args, **kwargs):
            if args and "UPDATE agent_runs" in args[0]:
                raise sqlite3.OperationalError("boom mid-transaction")
            return self._real.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(database_manager._connection, "_conn", _Conn(real_conn))

    with pytest.raises(sqlite3.OperationalError):
        llm_assist._persist_llm_candidate(
            database_manager,
            run_id=run_id,
            sync_record_id=sr_id,
            sync_record=sr,
            subject_id="123",
            reason="r",
            stop_reason="submit_suggestion",
            bgm=None,
        )

    # 回滚验证：agent_runs 未 succeeded，pending_candidates 未被改写
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "pending"  # 原始状态，未 succeeded
    row = _read_candidate(sr_id)
    assert row["llm_subject_id"] == ""  # 候选写入被回滚


# ---------------------------------------------------------------------------
# 并发抢占：atomic_claim 失败 → 返回 skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_atomic_claim_failure_returns_skipped(monkeypatch):
    run_id = "run-skip"
    sr_id = 9
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    # 先手动置为 processing，使 atomic_claim 失败
    database_manager.agent_runs.atomic_claim(run_id)
    # 现在已是 processing，再次 claim 会失败；但 run 内部会 claim 一次。
    # 为模拟并发抢占，直接把状态改回 pending 之外的不可 claim 状态：
    database_manager.agent_runs.update_run_status(run_id, "succeeded")

    bgm = _make_bgm()
    chat = _chat_side_effect([_submit_response()])
    status = await llm_assist.run(
        run_id,
        sync_record=_make_sync_record(sync_record_id=sr_id),
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        span_recorder=None,
    )
    assert status == "skipped"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _chat_side_effect(responses):
    async def _chat(messages, *, tools=None, tool_choice=None):
        idx = _chat.idx
        _chat.idx += 1
        if idx < len(responses):
            return responses[idx]
        # 超出则返回 end_turn 兜底
        return ChatResponse(content="done", stop_reason="end_turn")

    _chat.idx = 0
    return _chat


def _make_bgm(search_result=None):
    class _Bgm:
        def search(self, **kwargs):
            return search_result if search_result is not None else [{"id": 1}]

        def get_subject(self, sid):
            return {"name": f"subject-{sid}", "name_cn": f"条目-{sid}"}

        def get_related_subjects(self, sid):
            return []

    return _Bgm()


def _make_notify():
    from unittest.mock import MagicMock

    ns = MagicMock()
    ns.notify.return_value = True
    return ns


# ---------------------------------------------------------------------------
# G1：register_match_tools 重复注册必须覆盖 handler 闭包（重新绑定 bgm），
#     且覆盖时不得产生“重复注册”warning（quiet=True）


def test_register_match_tools_overwrite_no_warning(caplog):
    import logging

    from app.services.llm.tools import ToolRegistry

    registry = ToolRegistry()
    bgm = _make_bgm()

    # 连续两次注册到同一（模块单例）registry：第二次应覆盖 handler（G1），且不刷 warning
    with caplog.at_level(logging.WARNING):
        llm_assist.register_match_tools(registry, bgm)
        llm_assist.register_match_tools(registry, bgm)

    # 不应出现任何“重复注册”warning
    dup = [r for r in caplog.records if "重复注册" in r.message]
    assert not dup, f"重复注册 warning 不应出现: {dup}"

    # 工具仍全部可用（首次注册即已落位）
    assert registry.get("submit_suggestion") is not None
    assert registry.get("search_bangumi") is not None
    assert registry.get("get_subject_detail") is not None
    assert registry.get("check_subject") is not None
    assert registry.get("get_related_subjects") is not None


# ---------------------------------------------------------------------------
# G1：register_match_tools 重复注册必须覆盖 handler 闭包（重新绑定 bgm），
# 否则多用户跨 run 复用首次注册的错误 token
# ---------------------------------------------------------------------------


def test_register_match_tools_rebinds_handlers_to_latest_bgm():
    from app.services.llm.tools import ToolRegistry

    registry = ToolRegistry()
    bgm1 = _make_bgm(search_result=[{"id": 1, "name": "first"}])
    bgm2 = _make_bgm(search_result=[{"id": 2, "name": "second"}])

    llm_assist.register_match_tools(registry, bgm1)
    llm_assist.register_match_tools(registry, bgm2)

    # search handler 闭包应指向第二个 bgm
    search = registry.get("search_bangumi")
    assert search is not None
    assert search.handler({"title": "x"}) == [{"id": 2, "name": "second"}]


@pytest.mark.asyncio
async def test_two_runs_with_different_bgm_second_run_uses_second_bgm():
    """两次 run（共享模块单例 registry）→ 第二次的 search handler 调用第二个 bgm。"""
    used: list[str] = []

    def _bgm(tag: str):
        class _B:
            def search(self, **kwargs):
                used.append(tag)
                return [{"id": 1}]

            def get_subject(self, sid):
                return {"name": f"subject-{sid}", "name_cn": f"条目-{sid}"}

            def get_related_subjects(self, sid):
                return []

        return _B()

    bgm1, bgm2 = _bgm("bgm1"), _bgm("bgm2")

    run_a, sr_a = "run-g1-a", 60
    database_manager.agent_runs.create_pending(run_a, "match", sr_a)
    await llm_assist.run(
        run_a,
        sync_record=_make_sync_record(sync_record_id=sr_a),
        bgm=bgm1,
        thinking_level="medium",
        chat_fn=_chat_side_effect([_search_response()]),
        span_recorder=None,
    )
    assert used == ["bgm1"], "首个 run 应使用第一个 bgm"

    run_b, sr_b = "run-g1-b", 61
    database_manager.agent_runs.create_pending(run_b, "match", sr_b)
    await llm_assist.run(
        run_b,
        sync_record=_make_sync_record(sync_record_id=sr_b),
        bgm=bgm2,
        thinking_level="medium",
        chat_fn=_chat_side_effect([_search_response()]),
        span_recorder=None,
    )
    assert used == ["bgm1", "bgm2"], (
        f"第二个 run 必须使用第二个 bgm（实际 {used}）——handler 闭包不得钉死首次注册"
    )


# ---------------------------------------------------------------------------
# F5：llm_assist.run 将 config_override（llm_match_max_iterations）透传给
# get_max_iterations（优先级：配置覆盖 > 策略 > 默认）；空值传 None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_passes_config_override_to_get_max_iterations(monkeypatch):
    from unittest.mock import MagicMock

    from app.services.agent.loop import RunResult

    captured = {}

    def _gmi(task_type, thinking_level, config_override=None):
        captured["task_type"] = task_type
        captured["thinking_level"] = thinking_level
        captured["config_override"] = config_override
        return 1

    monkeypatch.setattr(llm_assist, "get_max_iterations", _gmi)

    cm = MagicMock()
    cm.get_sync_llm_match_config.return_value = {
        "llm_match_max_iterations": "10",
        "llm_match_thinking_level": "high",
    }
    monkeypatch.setattr(llm_assist, "config_manager", cm)

    async def _fake_loop(**kwargs):
        return RunResult(stop_reason="end_turn")

    monkeypatch.setattr(llm_assist, "loop_run", _fake_loop)

    run_id = "run-f5"
    sr_id = 50
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)
    await llm_assist.run(run_id, sync_record=sr, bgm=_make_bgm(), thinking_level="high")

    assert captured["task_type"] == "match"
    assert captured["thinking_level"] == "high"
    assert captured["config_override"] == 10


@pytest.mark.asyncio
async def test_run_empty_config_override_passes_none(monkeypatch):
    from unittest.mock import MagicMock

    from app.services.agent.loop import RunResult

    captured = {}

    def _gmi(task_type, thinking_level, config_override=None):
        captured["config_override"] = config_override
        return 1

    monkeypatch.setattr(llm_assist, "get_max_iterations", _gmi)

    cm = MagicMock()
    # 空字符串（默认）→ 应传 None，交由策略/默认兜底
    cm.get_sync_llm_match_config.return_value = {
        "llm_match_max_iterations": "",
        "llm_match_thinking_level": "medium",
    }
    monkeypatch.setattr(llm_assist, "config_manager", cm)

    async def _fake_loop(**kwargs):
        return RunResult(stop_reason="end_turn")

    monkeypatch.setattr(llm_assist, "loop_run", _fake_loop)

    run_id = "run-f5b"
    sr_id = 51
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)
    await llm_assist.run(
        run_id, sync_record=sr, bgm=_make_bgm(), thinking_level="medium"
    )

    assert captured["config_override"] is None


# ---------------------------------------------------------------------------
# G3：config_override 非正数（0 / 负值）→ 告警并回退 None（由策略默认接管），
# 否则 max_iterations<=0 会让循环空跑并把 run 滞留在 processing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,tag",
    [("0", "zero"), ("-1", "neg1"), ("-10", "neg10"), (" 0 ", "padded-zero")],
)
@pytest.mark.asyncio
async def test_run_non_positive_config_override_falls_back_to_none(
    monkeypatch, raw, tag
):
    from unittest.mock import MagicMock

    from app.services.agent.loop import RunResult

    captured = {}

    def _gmi(task_type, thinking_level, config_override=None):
        captured["config_override"] = config_override
        return 3

    monkeypatch.setattr(llm_assist, "get_max_iterations", _gmi)

    cm = MagicMock()
    cm.get_sync_llm_match_config.return_value = {
        "llm_match_max_iterations": raw,
        "llm_match_thinking_level": "medium",
    }
    monkeypatch.setattr(llm_assist, "config_manager", cm)

    log = MagicMock()
    monkeypatch.setattr(llm_assist, "logger", log)

    async def _fake_loop(**kwargs):
        return RunResult(stop_reason="end_turn")

    monkeypatch.setattr(llm_assist, "loop_run", _fake_loop)

    run_id = f"run-g3-{tag}"
    sr_id = 70
    assert database_manager.agent_runs.create_pending(run_id, "match", sr_id), (
        "前置：agent_run 应创建成功（否则 run 会因抢占失败提前返回 skipped）"
    )
    sr = _make_sync_record(sync_record_id=sr_id)
    status = await llm_assist.run(
        run_id, sync_record=sr, bgm=_make_bgm(), thinking_level="medium"
    )
    assert status != "skipped"

    assert captured["config_override"] is None, (
        f"非正数配置 {raw!r} 应回退 None，交由策略默认"
    )
    assert log.warning.called, "非正数配置应打印告警"


@pytest.mark.asyncio
async def test_run_invalid_config_override_logs_warning(monkeypatch):
    """非法字符串（无法转 int）→ 回退 None 且告警。"""
    from unittest.mock import MagicMock

    from app.services.agent.loop import RunResult

    captured = {}

    def _gmi(task_type, thinking_level, config_override=None):
        captured["config_override"] = config_override
        return 3

    monkeypatch.setattr(llm_assist, "get_max_iterations", _gmi)

    cm = MagicMock()
    cm.get_sync_llm_match_config.return_value = {
        "llm_match_max_iterations": "abc",
        "llm_match_thinking_level": "medium",
    }
    monkeypatch.setattr(llm_assist, "config_manager", cm)
    log = MagicMock()
    monkeypatch.setattr(llm_assist, "logger", log)

    async def _fake_loop(**kwargs):
        return RunResult(stop_reason="end_turn")

    monkeypatch.setattr(llm_assist, "loop_run", _fake_loop)

    run_id = "run-g3-invalid"
    sr_id = 71
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    await llm_assist.run(
        run_id,
        sync_record=_make_sync_record(sync_record_id=sr_id),
        bgm=_make_bgm(),
        thinking_level="medium",
    )

    assert captured["config_override"] is None
    assert log.warning.called


# ---------------------------------------------------------------------------
# F8：事务内不应发起 HTTP（bgm.get_subject 在事务外预取一次）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_does_not_call_bgm_in_transaction(monkeypatch):
    from unittest.mock import MagicMock

    bgm = MagicMock()
    bgm.get_subject.return_value = {"name": "N", "name_cn": "NC"}

    conn = MagicMock()
    captured = {}
    real_get = bgm.get_subject

    def _exec_with_lock(fn):
        before = real_get.call_count
        # 运行事务回调：必须不在此处发起 HTTP 调用
        fn(conn)
        after = real_get.call_count
        captured["in_tx_calls"] = after - before
        return 1

    dbm = MagicMock()
    dbm._execute_with_lock.side_effect = _exec_with_lock

    llm_assist._persist_and_notify(
        dbm,
        "run-f8",
        sync_record=_make_sync_record(sync_record_id=52),
        sync_record_id=52,
        bgm=bgm,
        subject_id="5",
        reason="r",
        stop_reason="submit_suggestion",
        total_tokens=0,
        notification_service=None,
    )

    # 事务回调内不得发起 bgm.get_subject（HTTP）
    assert captured["in_tx_calls"] == 0, "事务内不应发起 HTTP(bgm.get_subject)"
    # 事务外应预取恰好一次标题
    assert bgm.get_subject.call_count == 1, "应在事务外预取一次标题"


# ---------------------------------------------------------------------------
# 思考开关透传：llm_match_thinking_level 应同时透传到 client.chat 调用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_default_chat_fn_passes_thinking_level_medium(monkeypatch):
    """场景1：run(thinking_level="medium") 使用默认 chat_fn 时，client.chat 收到 thinking_level='medium'。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    # Mock LLMClient：chat 返回 end_turn 使循环立即结束
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=ChatResponse(content="", stop_reason="end_turn")
    )

    # get_llm_client 由 llm_assist 模块头部导入，patch 其模块命名空间
    # 不 mock loop_run：让真实循环执行，验证默认 chat_fn 确实调用 client.chat
    with patch(
        "app.services.matching.llm_assist.get_llm_client", return_value=mock_client
    ):
        run_id = "run-think-medium"
        sr_id = 80
        database_manager.agent_runs.create_pending(run_id, "match", sr_id)
        await llm_assist.run(
            run_id,
            sync_record=_make_sync_record(sync_record_id=sr_id),
            bgm=_make_bgm(),
            thinking_level="medium",
        )

    # client.chat 应被调用，且收到 thinking_level="medium"
    assert mock_client.chat.called, "默认 chat_fn 应调用 client.chat"
    _, kwargs = mock_client.chat.call_args
    assert kwargs.get("thinking_level") == "medium", (
        f"client.chat 应收到 thinking_level='medium'，实际 kwargs={kwargs}"
    )


@pytest.mark.asyncio
async def test_run_thinking_level_high_controls_max_iterations(monkeypatch):
    """场景2：run(thinking_level="high") → loop_run 收到 max_iterations=5。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.agent.loop import RunResult

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=ChatResponse(content="", stop_reason="end_turn")
    )

    # 捕获 loop_run 收到的实参
    captured = {}

    async def _fake_loop_run(**kwargs):
        captured.update(kwargs)
        return RunResult(stop_reason="end_turn")

    monkeypatch.setattr(llm_assist, "loop_run", _fake_loop_run)

    with patch(
        "app.services.matching.llm_assist.get_llm_client", return_value=mock_client
    ):
        run_id = "run-think-high"
        sr_id = 81
        database_manager.agent_runs.create_pending(run_id, "match", sr_id)
        await llm_assist.run(
            run_id,
            sync_record=_make_sync_record(sync_record_id=sr_id),
            bgm=_make_bgm(),
            thinking_level="high",
        )

    # 验证 max_iterations 直接传入 loop_run：high → 5
    assert captured.get("max_iterations") == 5, (
        f"loop_run 应收到 max_iterations=5，实际 captured={captured}"
    )


@pytest.mark.asyncio
async def test_run_default_chat_fn_passes_thinking_level_off(monkeypatch):
    """场景3：run(thinking_level="off") → client.chat 收到 thinking_level='off'。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=ChatResponse(content="", stop_reason="end_turn")
    )

    with patch(
        "app.services.matching.llm_assist.get_llm_client", return_value=mock_client
    ):
        run_id = "run-think-off"
        sr_id = 82
        database_manager.agent_runs.create_pending(run_id, "match", sr_id)
        await llm_assist.run(
            run_id,
            sync_record=_make_sync_record(sync_record_id=sr_id),
            bgm=_make_bgm(),
            thinking_level="off",
        )

    assert mock_client.chat.called, "默认 chat_fn 应调用 client.chat"
    _, kwargs = mock_client.chat.call_args
    assert kwargs.get("thinking_level") == "off", (
        f"client.chat 应收到 thinking_level='off'，实际 kwargs={kwargs}"
    )


@pytest.mark.asyncio
async def test_run_custom_chat_fn_injection_unaffected(monkeypatch):
    """场景4：显式传入 chat_fn 时，不调用 get_llm_client，chat_fn 不被包装/改签名。"""
    from unittest.mock import MagicMock, patch

    mock_client = MagicMock()

    custom_called = {}

    async def custom_chat_fn(messages, *, tools=None, tool_choice=None):
        custom_called["invoked"] = True
        custom_called["tools"] = tools
        custom_called["tool_choice"] = tool_choice
        return ChatResponse(content="", stop_reason="end_turn")

    with patch(
        "app.services.matching.llm_assist.get_llm_client", return_value=mock_client
    ):
        run_id = "run-custom-chatfn"
        sr_id = 83
        database_manager.agent_runs.create_pending(run_id, "match", sr_id)
        await llm_assist.run(
            run_id,
            sync_record=_make_sync_record(sync_record_id=sr_id),
            bgm=_make_bgm(),
            thinking_level="medium",
            chat_fn=custom_chat_fn,
        )

    # 自定义 chat_fn 应被直接调用
    assert custom_called.get("invoked") is True, "自定义 chat_fn 应被调用"
    # get_llm_client 不应被触发（默认 chat_fn 未构建）
    assert not mock_client.chat.called, (
        "注入自定义 chat_fn 时不应调用 get_llm_client().chat"
    )
    # 签名保持不变：tools / tool_choice 以关键字参数传入
    assert "tools" in custom_called
    assert "tool_choice" in custom_called


# ---------------------------------------------------------------------------
# TraceRecorder 接线：seed 行 / chat 包装 / tool span / budget 钩子
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_full_link_trace_recorder_seed_chat_tool_budget(monkeypatch):
    """S4.1：正常 run 全链路——seed 行存在、chat span model/tokens 专用列有值、
    tool_execute span 由 executor 包裹产生、replay_delta 格式与现状一致。"""
    run_id = "run-tr-full"
    sr_id = 90
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=False, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    bgm = _make_bgm()

    # 捕获 trace 模块调用
    captured = {
        "start": [],
        "end": [],
        "budget": [],
    }
    original_start = llm_assist.trace_start_span
    original_end = llm_assist.trace_end_span
    original_budget = llm_assist.trace_record_budget_message

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        span_id = original_start(run_id, name, iteration, sequence, parent_id)
        captured["start"].append(
            {
                "name": name,
                "iteration": iteration,
                "sequence": sequence,
                "span_id": span_id,
            }
        )
        return span_id

    def fake_end(span_id, **kwargs):
        captured["end"].append({"span_id": span_id, **kwargs})
        return original_end(span_id, **kwargs)

    def fake_budget(span_id, budget_message):
        captured["budget"].append(
            {"span_id": span_id, "budget_message": budget_message}
        )
        return original_budget(span_id, budget_message)

    monkeypatch.setattr(llm_assist, "trace_start_span", fake_start)
    monkeypatch.setattr(llm_assist, "trace_end_span", fake_end)
    monkeypatch.setattr(llm_assist, "trace_record_budget_message", fake_budget)

    chat = _chat_side_effect([_search_response(), _submit_response("789", "搜索补充")])

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        span_recorder=None,
    )

    assert status == "succeeded"

    # seed 行存在
    seed_starts = [s for s in captured["start"] if s["name"] == "seed"]
    assert len(seed_starts) == 1, "应存在 1 条 seed span"
    seed_ends = [
        e for e in captured["end"] if e["span_id"] == seed_starts[0]["span_id"]
    ]
    assert len(seed_ends) == 1
    assert "seed_messages" in seed_ends[0]["replay_delta"]

    # chat span 存在（llm_chat）
    chat_starts = [s for s in captured["start"] if s["name"] == "llm_chat"]
    assert len(chat_starts) >= 1, "应存在 llm_chat span"
    chat_ends = [
        e
        for e in captured["end"]
        if e["span_id"] in [s["span_id"] for s in chat_starts]
    ]
    for ce in chat_ends:
        # replay_delta 含 response 结构
        delta = ce.get("replay_delta", {})
        assert "response" in delta, delta
        resp = delta["response"]
        assert set(resp.keys()) >= {"stop_reason", "content", "tool_calls"}

    # tool_execute span 存在（由 executor 包裹产生）
    tool_starts = [s for s in captured["start"] if s["name"] == "tool_execute"]
    assert len(tool_starts) >= 1, "应存在 tool_execute span"

    # budget 钩子被调用（每轮一次）
    assert len(captured["budget"]) >= 1, "budget 应被记录"


@pytest.mark.asyncio
async def test_run_recorder_none_path_semantic_preserved(monkeypatch):
    """S4.2：span_recorder=None 路径语义保持——run 结果正确、loop 领域语义不破。"""
    run_id = "run-tr-none"
    sr_id = 91
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=False, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    bgm = _make_bgm()

    chat = _chat_side_effect([_submit_response("111", "直接建议")])

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=bgm,
        thinking_level="medium",
        chat_fn=chat,
        span_recorder=None,
    )

    assert status == "succeeded"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "succeeded"
    assert run_row["stop_reason"] == "submit_suggestion"


def test_trace_recorder_wrap_chat_fn_tracks_iteration():
    """S1.4：wrap_chat_fn 每轮 start/end span，iteration 自增。"""
    import asyncio
    from unittest.mock import patch

    from app.services.llm.models import Message

    starts = []
    ends = []

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        starts.append({"name": name, "iteration": iteration, "sequence": sequence})
        return f"span-{name}-{iteration}-{sequence}"

    def fake_end(span_id, **kwargs):
        ends.append({"span_id": span_id, **kwargs})

    recorder = llm_assist.TraceRecorder("run-test", start_iteration=0)

    async def dummy_chat(messages, *, tools=None, tool_choice=None):
        return ChatResponse(
            content="",
            stop_reason="end_turn",
            blocks=[],
            model="test-model",
        )

    wrapped = recorder.wrap_chat_fn(dummy_chat)

    # patch llm_assist 模块上的引用（闭包通过模块全局名字查找）
    with (
        patch.object(llm_assist, "trace_start_span", side_effect=fake_start),
        patch.object(llm_assist, "trace_end_span", side_effect=fake_end),
    ):
        asyncio.run(wrapped([Message(role="user", content="hi")], tools=[]))

    # 应有一条 llm_chat start + end
    assert len(starts) == 1
    assert starts[0]["name"] == "llm_chat"
    assert starts[0]["iteration"] == 0
    assert len(ends) == 1
    # model 写专用列（不再塞 payload_json）
    assert ends[0]["model"] == "test-model"
    assert ends[0]["status"] == "ok"
    assert "response" in ends[0]["replay_delta"]


def test_trace_recorder_tool_start_end_idempotent():
    """S1.3：end_tool 幂等——同 span_id 二次调用不崩溃。"""
    from unittest.mock import patch

    from app.services.llm.models import ToolResultBlock

    starts = []
    ends = []

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        sid = f"tool-span-{len(starts)}"
        starts.append(sid)
        return sid

    def fake_end(span_id, **kwargs):
        ends.append(span_id)

    recorder = llm_assist.TraceRecorder("run-test", start_iteration=0)
    tc = ToolUseBlock(id="t1", name="search_bangumi", input={"title": "x"})

    # 模拟 chat 已发生（设置 chat_span_id）
    recorder._chat_span_id = "chat-span-0"

    with (
        patch.object(llm_assist, "trace_start_span", side_effect=fake_start),
        patch.object(llm_assist, "trace_end_span", side_effect=fake_end),
    ):
        span_id = recorder.start_tool(tc, sequence=0)
        assert span_id is not None
        assert len(starts) == 1

        # 第一次 end
        recorder.end_tool(
            span_id,
            result=ToolResultBlock(tool_use_id="t1", content="ok", is_error=False),
        )
        assert len(ends) == 1

        # 第二次 end（幂等）——不应崩溃
        recorder.end_tool(
            span_id,
            result=ToolResultBlock(tool_use_id="t1", content="ok", is_error=False),
        )
        assert len(ends) == 1, "幂等：第二次 end_tool 不应产生新 span 记录"


def test_trace_recorder_budget_falls_back_to_chat_span():
    """S1.5：budget 钩子定位本轮最后 tool span，无则回退 chat span。"""
    from unittest.mock import patch

    budget_targets = []

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        return f"span-{name}-{iteration}"

    def fake_end(span_id, **kwargs):
        pass

    def fake_budget(span_id, budget_message):
        budget_targets.append(span_id)

    recorder = llm_assist.TraceRecorder("run-test", start_iteration=1)
    # 仅有 chat span，无 tool span
    recorder._chat_span_id = "span-llm_chat-0"

    with (
        patch.object(llm_assist, "trace_start_span", side_effect=fake_start),
        patch.object(llm_assist, "trace_end_span", side_effect=fake_end),
        patch.object(
            llm_assist, "trace_record_budget_message", side_effect=fake_budget
        ),
    ):
        recorder.record_budget("[剩余轮次：2]")
        assert len(budget_targets) == 1
        assert budget_targets[0] == "span-llm_chat-0", "无 tool span 时应回退 chat span"


# ---------------------------------------------------------------------------
# P0-1：wrap_chat_fn chat span 与 tool span 同轮 iteration 一致
# ---------------------------------------------------------------------------


def test_wrap_chat_fn_chat_and_tool_same_iteration():
    """P0-1：同一轮内 chat span 与 tool span 的 iteration 必须一致；连续两轮时第二轮 iteration=1。"""
    import asyncio
    from unittest.mock import patch

    from app.services.llm.models import Message

    starts = []
    ends = []

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        starts.append({"name": name, "iteration": iteration, "sequence": sequence})
        return f"span-{name}-{iteration}-{sequence}"

    def fake_end(span_id, **kwargs):
        ends.append({"span_id": span_id, **kwargs})

    recorder = llm_assist.TraceRecorder("run-test", start_iteration=0)

    # chat_fn 返回含 tool_calls 的响应 → 模拟工具执行后调用 start_tool
    async def dummy_chat(messages, *, tools=None, tool_choice=None):
        return ChatResponse(
            content="",
            stop_reason="tool_use",
            blocks=[ToolUseBlock(id="t1", name="search_bangumi", input={"title": "x"})],
            model="test-model",
        )

    wrapped = recorder.wrap_chat_fn(dummy_chat)

    with (
        patch.object(llm_assist, "trace_start_span", side_effect=fake_start),
        patch.object(llm_assist, "trace_end_span", side_effect=fake_end),
    ):
        # 第一轮
        asyncio.run(wrapped([Message(role="user", content="hi")], tools=[]))
        # 模拟工具执行（与 chat 同轮）
        tc = ToolUseBlock(id="t1", name="search_bangumi", input={"title": "x"})
        recorder.start_tool(tc, sequence=0)

        # 第二轮
        asyncio.run(wrapped([Message(role="user", content="hi2")], tools=[]))
        tc2 = ToolUseBlock(
            id="t2", name="get_subject_detail", input={"subject_id": "1"}
        )
        recorder.start_tool(tc2, sequence=0)

    # 提取 chat 和 tool 的 iteration
    chat_starts = [s for s in starts if s["name"] == "llm_chat"]
    tool_starts = [s for s in starts if s["name"] == "tool_execute"]

    assert len(chat_starts) == 2, f"应有 2 条 chat span，实际 {len(chat_starts)}"
    assert len(tool_starts) == 2, f"应有 2 条 tool span，实际 {len(tool_starts)}"

    # 第一轮：chat 与 tool 同 iteration
    assert chat_starts[0]["iteration"] == 0, (
        f"第一轮 chat iteration 应为 0，实际 {chat_starts[0]['iteration']}"
    )
    assert tool_starts[0]["iteration"] == 0, (
        f"第一轮 tool iteration 应与 chat 一致为 0，实际 {tool_starts[0]['iteration']}"
    )

    # 第二轮：chat 与 tool 同 iteration = 1
    assert chat_starts[1]["iteration"] == 1, (
        f"第二轮 chat iteration 应为 1，实际 {chat_starts[1]['iteration']}"
    )
    assert tool_starts[1]["iteration"] == 1, (
        f"第二轮 tool iteration 应与 chat 一致为 1，实际 {tool_starts[1]['iteration']}"
    )


# ---------------------------------------------------------------------------
# P0-2：wrap_chat_fn 异常不得被 UnboundLocalError 遮蔽
# ---------------------------------------------------------------------------


def test_wrap_chat_fn_exception_propagates_original():
    """P0-2：chat_fn 抛 ValueError('boom') → 捕获的必须是 ValueError('boom')，不是 UnboundLocalError。"""
    import asyncio
    from unittest.mock import patch

    from app.services.llm.models import Message

    ends = []

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        return f"span-{name}-{iteration}-{sequence}"

    def fake_end(span_id, **kwargs):
        ends.append({"span_id": span_id, **kwargs})

    recorder = llm_assist.TraceRecorder("run-test", start_iteration=0)

    async def exploding_chat(messages, *, tools=None, tool_choice=None):
        raise ValueError("boom")

    wrapped = recorder.wrap_chat_fn(exploding_chat)

    with (
        patch.object(llm_assist, "trace_start_span", side_effect=fake_start),
        patch.object(llm_assist, "trace_end_span", side_effect=fake_end),
    ):
        with pytest.raises(ValueError, match="boom"):
            asyncio.run(wrapped([Message(role="user", content="hi")], tools=[]))

    # 不应写 ok span（允许写 error span，但 status 不得为 "ok"）
    ok_ends = [e for e in ends if e.get("status") == "ok"]
    assert len(ok_ends) == 0, f"异常时不应写 ok span，实际写了 {len(ok_ends)} 条"


# ---------------------------------------------------------------------------
# P1：_build_default_chat_fn 必填 thinking_level
# ---------------------------------------------------------------------------


def test_build_default_chat_fn_requires_thinking_level():
    """P1：_build_default_chat_fn 不传 thinking_level 应抛 TypeError。"""
    with pytest.raises(TypeError):
        llm_assist._build_default_chat_fn()


# ---------------------------------------------------------------------------
# P1-a：_persist_llm_candidate ended_at 必须为 epoch 整数（与 mark_succeeded 一致）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P1-b：TraceRecorder 增加必填 start_iteration 构造参数
# ---------------------------------------------------------------------------


def test_trace_recorder_requires_start_iteration():
    """P1-b：TraceRecorder 构造函数要求必填 start_iteration（不得有默认值）。"""
    import inspect

    sig = inspect.signature(llm_assist.TraceRecorder.__init__)
    param = sig.parameters.get("start_iteration")
    assert param is not None, "TraceRecorder.__init__ 应有 start_iteration 参数"
    assert param.default is inspect.Parameter.empty, (
        "start_iteration 不得有默认值（必须显式传入）"
    )


def test_trace_recorder_start_iteration_affects_first_chat_iteration():
    """P1-b：start_iteration=N → 首轮 chat span iteration=N。"""
    import asyncio
    from unittest.mock import patch

    from app.services.llm.models import Message

    starts = []

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        starts.append({"name": name, "iteration": iteration, "sequence": sequence})
        return f"span-{name}-{iteration}-{sequence}"

    recorder = llm_assist.TraceRecorder("run-test", start_iteration=5)

    async def dummy_chat(messages, *, tools=None, tool_choice=None):
        return ChatResponse(content="", stop_reason="end_turn", blocks=[], model="m")

    wrapped = recorder.wrap_chat_fn(dummy_chat)

    with patch.object(llm_assist, "trace_start_span", side_effect=fake_start):
        asyncio.run(wrapped([Message(role="user", content="hi")], tools=[]))

    chat_starts = [s for s in starts if s["name"] == "llm_chat"]
    assert len(chat_starts) == 1
    assert chat_starts[0]["iteration"] == 5, (
        f"首轮 chat iteration 应等于 start_iteration=5，实际 {chat_starts[0]['iteration']}"
    )


# ---------------------------------------------------------------------------
# P1-c：TraceRecorder.begin_replayed_round 锚定到指定轮次
# ---------------------------------------------------------------------------


def test_trace_recorder_begin_replayed_round_sets_current_iteration():
    """P1-c：begin_replayed_round(N) 后 start_tool 的 iteration=N。"""
    from unittest.mock import patch

    from app.services.llm.models import ToolUseBlock

    starts = []

    def fake_start(run_id, name, iteration, sequence, parent_id=""):
        starts.append({"name": name, "iteration": iteration, "sequence": sequence})
        return f"span-{name}-{iteration}-{sequence}"

    recorder = llm_assist.TraceRecorder("run-test", start_iteration=3)
    recorder.begin_replayed_round(7)

    tc = ToolUseBlock(id="t1", name="search_bangumi", input={"title": "x"})
    with patch.object(llm_assist, "trace_start_span", side_effect=fake_start):
        recorder.start_tool(tc, sequence=0)

    tool_starts = [s for s in starts if s["name"] == "tool_execute"]
    assert len(tool_starts) == 1
    assert tool_starts[0]["iteration"] == 7, (
        f"begin_replayed_round(7) 后 tool iteration 应为 7，实际 {tool_starts[0]['iteration']}"
    )


# ---------------------------------------------------------------------------
# T4：LLMCallError 处理（run 捕获分流 + _handle_result 新增分支）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_llm_call_error_retryable_true_increments_attempts(
    monkeypatch,
):
    """LLMCallError(retryable=True) → increment_attempts，返回 processing（attempts<3）。"""
    from app.services.llm.client import LLMCallError

    run_id = "run-e-retryable"
    sr_id = 500
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    async def _boom(messages, *, tools=None, tool_choice=None):
        raise LLMCallError("429 rate limited", retryable=True)

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=_make_bgm(),
        thinking_level="medium",
        chat_fn=_boom,
        span_recorder=None,
    )

    assert status == "processing"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["attempts"] == 1


@pytest.mark.asyncio
async def test_run_llm_call_error_retryable_false_immediately_failed(monkeypatch):
    """LLMCallError(retryable=False) → 立即 mark_failed(stop_reason='llm_error')，返回 failed。"""
    from app.services.llm.client import LLMCallError

    run_id = "run-e-terminal"
    sr_id = 501
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    async def _boom(messages, *, tools=None, tool_choice=None):
        raise LLMCallError("401 Unauthorized", retryable=False)

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=_make_bgm(),
        thinking_level="medium",
        chat_fn=_boom,
        span_recorder=None,
    )

    assert status == "failed"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "failed"
    assert run_row["stop_reason"] == "llm_error"
    assert "401" in (run_row["last_error"] or "")


@pytest.mark.asyncio
async def test_run_llm_call_error_retryable_true_three_times_failed(monkeypatch):
    """LLMCallError(retryable=True) 连续 3 次 → increment_attempts 单点置终态 failed。"""
    from app.services.llm.client import LLMCallError

    run_id = "run-e-retry3"
    sr_id = 502
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    call_count = {"n": 0}

    async def _boom(messages, *, tools=None, tool_choice=None):
        call_count["n"] += 1
        raise LLMCallError("500 Internal Server Error", retryable=True)

    # 第 1 次 → processing (attempts=1)
    status1 = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=_make_bgm(),
        thinking_level="medium",
        chat_fn=_boom,
        span_recorder=None,
    )
    assert status1 == "processing"

    # 重新 claim 并跑第 2 次 → processing (attempts=2)
    database_manager.agent_runs.update_run_status(run_id, "pending")
    status2 = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=_make_bgm(),
        thinking_level="medium",
        chat_fn=_boom,
        span_recorder=None,
    )
    assert status2 == "processing"

    # 第 3 次 → failed (attempts=3)
    database_manager.agent_runs.update_run_status(run_id, "pending")
    status3 = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=_make_bgm(),
        thinking_level="medium",
        chat_fn=_boom,
        span_recorder=None,
    )
    assert status3 == "failed"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "failed"
    # 终态由 increment_attempts 单点事务写入（stop_reason='failed'），外层不再二次 mark_failed
    assert run_row["stop_reason"] == "failed"


@pytest.mark.asyncio
async def test_handle_result_llm_error_marks_failed(monkeypatch):
    """_handle_result 对 stop_reason='llm_error' → mark_failed(stop_reason='llm_error')。"""
    from unittest.mock import MagicMock

    from app.services.agent.loop import RunResult

    dbm = MagicMock()
    result = RunResult(stop_reason="llm_error")

    status = llm_assist._handle_result(
        dbm,
        "run-llm-err",
        result,
        sync_record=_make_sync_record(sync_record_id=510),
        sync_record_id=510,
        bgm=None,
        notification_service=None,
        total_tokens=0,
    )

    assert status == "failed"
    dbm.agent_runs.mark_failed.assert_called_once()
    call_kwargs = dbm.agent_runs.mark_failed.call_args[1]
    assert call_kwargs.get("stop_reason") == "llm_error"


@pytest.mark.asyncio
async def test_handle_result_max_tokens_marks_failed(monkeypatch):
    """_handle_result 对 stop_reason='max_tokens' → mark_failed(stop_reason='max_tokens')。"""
    from unittest.mock import MagicMock

    from app.services.agent.loop import RunResult

    dbm = MagicMock()
    result = RunResult(stop_reason="max_tokens")

    status = llm_assist._handle_result(
        dbm,
        "run-max-tok",
        result,
        sync_record=_make_sync_record(sync_record_id=511),
        sync_record_id=511,
        bgm=None,
        notification_service=None,
        total_tokens=0,
    )

    assert status == "failed"
    dbm.agent_runs.mark_failed.assert_called_once()
    call_kwargs = dbm.agent_runs.mark_failed.call_args[1]
    assert call_kwargs.get("stop_reason") == "max_tokens"


@pytest.mark.asyncio
async def test_handle_result_end_turn_still_no_suggestion(monkeypatch):
    """_handle_result 对 stop_reason='end_turn' 仍落 no_suggestion（语义不变）。"""
    from unittest.mock import MagicMock

    from app.services.agent.loop import RunResult

    dbm = MagicMock()
    result = RunResult(stop_reason="end_turn")

    status = llm_assist._handle_result(
        dbm,
        "run-end-turn",
        result,
        sync_record=_make_sync_record(sync_record_id=512),
        sync_record_id=512,
        bgm=None,
        notification_service=None,
        total_tokens=0,
    )

    assert status == "no_suggestion"
    dbm.agent_runs.mark_no_suggestion.assert_called_once_with(
        "run-end-turn", stop_reason="end_turn"
    )


def test_begin_replayed_round_sets_next_iteration_to_iteration_plus_one():
    """begin_replayed_round(iteration) 应设 _current_iteration=iteration 且 _next_iteration=iteration+1。"""
    recorder = llm_assist.TraceRecorder("run-test", start_iteration=0)
    recorder.begin_replayed_round(5)
    assert recorder._current_iteration == 5
    assert recorder._next_iteration == 6


# ---------------------------------------------------------------------------
# P1-a 回归：_persist_llm_candidate 写入 agent_runs.ended_at 为 epoch 整数
# ---------------------------------------------------------------------------


def test_persist_llm_candidate_ended_at_is_epoch_integer(monkeypatch):
    """P1-a：_persist_llm_candidate 写入 agent_runs.ended_at 为 epoch 整数。"""
    import time

    run_id = "run-ended-at"
    sr_id = 300
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    database_manager.log_pending_candidate(
        request_title=f"rule-{sr_id}",
        request_season=1,
        user_name="alice",
        source="plex",
        candidates=[{"subject_id": "111", "name": "A", "score": 0.8}],
        sync_record_id=sr_id,
    )
    sr = _make_sync_record(with_candidates=True, sync_record_id=sr_id)

    before = int(time.time())
    llm_assist._persist_llm_candidate(
        database_manager,
        run_id=run_id,
        sync_record_id=sr_id,
        sync_record=sr,
        subject_id="123",
        reason="r",
        stop_reason="submit_suggestion",
        bgm=None,
    )
    after = int(time.time())

    run_row = database_manager.agent_runs.get_run(run_id)
    ended_at = run_row["ended_at"]
    # 断言 ended_at 为 int 且 > 0
    assert isinstance(ended_at, int), f"ended_at 应为 int，实际 {type(ended_at)}"
    assert ended_at > 0, "ended_at 应 > 0"
    assert before <= ended_at <= after + 1, (
        f"ended_at 应在 [{before}, {after}+1] 范围内，实际 {ended_at}"
    )

    # 断言 _iso_from_epoch 返回非 None（可解析为 ISO 字符串）
    from app.api.agent_runs import _iso_from_epoch

    iso = _iso_from_epoch(ended_at)
    assert iso is not None, "_iso_from_epoch(ended_at) 应返回非 None"

    # 断言 SQLite typeof(ended_at)='integer'
    conn = database_manager._connection._conn
    cur = conn.execute(
        "SELECT typeof(ended_at) FROM agent_runs WHERE run_id=?", (run_id,)
    )
    row = cur.fetchone()
    assert row[0] == "integer", f"SQLite typeof(ended_at) 应为 'integer'，实际 {row[0]}"


# ---------------------------------------------------------------------------
# 恢复续跑单一入口 continue_run
# （由调度器 _recover_run 调用：replay → 补执行 → 续跑 → 落库）
# ---------------------------------------------------------------------------


def _make_continuation_repo() -> MagicMock:
    """恢复续跑测试用 agent_runs repo mock（increment_attempts 默认未达上限）。"""
    repo = MagicMock()
    repo.increment_attempts.return_value = 1
    return repo


def _make_continuation_dbm(repo: MagicMock) -> MagicMock:
    dbm = MagicMock()
    dbm.agent_runs = repo
    return dbm


def _make_replay_result(
    *,
    executed: int = 0,
    missing: list | None = None,
    last_response: dict | None = None,
    messages: list | None = None,
    total_tokens: int = 0,
) -> ReplayResult:
    return ReplayResult(
        messages=messages or [Message(role="system", content="s")],
        executed_iterations=executed,
        missing_tool_calls=missing or [],
        last_response=last_response,
        total_tokens=total_tokens,
    )


def _medium_cfg(raw_max: str = "") -> dict:
    return {
        "llm_match_thinking_level": "medium",
        "llm_match_max_iterations": raw_max,
    }


# S3：end_turn 分派 ---------------------------------------------------------


def test_continue_run_end_turn_marks_no_suggestion_without_llm():
    """last_response=end_turn → 直接 mark_no_suggestion，不调 LLM（loop_run）。"""
    repo = _make_continuation_repo()
    rr = _make_replay_result(
        executed=1,
        last_response={"stop_reason": "end_turn", "content": "x", "tool_calls": []},
    )
    loop = AsyncMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.loop_run", loop),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r", {"id": 1}, MagicMock()))

    repo.mark_no_suggestion.assert_called_once_with("r", stop_reason="end_turn")
    loop.assert_not_awaited()


# S4：submit 分派 -----------------------------------------------------------


def test_continue_run_submit_suggestion_dispatches_to_handle_result():
    """last_response=submit_suggestion → 捕获参数走校验落库路径（_handle_result）。"""
    repo = _make_continuation_repo()
    handle = MagicMock()
    fake_svc = MagicMock()
    rr = _make_replay_result(
        executed=1,
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
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.loop_run", loop),
        patch("app.services.matching.llm_assist._handle_result", handle),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(
            llm_assist.continue_run(
                "r", {"id": 1}, MagicMock(), notification_service=fake_svc
            )
        )

    handle.assert_called_once()
    result_arg = handle.call_args[0][2]  # _handle_result(dbm, run_id, result, ...)
    assert result_arg.stop_reason == "submit_suggestion"
    assert result_arg.suggestion == {"subject_id": "123", "reason": "跨季匹配"}
    # 接线断言：notification_service 必须透传（生产链路真正发送站内信）
    assert handle.call_args[1].get("notification_service") is fake_svc
    loop.assert_not_awaited()


def test_continue_run_submit_uses_replayed_total_tokens():
    """P2-1：submit_suggestion 直接分派分支把 replay 累计 tokens 传给 _handle_result。

    恢复路径无实时 recorder，历史轮次 tokens 由 ``ReplayResult.total_tokens`` 携带；
    不得再硬编码 0。
    """
    repo = _make_continuation_repo()
    handle = MagicMock()
    rr = _make_replay_result(
        executed=1,
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
        total_tokens=150,
    )
    loop = AsyncMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.loop_run", loop),
        patch("app.services.matching.llm_assist._handle_result", handle),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r-tok", {"id": 1}, MagicMock()))

    handle.assert_called_once()
    assert handle.call_args[1].get("total_tokens") == 150, (
        f"恢复 submit 分支应传 replay 累计 tokens=150，"
        f"实际 {handle.call_args[1].get('total_tokens')}"
    )
    loop.assert_not_awaited()


# S5：补执行缺失工具 ---------------------------------------------------------


def test_continue_run_tool_use_backfills_and_continues_loop():
    """last_response 含 tool_use（缺失工具）→ 补执行缺失工具 + 回填后继续 loop_run。"""
    repo = _make_continuation_repo()
    missing = {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}}
    rr = _make_replay_result(
        executed=0,
        missing=[missing],
        last_response={
            "stop_reason": "tool_use",
            "content": "go",
            "tool_calls": [missing],
        },
    )
    backfill = AsyncMock()
    loop = AsyncMock(return_value=RunResult(stop_reason="end_turn"))
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.loop_run", loop),
        patch("app.services.matching.llm_assist._replay_missing_tool", backfill),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r", {"id": 1}, MagicMock()))

    # 缺失工具补执行一次，并锚定同轮 span（sequence=0）
    backfill.assert_awaited_once()
    assert backfill.await_args.args[0] == missing
    assert backfill.await_args.kwargs.get("sequence") == 0
    assert backfill.await_args.kwargs.get("span_recorder") is not None
    # 回填后进入下一轮 loop_run 续跑（1 次 LLM 调用）
    loop.assert_awaited_once()


# S6：last_response=None 完整续跑 -------------------------------------------


def test_continue_run_last_response_none_runs_loop_and_lands_result():
    """last_response=None → loop 从 executed_iterations 续跑并落 RunResult。"""
    repo = _make_continuation_repo()
    handle = MagicMock()
    seed = [Message(role="system", content="s"), Message(role="user", content="u")]
    rr = _make_replay_result(executed=2, missing=[], last_response=None, messages=seed)
    loop = AsyncMock(return_value=RunResult(stop_reason="end_turn"))
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.loop_run", loop),
        patch("app.services.matching.llm_assist._handle_result", handle),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r", {"id": 1}, MagicMock()))

    loop.assert_awaited_once()
    assert loop.await_args.kwargs["seed_messages"] is seed
    # medium=3，executed=2 → 剩余 1 轮
    assert loop.await_args.kwargs["max_iterations"] == 1
    handle.assert_called_once()
    assert handle.call_args[0][2].stop_reason == "end_turn"


# G3：config_override 非正数回退 None ---------------------------------------


def _run_continue_run_with_config(raw_max: str):
    """以指定 llm_match_max_iterations 跑一次 continue_run，返回捕获的 config_override。"""
    repo = _make_continuation_repo()
    captured: dict = {}

    def _gmi(task_type, thinking_level, config_override=None):
        captured["config_override"] = config_override
        return 3

    log = MagicMock()
    rr = _make_replay_result(
        executed=0,
        last_response={"stop_reason": "end_turn", "content": "x", "tool_calls": []},
    )
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.get_max_iterations", _gmi),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.logger", log),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg(raw_max)
        asyncio.run(llm_assist.continue_run("r", {"id": 1}, MagicMock()))
    return captured.get("config_override"), log


def test_continue_run_zero_config_override_falls_back_to_none():
    override, log = _run_continue_run_with_config("0")
    assert override is None, "0 应回退 None（避免 max_iterations=0 空跑）"
    assert log.warning.called


def test_continue_run_negative_config_override_falls_back_to_none():
    override, log = _run_continue_run_with_config("-3")
    assert override is None, "负值应回退 None"
    assert log.warning.called


def test_continue_run_positive_config_override_is_passed_through():
    override, _ = _run_continue_run_with_config("7")
    assert override == 7


# G4：预算耗尽必须落终态 -----------------------------------------------------


def test_continue_run_tool_use_no_remaining_marks_no_suggestion():
    """tool_use 补执行后 remaining<=0 → 落 no_suggestion/exhausted，不续跑。"""
    repo = _make_continuation_repo()
    missing = {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}}
    rr = _make_replay_result(
        executed=2,  # medium=3 → remaining=1；补执行后 -1 → 0
        missing=[missing],
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
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist._replay_missing_tool", backfill),
        patch("app.services.matching.llm_assist.loop_run", loop),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r", {"id": 1}, MagicMock()))

    # 不再续跑 loop，但必须落终态（否则 run 永久 processing）
    loop.assert_not_awaited()
    backfill.assert_not_awaited()
    repo.mark_no_suggestion.assert_called_once_with("r", stop_reason="exhausted")


def test_continue_run_no_remaining_before_replay_marks_no_suggestion():
    """replay 后剩余轮次已耗尽（remaining<=0）同样必须落终态。"""
    repo = _make_continuation_repo()
    rr = _make_replay_result(
        executed=3,  # medium=3 → remaining=0
        missing=[],
        last_response=None,
    )
    loop = AsyncMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.loop_run", loop),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r", {"id": 1}, MagicMock()))

    loop.assert_not_awaited()
    repo.mark_no_suggestion.assert_called_once_with("r", stop_reason="exhausted")


def test_continue_run_replay_exhausted_logs_warning():
    """预算耗尽（replay 前）→ warning 且注明恢复(replay)路径，不得停留在 debug。"""
    repo = _make_continuation_repo()
    rr = _make_replay_result(executed=3, missing=[], last_response=None)
    log = MagicMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.logger", log),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r", {"id": 1}, MagicMock()))

    repo.mark_no_suggestion.assert_called_once_with("r", stop_reason="exhausted")
    warnings = [str(c.args[0]) for c in log.warning.call_args_list]
    assert any("恢复" in m or "replay" in m.lower() for m in warnings), (
        f"预算耗尽应 warning 且注明恢复(replay)路径，实际 {warnings}"
    )
    debugs = [str(c.args[0]) for c in log.debug.call_args_list]
    assert not any("已无剩余轮次" in m for m in debugs), (
        "预算耗尽日志不应停留在 debug 级"
    )


def test_continue_run_replay_exhausted_after_backfill_logs_warning():
    """补执行后预算耗尽 → warning 且注明恢复(replay)路径。"""
    repo = _make_continuation_repo()
    missing = {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}}
    rr = _make_replay_result(
        executed=2,
        missing=[missing],
        last_response={
            "stop_reason": "tool_use",
            "content": "go",
            "tool_calls": [missing],
        },
    )
    log = MagicMock()
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch("app.services.matching.llm_assist.logger", log),
        patch("app.services.matching.llm_assist._replay_missing_tool", new=AsyncMock()),
        patch("app.services.matching.llm_assist.loop_run", new=AsyncMock()),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r2", {"id": 1}, MagicMock()))

    repo.mark_no_suggestion.assert_called_once_with("r2", stop_reason="exhausted")
    warnings = [str(c.args[0]) for c in log.warning.call_args_list]
    assert any("恢复" in m or "replay" in m.lower() for m in warnings), (
        f"补执行后预算耗尽应 warning 且注明恢复(replay)路径，实际 {warnings}"
    )
    debugs = [str(c.args[0]) for c in log.debug.call_args_list]
    assert not any("已无剩余轮次" in m for m in debugs), (
        "补执行后预算耗尽日志不应停留在 debug 级"
    )


# S7：LLM 调用失败按可重试性分流 --------------------------------------------


def test_continue_run_llm_call_error_retryable_false_marks_failed():
    """LLMCallError(retryable=False) → mark_failed(stop_reason='llm_error')。"""
    from app.services.llm.client import LLMCallError

    repo = _make_continuation_repo()
    rr = _make_replay_result(executed=0, missing=[], last_response=None)

    async def _boom(messages, *, tools=None, tool_choice=None):
        raise LLMCallError("401 Unauthorized", retryable=False)

    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch(
            "app.services.matching.llm_assist._build_default_chat_fn",
            return_value=_boom,
        ),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r-err-terminal", {"id": 1}, MagicMock()))

    repo.mark_failed.assert_called_once()
    call_kwargs = repo.mark_failed.call_args[1]
    assert call_kwargs.get("stop_reason") == "llm_error"
    assert "401" in call_kwargs.get("last_error", "")
    repo.increment_attempts.assert_not_called()


def test_continue_run_llm_call_error_retryable_true_increments_attempts():
    """LLMCallError(retryable=True) → increment_attempts 并携带 last_error。"""
    from app.services.llm.client import LLMCallError

    repo = _make_continuation_repo()
    rr = _make_replay_result(executed=0, missing=[], last_response=None)

    async def _boom(messages, *, tools=None, tool_choice=None):
        raise LLMCallError("500 Internal Server Error", retryable=True)

    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch(
            "app.services.matching.llm_assist._build_default_chat_fn",
            return_value=_boom,
        ),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r-err-retry", {"id": 1}, MagicMock()))

    repo.increment_attempts.assert_called_once_with(
        "r-err-retry", last_error="500 Internal Server Error"
    )
    repo.mark_failed.assert_not_called()


def test_continue_run_outer_exception_logs_current_run_status():
    """P2-3：最外层 except 日志须带当前 run 状态，便于定位「已终态后的异常」。"""
    repo = _make_continuation_repo()
    repo.get_run.return_value = {"status": "processing"}
    rr = _make_replay_result(executed=0, missing=[], last_response=None)
    log = MagicMock()

    async def _chat_fn(messages, *, tools=None, tool_choice=None):
        return ChatResponse(content="done", stop_reason="end_turn")

    boom_loop = AsyncMock(side_effect=RuntimeError("kaboom"))
    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch(
            "app.services.matching.llm_assist._build_default_chat_fn",
            return_value=_chat_fn,
        ),
        patch("app.services.matching.llm_assist.loop_run", boom_loop),
        patch("app.services.matching.llm_assist.logger", log),
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        asyncio.run(llm_assist.continue_run("r-outer-err", {"id": 1}, MagicMock()))

    errors = [str(c.args[0]) for c in log.error.call_args_list]
    assert any("processing" in m for m in errors), (
        f"最外层异常日志应包含当前 run 状态(processing)，实际 {errors}"
    )
    repo.increment_attempts.assert_called_once()


# F4 / G5：缺失工具补执行（readonly 执行 / 非 read 占位闭合协议） -------------


def test_replay_missing_tool_appends_tool_result_to_messages():
    """缺失的 read 工具补执行 → 结果回填 messages（assistant 后有 tool_result）。"""

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
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="t2", name="get_subject_detail", input={"subject_id": "2"}
                )
            ],
        ),
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
    asyncio.run(
        llm_assist._replay_missing_tool(
            {"id": "t2", "name": "get_subject_detail", "input": {"subject_id": "2"}},
            registry,
            messages,
        )
    )

    after = _count_tool_results(messages)
    assert after == before + 1 == 2  # t1（已记录）+ t2（补执行）
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
    """write 缺失工具不重放副作用，但回填占位 tool_result 闭合协议。"""
    called: list = []
    registry = _registry_with("write_mapping", "write", called)
    messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="w1", name="write_mapping", input={})],
        )
    ]

    asyncio.run(
        llm_assist._replay_missing_tool(
            {"id": "w1", "name": "write_mapping", "input": {}}, registry, messages
        )
    )

    assert called == []
    blk = _last_tool_result(messages)
    assert blk is not None, "write 缺失工具也必须回填 tool_result 闭合协议"
    assert blk.tool_use_id == "w1"
    assert blk.is_error is False
    assert blk.content == _SKIP_PLACEHOLDER


def test_replay_missing_terminal_tool_appends_placeholder_tool_result():
    """terminal 缺失工具不重放，回填占位 tool_result。"""
    called: list = []
    registry = _registry_with("submit_suggestion", "terminal", called)
    messages: list = []

    asyncio.run(
        llm_assist._replay_missing_tool(
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
    """未注册工具同样回填占位，避免 tool_use 悬空。"""
    registry = ToolRegistry()
    messages: list = []

    asyncio.run(
        llm_assist._replay_missing_tool(
            {"id": "u1", "name": "ghost_tool", "input": {}}, registry, messages
        )
    )

    blk = _last_tool_result(messages)
    assert blk is not None, "未注册工具同样需回填占位，避免 tool_use 悬空"
    assert blk.tool_use_id == "u1"
    assert blk.content == _SKIP_PLACEHOLDER


# P1-c：补执行落 tool_execute span（二次 replay 不再判缺失） -------------------


def test_replay_missing_tool_writes_span_and_second_replay_not_missing(monkeypatch):
    """补执行缺失工具后写 tool_execute span，二次 replay 不再判缺失。"""
    from app.services.agent.trace import replay as real_replay
    from app.services.llm.tools import get_tool_registry

    run_id = "run-continue-replay-span"
    sr_id = 401
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    call_count = {"n": 0}

    async def _chat(
        messages, *, tools=None, tool_choice=None, job_name=None, thinking_level=None
    ):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ChatResponse(
                content="",
                stop_reason="tool_use",
                blocks=[
                    ToolUseBlock(id="t1", name="search_bangumi", input={"title": "x"})
                ],
            )
        return ChatResponse(content="done", stop_reason="end_turn")

    client = MagicMock()
    client.chat = AsyncMock(side_effect=_chat)
    monkeypatch.setattr(
        "app.services.matching.llm_assist.get_llm_client", lambda: client
    )

    class _Bgm:
        def search(self, **kwargs):
            return [{"id": 1, "name": "result"}]

        def get_subject(self, sid):
            return {"name": f"s-{sid}", "name_cn": f"条目-{sid}"}

        def get_related_subjects(self, sid):
            return []

    bgm = _Bgm()
    reg = get_tool_registry()
    eb_calls = {"n": 0}

    async def _eb(tool_calls, *, recorder=None):
        eb_calls["n"] += 1
        if eb_calls["n"] == 1:
            raise RuntimeError("crash mid-exec")
        return {
            tc.id: ToolResultBlock(tool_use_id=tc.id, content="ok", is_error=False)
            for tc in tool_calls
        }

    monkeypatch.setattr(reg, "execute_batch", _eb)

    async def _go():
        # 初始正常运行至崩溃（记录 1 条 llm_chat span，但 tool_execute 缺失）
        await llm_assist.run(run_id, sync_record=sr, bgm=bgm, thinking_level="medium")
        # 恢复续跑：真实 trace.replay + 真实 loop_run
        await llm_assist.continue_run(run_id, sr, bgm)

    asyncio.run(_go())

    steps = database_manager.agent_runs.get_steps(run_id)
    tool_steps = [s for s in steps if s["name"] == "tool_execute"]
    assert len(tool_steps) >= 1, (
        f"补执行应产生至少 1 条 tool_execute span，实际 {len(tool_steps)}"
    )

    rr2 = real_replay(run_id)
    assert len(rr2.missing_tool_calls) == 0, (
        f"二次 replay 不应再判缺失，实际 missing={rr2.missing_tool_calls}"
    )


# 端到端断点恢复：恢复续跑不重复调 LLM ---------------------------------------


def test_continue_run_recovery_no_double_llm_call(monkeypatch):
    """崩溃前 1 次 + 恢复后 1 次 → 累计 LLM 调用 2，run 落 no_suggestion。"""
    from app.services.llm.tools import get_tool_registry

    run_id = "run-continue-m22"
    sr_id = 992
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    spy_calls = {"n": 0}

    async def _chat(
        messages, *, tools=None, tool_choice=None, job_name=None, thinking_level=None
    ):
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
    monkeypatch.setattr(
        "app.services.matching.llm_assist.get_llm_client", lambda: client
    )

    class _Bgm:
        def search(self, **kwargs):
            return [{"id": 1, "name": "foo"}]

        def get_subject(self, sid):
            return {"name": f"subject-{sid}", "name_cn": f"条目-{sid}"}

        def get_related_subjects(self, sid):
            return []

    bgm = _Bgm()
    reg = get_tool_registry()
    eb_calls = {"n": 0}

    async def _eb(tool_calls, *, recorder=None):
        eb_calls["n"] += 1
        if eb_calls["n"] == 1:
            raise RuntimeError("crash mid-exec")
        return {
            tc.id: ToolResultBlock(tool_use_id=tc.id, content="ok", is_error=False)
            for tc in tool_calls
        }

    monkeypatch.setattr(reg, "execute_batch", _eb)

    async def _go():
        await llm_assist.run(run_id, sync_record=sr, bgm=bgm, thinking_level="medium")
        await llm_assist.continue_run(run_id, sr, bgm)

    asyncio.run(_go())

    assert chat.call_count == 2, f"期望累计 2 次 LLM 调用，实际 {chat.call_count}"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["status"] == "no_suggestion"


# P0-3：恢复续跑写 chat span + thinking_level 透传 -------------------------


def test_continue_run_writes_chat_span(monkeypatch):
    """续跑轮应产生 chat span（last_response=None 进入通用循环）。"""
    run_id = "run-continue-chat-span"
    sr_id = 993
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    async def _chat(
        messages, *, tools=None, tool_choice=None, job_name=None, thinking_level=None
    ):
        return ChatResponse(content="done", stop_reason="end_turn")

    client = MagicMock()
    client.chat = AsyncMock(side_effect=_chat)
    monkeypatch.setattr(
        "app.services.matching.llm_assist.get_llm_client", lambda: client
    )

    rr = _make_replay_result(
        executed=0,
        missing=[],
        last_response=None,
        messages=[
            Message(role="system", content="s"),
            Message(role="user", content="u"),
        ],
    )
    monkeypatch.setattr("app.services.agent.trace.replay", lambda rid: rr)

    asyncio.run(llm_assist.continue_run(run_id, sr, MagicMock()))

    steps = database_manager.agent_runs.get_steps(run_id)
    chat_steps = [s for s in steps if s["name"] == "llm_chat"]
    assert len(chat_steps) >= 1, (
        f"恢复路径应产生至少 1 条 chat span，实际 {len(chat_steps)}"
    )


def test_continue_run_respects_thinking_level(monkeypatch):
    """配置 thinking_level='high' → _build_default_chat_fn 收到 'high'。"""
    repo = _make_continuation_repo()
    captured: dict = {}

    def _fake_build(thinking_level):
        captured["thinking_level"] = thinking_level

        async def chat_fn(messages, *, tools=None, tool_choice=None):
            return ChatResponse(content="done", stop_reason="end_turn")

        return chat_fn

    rr = _make_replay_result(executed=0, missing=[], last_response=None)

    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager") as cm,
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch(
            "app.services.matching.llm_assist._build_default_chat_fn",
            side_effect=_fake_build,
        ),
    ):
        cm.get_sync_llm_match_config.return_value = {
            "llm_match_thinking_level": "high",
            "llm_match_max_iterations": "",
        }
        asyncio.run(llm_assist.continue_run("r-think", {"id": 1}, MagicMock()))

    assert captured.get("thinking_level") == "high", (
        f"恢复路径应透传 thinking_level='high'，实际 {captured}"
    )


def test_continue_run_normalizes_uppercase_thinking_level(tmp_path):
    """ini 配 'HIGH' → 集中配置归一化为 'high' → 恢复路径透传 'high'。"""
    from app.core.config import ConfigManager

    ini = tmp_path / "config.ini"
    ini.write_text("[sync]\nllm_match_thinking_level = HIGH\n", encoding="utf-8")
    cm = ConfigManager.__new__(ConfigManager)
    cm.platform = "Test"
    cm.cwd = tmp_path
    cm.config_paths = {
        "env": None,
        "mounted": tmp_path / "__no_mounted__.ini",
        "dev": tmp_path / "__no_dev__.ini",
        "default": ini,
    }
    cm.active_config_path = ini
    cm._config_cache = None
    cm._last_modified = 0
    cm._load_config()

    # 前置断言：真实 config 已完成归一化（否则后续透传断言无意义）
    assert cm.get_sync_llm_match_config()["llm_match_thinking_level"] == "high"

    repo = _make_continuation_repo()
    captured: dict = {}

    def _fake_build(thinking_level):
        captured["thinking_level"] = thinking_level

        async def chat_fn(messages, *, tools=None, tool_choice=None):
            return ChatResponse(content="done", stop_reason="end_turn")

        return chat_fn

    rr = _make_replay_result(executed=0, missing=[], last_response=None)

    with (
        patch("app.services.agent.trace.replay", return_value=rr),
        patch("app.services.matching.llm_assist.config_manager", cm),
        patch(
            "app.services.matching.llm_assist.get_database_manager",
            return_value=_make_continuation_dbm(repo),
        ),
        patch(
            "app.services.matching.llm_assist._build_default_chat_fn",
            side_effect=_fake_build,
        ),
    ):
        asyncio.run(
            llm_assist.continue_run("r-think-normalize", {"id": 1}, MagicMock())
        )

    assert captured.get("thinking_level") == "high", (
        f"恢复路径应透传归一化后的 thinking_level='high'，实际 {captured}"
    )


# P1-b：续跑新 span iteration 严格大于既有最大 iteration ----------------------


def test_continue_run_iteration_strictly_greater_than_existing_max(monkeypatch):
    """executed_iterations=2（既有最大 1）→ 续跑 chat span iteration >= 2。"""
    run_id = "run-continue-iter"
    sr_id = 994
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    async def _chat(
        messages, *, tools=None, tool_choice=None, job_name=None, thinking_level=None
    ):
        return ChatResponse(content="done", stop_reason="end_turn")

    client = MagicMock()
    client.chat = AsyncMock(side_effect=_chat)
    monkeypatch.setattr(
        "app.services.matching.llm_assist.get_llm_client", lambda: client
    )

    rr = _make_replay_result(
        executed=2,
        missing=[],
        last_response=None,
        messages=[
            Message(role="system", content="s"),
            Message(role="user", content="u"),
        ],
    )
    monkeypatch.setattr("app.services.agent.trace.replay", lambda rid: rr)

    asyncio.run(llm_assist.continue_run(run_id, sr, MagicMock()))

    steps = database_manager.agent_runs.get_steps(run_id)
    chat_steps = [s for s in steps if s["name"] == "llm_chat"]
    assert len(chat_steps) >= 1, "恢复路径应产生至少 1 条 chat span"
    min_chat_iter = min(s["iteration"] for s in chat_steps)
    assert min_chat_iter >= 2, (
        f"续跑 chat span 的最小 iteration 应 >= 2（接续 executed_iterations=2），"
        f"实际最小 iteration={min_chat_iter}"
    )


# P1-e2e：二次恢复不产生额外 LLM 调用，且 chat span 无撞号 --------------------


@pytest.mark.asyncio
async def test_continue_run_double_recovery_no_extra_llm_call(monkeypatch):
    """崩溃 → 恢复完成 → 再次重置 processing → 第二次恢复不再额外调 LLM。"""
    from app.services.agent.trace import replay as real_replay
    from app.services.llm.tools import get_tool_registry

    run_id = "run-continue-double"
    sr_id = 995
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(sync_record_id=sr_id)

    chat_calls = {"n": 0}

    async def _chat(
        messages, *, tools=None, tool_choice=None, job_name=None, thinking_level=None
    ):
        chat_calls["n"] += 1
        if chat_calls["n"] == 1:
            return ChatResponse(
                content="",
                stop_reason="tool_use",
                blocks=[
                    ToolUseBlock(id="t1", name="search_bangumi", input={"title": "x"})
                ],
            )
        return ChatResponse(content="done", stop_reason="end_turn")

    client = MagicMock()
    client.chat = AsyncMock(side_effect=_chat)
    monkeypatch.setattr(
        "app.services.matching.llm_assist.get_llm_client", lambda: client
    )

    class _Bgm:
        def search(self, **kwargs):
            return [{"id": 1, "name": "result"}]

        def get_subject(self, sid):
            return {"name": f"s-{sid}", "name_cn": f"条目-{sid}"}

        def get_related_subjects(self, sid):
            return []

    bgm = _Bgm()
    reg = get_tool_registry()

    eb_calls = {"n": 0}

    async def _eb(tool_calls, *, recorder=None):
        """模拟 execute_batch：写 tool_execute span（同真实实现），首次调用抛错。"""
        eb_calls["n"] += 1
        if eb_calls["n"] == 1:
            if recorder is not None:
                for i, tc in enumerate(tool_calls):
                    sid = recorder.start_tool(tc, sequence=i)
                    recorder.end_tool(sid, error="crash")
            raise RuntimeError(f"crash #{eb_calls['n']}")
        results = {}
        for i, tc in enumerate(tool_calls):
            sid = recorder.start_tool(tc, sequence=i) if recorder else None
            blk = ToolResultBlock(tool_use_id=tc.id, content="ok", is_error=False)
            results[tc.id] = blk
            if sid is not None:
                recorder.end_tool(sid, result=blk)
        return results

    monkeypatch.setattr(reg, "execute_batch", _eb)

    # 第一次 run → 崩溃
    await llm_assist.run(run_id, sync_record=sr, bgm=bgm, thinking_level="medium")
    # 第一次恢复（完成对话）
    await llm_assist.continue_run(run_id, sr, bgm)

    steps_after_first = database_manager.agent_runs.get_steps(run_id)
    chat_iters_after_first = [
        s["iteration"] for s in steps_after_first if s["name"] == "llm_chat"
    ]
    assert len(chat_iters_after_first) == len(set(chat_iters_after_first)), (
        f"第一次恢复后 chat span iteration 存在撞号：{chat_iters_after_first}"
    )

    # 模拟二次崩溃：改回 processing
    database_manager.agent_runs.update_run_status(run_id, "processing")

    # 第二次恢复（应直接 mark_no_suggestion，无额外 LLM 调用）
    await llm_assist.continue_run(run_id, sr, bgm)

    assert chat_calls["n"] == 2, (
        f"期望累计 2 次 LLM 调用（第二次恢复不应额外调 LLM），实际 {chat_calls['n']}"
    )

    steps_final = database_manager.agent_runs.get_steps(run_id)
    chat_iters_final = [s["iteration"] for s in steps_final if s["name"] == "llm_chat"]
    assert len(chat_iters_final) == len(set(chat_iters_final)), (
        f"最终 chat span iteration 存在撞号：{chat_iters_final}"
    )

    rr_final = real_replay(run_id)
    assert len(rr_final.missing_tool_calls) == 0, (
        f"二次 replay 不应再判缺失，实际 missing={rr_final.missing_tool_calls}"
    )


# S8：run() 可重试失败达上限单点置终态（无双写） -----------------------------


def test_run_retryable_llm_error_at_limit_single_terminal_write():
    """可重试 LLM 失败达上限 → 仅 increment_attempts 单点（内部置终态），不再 mark_failed。"""
    from app.services.llm.client import LLMCallError

    repo = MagicMock()
    repo.atomic_claim.return_value = True
    repo.increment_attempts.return_value = 3
    dbm = MagicMock()
    dbm.agent_runs = repo

    async def _boom(messages, *, tools=None, tool_choice=None):
        raise LLMCallError("500 boom", retryable=True)

    with (
        patch(
            "app.services.matching.llm_assist.get_database_manager", return_value=dbm
        ),
        patch("app.services.matching.llm_assist.config_manager") as cm,
    ):
        cm.get_sync_llm_match_config.return_value = _medium_cfg()
        status = asyncio.run(
            llm_assist.run(
                "run-double-write",
                sync_record=_make_sync_record(sync_record_id=520),
                bgm=MagicMock(),
                thinking_level="medium",
                chat_fn=_boom,
            )
        )

    assert status == "failed"
    repo.increment_attempts.assert_called_once_with(
        "run-double-write", last_error="500 boom"
    )
    # 终态写入收敛到 increment_attempts 单点事务，外层不得二次 mark_failed
    repo.mark_failed.assert_not_called()


# ---------------------------------------------------------------------------
# T8b：total_tokens 全轮累计（非仅末轮）
# ---------------------------------------------------------------------------


def _search_response_with_usage(tokens: int) -> ChatResponse:
    """带 usage 的搜索响应（tool_use），供多轮累计测试用。"""
    return ChatResponse(
        content="",
        stop_reason="tool_use",
        blocks=[
            ToolUseBlock(id="t1", name="search_bangumi", input={"title": "花开伊吕波"})
        ],
        usage=Usage(total_tokens=tokens),
    )


def _submit_response_with_usage(tokens: int, subject_id="123", reason="跨季匹配"):
    """带 usage 的 submit_suggestion 响应。"""
    return ChatResponse(
        content="",
        stop_reason="tool_use",
        blocks=[
            ToolUseBlock(
                id="s1",
                name="submit_suggestion",
                input={"subject_id": subject_id, "reason": reason},
            )
        ],
        usage=Usage(total_tokens=tokens),
    )


def test_trace_recorder_accumulates_total_tokens_across_rounds():
    """S2：wrap_chat_fn 逐轮累计 usage.total_tokens。"""
    import asyncio
    from unittest.mock import patch

    recorder = llm_assist.TraceRecorder("run-tok-rec", start_iteration=0)
    responses = [
        ChatResponse(content="", stop_reason="end_turn", usage=Usage(total_tokens=100)),
        ChatResponse(content="", stop_reason="end_turn", usage=Usage(total_tokens=100)),
    ]
    chat = _chat_side_effect(responses)
    wrapped = recorder.wrap_chat_fn(chat)

    with (
        patch.object(llm_assist, "trace_start_span", return_value="span-x"),
        patch.object(llm_assist, "trace_end_span"),
    ):
        asyncio.run(wrapped([Message(role="user", content="hi")], tools=[]))
        asyncio.run(wrapped([Message(role="user", content="hi")], tools=[]))

    assert recorder.total_tokens == 200, (
        f"应累计 2 轮 tokens=200，实际 {recorder.total_tokens}"
    )


@pytest.mark.asyncio
async def test_total_tokens_accumulates_across_all_rounds(monkeypatch):
    """S2：2 轮 chat 各 100 tokens → 终态 agent_runs.total_tokens == 200。"""
    run_id = "run-tokens-accum"
    sr_id = 520
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=True, sync_record_id=sr_id)

    monkeypatch.setattr(llm_assist, "_validate_subject_id", lambda sid: (True, ""))
    chat = _chat_side_effect(
        [
            _search_response_with_usage(100),
            _submit_response_with_usage(100),
        ]
    )

    status = await llm_assist.run(
        run_id,
        sync_record=sr,
        bgm=_make_bgm(),
        thinking_level="medium",
        chat_fn=chat,
        notification_service=None,
    )

    assert status == "succeeded"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["total_tokens"] == 200, (
        f"应累计全部轮次 tokens=200，实际 {run_row['total_tokens']}"
    )


@pytest.mark.asyncio
async def test_total_tokens_zero_when_no_usage():
    """S2：响应无 usage 时累计为 0（不报错、不误记）。"""
    run_id = "run-tokens-zero"
    sr_id = 521
    database_manager.agent_runs.create_pending(run_id, "match", sr_id)
    sr = _make_sync_record(with_candidates=True, sync_record_id=sr_id)

    with patch.object(llm_assist, "_validate_subject_id", return_value=(True, "")):
        chat = _chat_side_effect(
            [_search_response(), _submit_response("123", "跨季匹配")]
        )
        status = await llm_assist.run(
            run_id,
            sync_record=sr,
            bgm=_make_bgm(),
            thinking_level="medium",
            chat_fn=chat,
            notification_service=None,
        )

    assert status == "succeeded"
    run_row = database_manager.agent_runs.get_run(run_id)
    assert run_row["total_tokens"] == 0, (
        f"无 usage 应记 0，实际 {run_row['total_tokens']}"
    )


# ---------------------------------------------------------------------------
# P2-2：函数内 import 守卫（白名单为空 = 全部禁止，import 统一在模块头部）
# ---------------------------------------------------------------------------


def test_llm_assist_module_no_unlisted_function_level_imports():
    """llm_assist 模块函数体内不得残留 import（白名单为空，全部已提升到模块头部）。

    白名单形如 ``{(函数名, ast.unparse(import 语句)), ...}``；当前为空表示全禁。
    如确需豁免（如避免模块级导入环），在此登记并同时在源码处注明豁免原因。
    """
    import ast
    import inspect

    allowed: set[tuple[str, str]] = set()

    tree = ast.parse(inspect.getsource(llm_assist))
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            stmt = ast.unparse(node)
            if (fn.name, stmt) not in allowed:
                offenders.append(f"{fn.name}: {stmt}")
    assert offenders == [], (
        f"函数内不应残留未豁免 import（应提升到模块头部），实际：{offenders}"
    )

"""T11 测试：匹配场景服务 llm_assist（M5/M5b/M15/M24/M25 + 兜底 + 注入防护 + 事务）。

使用全局 database_manager（conftest 已重定向到临时 DB）验证落库与状态机；
LLM 调用、bgm、校验、通知均以 mock 注入，保证单测稳定与行为精确断言。
"""

import json

import pytest

from app.core.database import database_manager, set_database_manager
from app.services.llm.models import ChatResponse, ToolUseBlock
from app.services.llm.tools import reset_tool_registry
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
# M24：无候选全链路（LLM 搜索补充 → 建议 → 落库）
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
        chat_fn=chat,
        notification_service=ns,
        span_recorder=None,
    )

    assert status == "succeeded"
    row = _assert_candidate_written(sr_id, subject_id="789", reason="搜索补充")
    candidates = json.loads(row["candidates_json"])
    assert any(str(c.get("subject_id")) == "789" for c in candidates)


# ---------------------------------------------------------------------------
# M15：submit_suggestion subject_id 非法 → no_suggestion + last_error
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
# M25：工具执行失败（bgm.search 抛错）→ 循环继续 → 最终 no_suggestion
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
# 事务原子性：候选写入与状态更新在同一事务，中途失败整体回滚
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_llm_candidate_atomic_rollback_on_failure(monkeypatch):
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
    # 列先已存在（独立事务提交），便于验证 persist 事务回滚不影响已提交的列
    llm_assist.ensure_llm_columns(database_manager)

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
# F7：register_match_tools 幂等，重复调用不产生“重复注册”warning
# ---------------------------------------------------------------------------


def test_register_match_tools_idempotent_no_duplicate_warning(caplog):
    import logging

    from app.services.llm.tools import ToolRegistry

    registry = ToolRegistry()
    bgm = _make_bgm()

    # 连续两次注册到同一（模块单例）registry：第二次应全部跳过
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
    await llm_assist.run(run_id, sync_record=sr, bgm=_make_bgm())

    assert captured["config_override"] is None


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

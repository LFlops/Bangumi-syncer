"""匹配场景服务 llm_assist 测试（无候选全链路 + 兜底 + 注入防护 + 事务）。

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


def test_ensure_llm_columns_duplicate_logs_debug(monkeypatch):
    """重复补列（duplicate column）时记录 debug 日志，不再静默 pass。"""
    import sqlite3
    from unittest.mock import MagicMock

    log = MagicMock()
    monkeypatch.setattr(llm_assist, "logger", log)

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE pending_candidates (id INTEGER PRIMARY KEY)")
    llm_assist._ensure_llm_columns(conn)  # 首次建列：无异常
    llm_assist._ensure_llm_columns(conn)  # 第二次：命中 duplicate column

    assert log.debug.call_count == 2, "两列重复补列应各记一条 debug 日志"
    assert all("补列跳过" in str(c.args[0]) for c in log.debug.call_args_list)


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

    # get_llm_client 在 _build_default_chat_fn 内部局部导入，需 patch 源模块
    # 不 mock loop_run：让真实循环执行，验证默认 chat_fn 确实调用 client.chat
    with patch("app.services.llm.get_llm_client", return_value=mock_client):
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

    with patch("app.services.llm.get_llm_client", return_value=mock_client):
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

    with patch("app.services.llm.get_llm_client", return_value=mock_client):
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

    with patch("app.services.llm.get_llm_client", return_value=mock_client):
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

    recorder = llm_assist.TraceRecorder("run-test")

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

    recorder = llm_assist.TraceRecorder("run-test")
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

    recorder = llm_assist.TraceRecorder("run-test")
    # 仅有 chat span，无 tool span
    recorder._chat_span_id = "span-llm_chat-0"
    recorder._next_iteration = 1

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

    recorder = llm_assist.TraceRecorder("run-test")

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

    recorder = llm_assist.TraceRecorder("run-test")

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

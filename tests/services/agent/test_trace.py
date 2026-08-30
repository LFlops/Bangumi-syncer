"""T8 红阶段测试：span 记录器 + 会话增量 + 断点重放（场景 M19/M22/M22b/M23 前置）

覆盖：
1. start_span / end_span 写入 agent_steps（字段完整；独立 best-effort 事务，repo 抛错不影响主流程）
2. replay_delta 格式：llm_chat 聚合 tool_calls；tool_execute 仅 tool_result；预算消息归属
3. payload_json 结构化截断 ≤2KB 且 json.loads 合法
4. replay：2 轮记录（轮1 llm_chat+2 tool_execute；轮2 llm_chat 无工具）→ 重建 messages + last_response
5. 缺失工具识别：轮1 仅 1 个 tool_execute（另一崩溃）→ missing_tool_calls 含缺失项
6. 超限 span（status=error）→ 返回不可恢复 iteration
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.database import DatabaseManager, set_database_manager
from app.services.agent import trace


@pytest.fixture
def dbm(tmp_path: Path) -> DatabaseManager:
    """构造临时 DB 并注入全局单例；测试后复位。"""
    instance = DatabaseManager(str(tmp_path / "agent_trace.db"))
    set_database_manager(instance)
    yield instance
    instance._connection._conn.close()
    set_database_manager(None)


def _chat_replay_delta(stop_reason: str, content: str, tool_calls: list) -> dict:
    return {
        "response": {
            "stop_reason": stop_reason,
            "content": content,
            "tool_calls": tool_calls,
        }
    }


def _tool_replay_delta(tool_use_id: str, content: str, is_error: bool = False) -> dict:
    return {
        "tool_result": {
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }
    }


# ----------------------------------------------------------------------
# 1. start_span / end_span 写入字段完整 + best-effort
# ----------------------------------------------------------------------


class TestStartEndSpanWrites:
    def test_start_end_span_persists_full_fields(self, dbm):
        span_id = trace.start_span("run-1", "llm_chat", 0, 0)
        assert span_id  # 返回 uuid 字符串
        trace.end_span(
            span_id,
            status="ok",
            model="claude-3-5",
            tokens=123,
            latency_ms=456,
            input_summary="search_bangumi(title:str, subject_types:list)",
            payload_json={"type": "llm_chat", "note": "ok"},
            replay_delta=_chat_replay_delta(
                "tool_use",
                "let's search",
                [{"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}}],
            ),
        )
        steps = dbm.agent_runs.get_steps("run-1")
        assert len(steps) == 1
        s = steps[0]
        assert s["span_id"] == span_id
        assert s["name"] == "llm_chat"
        assert s["iteration"] == 0 and s["sequence"] == 0
        assert s["status"] == "ok"
        assert s["model"] == "claude-3-5"
        assert s["tokens"] == 123
        assert s["latency_ms"] == 456
        assert s["input_summary"] == "search_bangumi(title:str, subject_types:list)"
        # replay_delta 完整保存（不截断）
        import json

        rd = json.loads(s["replay_delta"])
        assert rd["response"]["tool_calls"][0]["id"] == "t1"
        # payload_json 合法 JSON
        assert json.loads(s["payload_json"])["type"] == "llm_chat"
        assert s["started_at"] and s["ended_at"]

    def test_end_span_failure_is_best_effort(self, dbm):
        """repo 抛错不应影响主流程（独立事务，仅日志）。"""
        span_id = trace.start_span("run-2", "tool_execute", 0, 1)
        # 让底层 _run_write 抛错
        dbm.agent_runs._run_write = MagicMock(side_effect=RuntimeError("db down"))
        # 不应抛异常
        trace.end_span(
            span_id,
            tool_name="search_bangumi",
            replay_delta=_tool_replay_delta("t1", "r"),
        )
        # start_span 本身抛错也应被吞掉
        dbm.agent_runs.add_step = MagicMock(side_effect=RuntimeError("db down"))
        sid = trace.start_span("run-3", "llm_chat", 1, 0)
        assert isinstance(sid, str) and sid  # 仍返回 span_id

    def test_start_span_records_iteration_sequence_parent(self, dbm):
        trace.start_span("run-4", "tool_execute", 2, 3, parent_id="parent-xyz")
        steps = dbm.agent_runs.get_steps("run-4")
        assert steps[0]["iteration"] == 2
        assert steps[0]["sequence"] == 3
        assert steps[0]["parent_id"] == "parent-xyz"
        assert steps[0]["name"] == "tool_execute"


# ----------------------------------------------------------------------
# 2. replay_delta 格式
# ----------------------------------------------------------------------


class TestReplayDeltaFormat:
    def test_llm_chat_replay_delta_aggregates_tool_calls(self, dbm):
        sid = trace.start_span("run-5", "llm_chat", 0, 0)
        tcs = [
            {"id": "a", "name": "search_bangumi", "input": {"title": "x"}},
            {"id": "b", "name": "get_subject_detail", "input": {"subject_id": "1"}},
        ]
        trace.end_span(sid, replay_delta=_chat_replay_delta("tool_use", "go", tcs))
        rd = dbm.agent_runs.get_steps("run-5")[0]["replay_delta"]
        import json

        obj = json.loads(rd)
        assert "response" in obj
        assert obj["response"]["tool_calls"] == tcs  # 聚合全部工具调用

    def test_tool_execute_replay_delta_only_tool_result(self, dbm):
        sid = trace.start_span("run-6", "tool_execute", 0, 1)
        trace.end_span(
            sid,
            tool_name="search_bangumi",
            replay_delta=_tool_replay_delta("a", "result-here"),
        )
        rd = dbm.agent_runs.get_steps("run-6")[0]["replay_delta"]
        import json

        obj = json.loads(rd)
        assert set(obj.keys()) == {"tool_result"}  # 仅 tool_result
        assert obj["tool_result"]["tool_use_id"] == "a"
        assert obj["tool_result"]["content"] == "result-here"


# ----------------------------------------------------------------------
# 3. payload_json 截断保合法
# ----------------------------------------------------------------------


class TestPayloadJsonTruncation:
    def test_truncate_json_keeps_valid_and_bounded(self):
        big = {"content": "x" * 5000, "meta": "keep"}
        out = trace.truncate_json(big)
        import json

        parsed = json.loads(out)  # 必须可解析
        assert isinstance(parsed, dict)
        assert len(out.encode("utf-8")) <= trace.MAX_PAYLOAD_JSON_BYTES

    def test_end_span_truncates_oversized_payload_json(self, dbm):
        sid = trace.start_span("run-7", "llm_chat", 0, 0)
        trace.end_span(sid, payload_json={"content": "y" * 5000})
        stored = dbm.agent_runs.get_steps("run-7")[0]["payload_json"]
        import json

        json.loads(stored)  # 合法
        assert len(stored.encode("utf-8")) <= trace.MAX_PAYLOAD_JSON_BYTES

    def test_truncate_json_small_passthrough(self):
        small = {"a": 1}
        assert trace.truncate_json(small) == '{"a": 1}'


# ----------------------------------------------------------------------
# 4. 预算消息归属
# ----------------------------------------------------------------------


class TestBudgetMessage:
    def test_record_budget_message_merges_into_span(self, dbm):
        sid = trace.start_span("run-8", "tool_execute", 0, 1)
        trace.end_span(
            sid, tool_name="search_bangumi", replay_delta=_tool_replay_delta("a", "r")
        )
        trace.record_budget_message(sid, "[剩余轮次：2]")
        rd = dbm.agent_runs.get_steps("run-8")[0]["replay_delta"]
        import json

        obj = json.loads(rd)
        assert obj["tool_result"]["tool_use_id"] == "a"
        assert obj["budget_message"] == "[剩余轮次：2]"


# ----------------------------------------------------------------------
# 5. replay 重建 2 轮
# ----------------------------------------------------------------------


class TestReplayReconstruct:
    def _seed(self):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ctx"},
        ]

    def _build_two_rounds(self, dbm):
        # 轮 1
        c1 = trace.start_span("run-r", "llm_chat", 0, 0)
        tcs1 = [
            {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}},
            {"id": "t2", "name": "get_subject_detail", "input": {"subject_id": "2"}},
        ]
        trace.end_span(
            c1, replay_delta=_chat_replay_delta("tool_use", "do tools", tcs1)
        )
        e1 = trace.start_span("run-r", "tool_execute", 0, 1)
        trace.end_span(
            e1,
            tool_name="search_bangumi",
            replay_delta=_tool_replay_delta("t1", "res1"),
        )
        e2 = trace.start_span("run-r", "tool_execute", 0, 2)
        trace.end_span(
            e2,
            tool_name="get_subject_detail",
            replay_delta=_tool_replay_delta("t2", "res2"),
        )
        trace.record_budget_message(e2, "[剩余轮次：1]")
        # 轮 2（无工具 / 终局）
        c2 = trace.start_span("run-r", "llm_chat", 1, 0)
        trace.end_span(
            c2, replay_delta=_chat_replay_delta("end_turn", "no more tools", [])
        )

    def test_replay_reconstructs_two_rounds(self, dbm):
        self._build_two_rounds(dbm)
        result = trace.replay("run-r", self._seed)
        # messages = seed + 轮1(assistant + 2 tool_result + budget)
        assert result.messages[0] == {"role": "system", "content": "sys"}
        assert result.messages[1] == {"role": "user", "content": "ctx"}
        # 轮1 聚合 assistant
        assistant = result.messages[2]
        assert assistant["role"] == "assistant"
        assert [tc["id"] for tc in assistant["tool_calls"]] == ["t1", "t2"]
        # 2 条 tool_result
        tr_msgs = [
            m
            for m in result.messages[3:]
            if m.get("role") == "user" and "tool_results" in m
        ]
        assert len(tr_msgs) == 2
        assert tr_msgs[0]["tool_results"][0]["tool_use_id"] == "t1"
        assert tr_msgs[1]["tool_results"][0]["tool_use_id"] == "t2"
        # 预算消息
        budget_msgs = [m for m in result.messages if m.get("budget_message")]
        assert budget_msgs and budget_msgs[0]["content"] == "[剩余轮次：1]"
        # 轮2 响应在 last_response，不在 messages
        assert result.last_response is not None
        assert result.last_response["stop_reason"] == "end_turn"
        assert result.last_response["content"] == "no more tools"
        assert result.executed_iterations == 1
        assert result.missing_tool_calls == []
        assert result.unrecoverable_iteration is None


# ----------------------------------------------------------------------
# 6. 缺失工具识别
# ----------------------------------------------------------------------


class TestMissingToolIdentification:
    def test_missing_tool_calls_detected(self, dbm):
        # 轮1：llm_chat 2 工具，但仅 1 个 tool_execute 记录（另一崩溃未写）
        c = trace.start_span("run-m", "llm_chat", 0, 0)
        tcs = [
            {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}},
            {"id": "t2", "name": "get_subject_detail", "input": {"subject_id": "2"}},
        ]
        trace.end_span(c, replay_delta=_chat_replay_delta("tool_use", "go", tcs))
        e = trace.start_span("run-m", "tool_execute", 0, 1)
        trace.end_span(
            e, tool_name="search_bangumi", replay_delta=_tool_replay_delta("t1", "res1")
        )
        # 故意不写 t2 的 tool_execute

        result = trace.replay("run-m", lambda: [])
        assert result.executed_iterations == 0
        missing_ids = [tc["id"] for tc in result.missing_tool_calls]
        assert missing_ids == ["t2"]
        # last_response 携带该轮响应供调用方执行缺失工具
        assert result.last_response is not None
        assert result.last_response["tool_calls"][1]["id"] == "t2"
        # messages 含 assistant + 已记录的 tool_result
        assert any(m.get("role") == "assistant" for m in result.messages)
        tr_ids = [
            m["tool_results"][0]["tool_use_id"]
            for m in result.messages
            if m.get("tool_results")
        ]
        assert tr_ids == ["t1"]


# ----------------------------------------------------------------------
# 7. 超限 span → 不可恢复 iteration
# ----------------------------------------------------------------------


class TestUnrecoverableSpan:
    def test_oversized_replay_delta_marks_error_and_unrecoverable(self, dbm):
        # 轮0 正常
        c0 = trace.start_span("run-u", "llm_chat", 0, 0)
        trace.end_span(
            c0,
            replay_delta=_chat_replay_delta(
                "tool_use", "ok", [{"id": "t1", "name": "x", "input": {}}]
            ),
        )
        e0 = trace.start_span("run-u", "tool_execute", 0, 1)
        trace.end_span(e0, replay_delta=_tool_replay_delta("t1", "r"))
        # 轮1 的 llm_chat replay_delta 超 32KB → status=error
        c1 = trace.start_span("run-u", "llm_chat", 1, 0)
        huge = "x" * (trace.MAX_REPLAY_DELTA_BYTES + 100)
        trace.end_span(c1, replay_delta=huge)

        # 存储层面：该 span status=error
        steps = dbm.agent_runs.get_steps("run-u")
        err_step = [s for s in steps if s["iteration"] == 1][0]
        assert err_step["status"] == "error"

        # 重放：返回不可恢复 iteration=1，且不重建该轮
        result = trace.replay("run-u", lambda: [{"role": "system", "content": "s"}])
        assert result.unrecoverable_iteration == 1
        # messages 只含 seed(1) + 轮0(assistant + tool_result)，不含轮1
        assert len(result.messages) == 1 + 2
        assert result.messages[0] == {"role": "system", "content": "s"}
        assert result.last_response is None

    def test_first_span_error_returns_unrecoverable_zero(self, dbm):
        c = trace.start_span("run-u0", "llm_chat", 0, 0)
        huge = "y" * (trace.MAX_REPLAY_DELTA_BYTES + 10)
        trace.end_span(c, replay_delta=huge)
        result = trace.replay("run-u0", lambda: [])
        assert result.unrecoverable_iteration == 0

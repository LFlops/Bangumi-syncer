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
from app.services.llm.models import Message, ToolResultBlock, ToolUseBlock


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
            Message(role="system", content="sys"),
            Message(role="user", content="ctx"),
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
        # 全部为 Message 实例（无 dict）
        assert all(isinstance(m, Message) for m in result.messages)
        # seed 原样保留
        assert result.messages[0] == Message(role="system", content="sys")
        assert result.messages[1] == Message(role="user", content="ctx")

        # 轮1 聚合 assistant：content 为 list[ToolUseBlock]
        assistant = result.messages[2]
        assert isinstance(assistant, Message)
        assert assistant.role == "assistant"
        assert isinstance(assistant.content, list)
        assert all(isinstance(b, ToolUseBlock) for b in assistant.content)
        assert [b.id for b in assistant.content] == ["t1", "t2"]
        assert [b.name for b in assistant.content] == [
            "search_bangumi",
            "get_subject_detail",
        ]
        # 可被 Message.model_validate 校验（content 格式合法）
        roundtrip = Message.model_validate(assistant.model_dump())
        assert roundtrip == assistant

        # tool_result 逐条 Message(role="user", content=[ToolResultBlock])，不合并
        tr_msgs = [
            m
            for m in result.messages
            if m.role == "user"
            and isinstance(m.content, list)
            and any(isinstance(b, ToolResultBlock) for b in m.content)
        ]
        assert len(tr_msgs) == 2  # 数量与工具数一致
        assert tr_msgs[0].content[0].tool_use_id == "t1"
        assert tr_msgs[0].content[0].content == "res1"
        assert tr_msgs[1].content[0].tool_use_id == "t2"
        assert tr_msgs[1].content[0].content == "res2"

        # 预算消息为 Message(role="user", content="[剩余轮次：1]")
        budget_msgs = [
            m
            for m in result.messages
            if isinstance(m, Message)
            and m.role == "user"
            and m.content == "[剩余轮次：1]"
        ]
        assert budget_msgs and budget_msgs[0].content == "[剩余轮次：1]"

        # 轮2 响应在 last_response，不在 messages
        assert result.last_response is not None
        assert result.last_response["stop_reason"] == "end_turn"
        assert result.last_response["content"] == "no more tools"
        assert result.executed_iterations == 1
        assert result.missing_tool_calls == []
        assert result.unrecoverable_iteration is None

    def test_replay_messages_byte_equivalent_to_loop_run(self, dbm):
        """replay 重建的 messages 与原执行路径（loop.run）消息列表逐字节一致。

        构造对照：按 loop.run 的构建规则手工拼出期望 Message 列表，与 replay 输出比较。
        """
        self._build_two_rounds(dbm)
        result = trace.replay("run-r", self._seed)

        expected = [
            Message(role="system", content="sys"),
            Message(role="user", content="ctx"),
            # 轮1 assistant（content=list[ToolUseBlock]）
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="t1", name="search_bangumi", input={"title": "foo"}
                    ),
                    ToolUseBlock(
                        id="t2", name="get_subject_detail", input={"subject_id": "2"}
                    ),
                ],
            ),
            # 逐条 tool_result
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="t1", content="res1", is_error=False)
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="t2", content="res2", is_error=False)
                ],
            ),
            # 预算消息
            Message(role="user", content="[剩余轮次：1]"),
        ]
        assert result.messages == expected

    def test_replay_budget_n_uses_executed_iterations(self, dbm):
        """预算消息 N 用 executed_iterations 计算（无存储 budget_message 时回退计算）。"""
        # 单轮 2 工具，未记录 budget_message，max_iterations=3 → N = 3 - 1 = 2
        c = trace.start_span("run-b", "llm_chat", 0, 0)
        tcs = [
            {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}},
            {"id": "t2", "name": "get_subject_detail", "input": {"subject_id": "2"}},
        ]
        trace.end_span(c, replay_delta=_chat_replay_delta("tool_use", "go", tcs))
        e1 = trace.start_span("run-b", "tool_execute", 0, 1)
        trace.end_span(e1, replay_delta=_tool_replay_delta("t1", "r1"))
        e2 = trace.start_span("run-b", "tool_execute", 0, 2)
        trace.end_span(e2, replay_delta=_tool_replay_delta("t2", "r2"))
        # 注意：未调用 record_budget_message

        result = trace.replay("run-b", lambda: [], max_iterations=3)
        assert result.executed_iterations == 1
        budget_msgs = [
            m
            for m in result.messages
            if m.role == "user" and isinstance(m.content, str)
        ]
        assert budget_msgs and budget_msgs[0].content == "[剩余轮次：2]"

    def test_replay_budget_n_uses_executed_iterations_not_index_h4(self, dbm):
        """H4 修正：稀疏 iteration 时，N 用已执行轮数而非 iteration 索引。

        构造两轮 {0, 5}，max_iterations=7：
        - 按 executed_iterations：第 2 轮执行后 = 2 → N = 7 - 2 = 5
        - 若误用 iteration 索引：7 - (5 + 1) = 1（错误）
        """
        # 轮 0
        c0 = trace.start_span("run-h4", "llm_chat", 0, 0)
        trace.end_span(
            c0,
            replay_delta=_chat_replay_delta(
                "tool_use", "go", [{"id": "a", "name": "x", "input": {}}]
            ),
        )
        e0 = trace.start_span("run-h4", "tool_execute", 0, 1)
        trace.end_span(e0, replay_delta=_tool_replay_delta("a", "ra"))
        # 轮 5（刻意稀疏，模拟恢复后重跑留下的非连续 iteration）
        c5 = trace.start_span("run-h4", "llm_chat", 5, 0)
        trace.end_span(
            c5,
            replay_delta=_chat_replay_delta(
                "tool_use", "go", [{"id": "b", "name": "y", "input": {}}]
            ),
        )
        e5 = trace.start_span("run-h4", "tool_execute", 5, 1)
        trace.end_span(e5, replay_delta=_tool_replay_delta("b", "rb"))

        result = trace.replay("run-h4", lambda: [], max_iterations=7)
        assert result.executed_iterations == 2
        budget_msgs = [
            m
            for m in result.messages
            if m.role == "user" and isinstance(m.content, str)
        ]
        # 两条预算消息，最后一条对应第 2 轮执行：N = 7 - 2 = 5
        assert len(budget_msgs) == 2
        assert budget_msgs[-1].content == "[剩余轮次：5]"


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
        assert all(isinstance(m, Message) for m in result.messages)
        assert result.executed_iterations == 0
        missing_ids = [tc["id"] for tc in result.missing_tool_calls]
        assert missing_ids == ["t2"]
        # last_response 携带该轮响应供调用方执行缺失工具
        assert result.last_response is not None
        assert result.last_response["tool_calls"][1]["id"] == "t2"
        # messages 含 assistant + 已记录的 tool_result（逐条 Message）
        assert any(
            isinstance(m, Message) and m.role == "assistant" for m in result.messages
        )
        tr_ids = [
            m.content[0].tool_use_id
            for m in result.messages
            if isinstance(m, Message)
            and m.role == "user"
            and isinstance(m.content, list)
            and any(isinstance(b, ToolResultBlock) for b in m.content)
        ]
        assert tr_ids == ["t1"]
        # 缺失轮次不追加预算消息
        assert not any(
            isinstance(m, Message) and m.role == "user" and isinstance(m.content, str)
            for m in result.messages
        )


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
        result = trace.replay("run-u", lambda: [Message(role="system", content="s")])
        assert result.unrecoverable_iteration == 1
        # messages 只含 seed(1) + 轮0(assistant + tool_result)，不含轮1
        assert len(result.messages) == 1 + 2
        assert all(isinstance(m, Message) for m in result.messages)
        assert result.messages[0] == Message(role="system", content="s")
        assert result.last_response is None

    def test_first_span_error_returns_unrecoverable_zero(self, dbm):
        c = trace.start_span("run-u0", "llm_chat", 0, 0)
        huge = "y" * (trace.MAX_REPLAY_DELTA_BYTES + 10)
        trace.end_span(c, replay_delta=huge)
        result = trace.replay("run-u0", lambda: [])
        assert result.unrecoverable_iteration == 0

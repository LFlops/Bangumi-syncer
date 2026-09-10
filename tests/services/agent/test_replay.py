"""replay(run_id) 断点重放测试（新签名：无 seed_builder / max_iterations）。

覆盖：
- S2.1：完整 run 重建与原执行逐字节等价（含 seed 前缀，无 seed_builder）
- S2.2：断点续跑：missing 仅缺失项；已成功不重跑；不完整轮不计 executed_iterations
- S2.3：终局响应分派；行缺失/空 delta 从该轮 break 不抛异常
- S2.4：replay_delta 加密读回透明
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.database import DatabaseManager, set_database_manager
from app.services.agent import trace
from app.services.llm.models import Message, ToolResultBlock, ToolUseBlock


@pytest.fixture
def dbm(tmp_path: Path) -> DatabaseManager:
    instance = DatabaseManager(str(tmp_path / "replay.db"))
    set_database_manager(instance)
    yield instance
    instance._connection._conn.close()
    set_database_manager(None)


@pytest.fixture
def crypto_on():
    with patch(
        "app.core.config_secret_crypto._master_secret",
        return_value="test-master-secret-key-for-encryption",
    ):
        yield


def _chat_rd(stop_reason, content, tool_calls):
    return {
        "response": {
            "stop_reason": stop_reason,
            "content": content,
            "tool_calls": tool_calls,
        }
    }


def _tool_rd(tool_use_id, content, is_error=False):
    return {
        "tool_result": {
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }
    }


def _ensure_run(dbm, run_id):
    dbm.agent_runs.create_pending(run_id, "match")


def _write_seed(dbm, run_id, seed_messages):
    """写入 seed 行（等价于 TraceRecorder.write_seed_row）。"""
    _ensure_run(dbm, run_id)
    span_id = trace.start_span(run_id, "seed", 0, 0)
    seed_delta = [m.model_dump() for m in seed_messages]
    dbm.agent_runs.update_step(
        span_id, replay_delta={"seed_messages": seed_delta}, ended_at=_now_epoch()
    )


def _now_epoch():
    import time

    return int(time.time())


def _write_llm_chat(
    dbm, run_id, iteration, tool_calls, stop_reason="tool_use", content=""
):
    """写入一轮 llm_chat span。"""
    _ensure_run(dbm, run_id)
    span_id = trace.start_span(run_id, "llm_chat", iteration, 0)
    trace.end_span(span_id, replay_delta=_chat_rd(stop_reason, content, tool_calls))
    return span_id


def _write_tool_exec(dbm, run_id, iteration, sequence, tool_use_id, content):
    """写入一条 tool_execute span。"""
    _ensure_run(dbm, run_id)
    span_id = trace.start_span(run_id, "tool_execute", iteration, sequence)
    trace.end_span(span_id, replay_delta=_tool_rd(tool_use_id, content))


class TestReplayReconstructsFullRun:
    def test_replay_with_seed_prefix(self, dbm):
        """S2.1：replay 从 seed 行还原种子消息作为 messages 前缀。"""
        seed = [
            Message(role="system", content="sys"),
            Message(role="user", content="ctx"),
        ]
        _write_seed(dbm, "run-seed", seed)
        _write_llm_chat(dbm, "run-seed", 0, [{"id": "t1", "name": "x", "input": {}}])
        _write_tool_exec(dbm, "run-seed", 0, 1, "t1", "res1")

        result = trace.replay("run-seed")
        assert len(result.messages) >= 2
        assert result.messages[0] == seed[0]
        assert result.messages[1] == seed[1]

    def test_replay_byte_equivalent_to_loop_run(self, dbm):
        """S2.1：replay 重建的 messages 与原执行路径逐字节一致。"""
        seed = [
            Message(role="system", content="sys"),
            Message(role="user", content="ctx"),
        ]
        _write_seed(dbm, "run-eq", seed)
        # 轮 0：2 工具
        _write_llm_chat(
            dbm,
            "run-eq",
            0,
            [
                {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}},
                {
                    "id": "t2",
                    "name": "get_subject_detail",
                    "input": {"subject_id": "2"},
                },
            ],
        )
        _write_tool_exec(dbm, "run-eq", 0, 1, "t1", "res1")
        _write_tool_exec(dbm, "run-eq", 0, 2, "t2", "res2")
        trace.record_budget_message(
            dbm.agent_runs.get_steps("run-eq")[-1]["span_id"], "[剩余轮次：1]"
        )
        # 轮 1：终局
        _write_llm_chat(dbm, "run-eq", 1, [], stop_reason="end_turn", content="done")

        result = trace.replay("run-eq")
        expected = [
            Message(role="system", content="sys"),
            Message(role="user", content="ctx"),
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
            Message(role="user", content="[剩余轮次：1]"),
        ]
        assert result.messages == expected
        assert result.last_response is not None
        assert result.last_response["stop_reason"] == "end_turn"
        assert result.executed_iterations == 1
        assert result.missing_tool_calls == []

    def test_replay_returns_replay_result(self, dbm):
        """replay 返回 ReplayResult（新签名，无 unrecoverable_iteration）。"""
        _write_seed(dbm, "run-rr", [])
        _write_llm_chat(dbm, "run-rr", 0, [], stop_reason="end_turn", content="hi")
        result = trace.replay("run-rr")
        assert isinstance(result, trace.ReplayResult)
        # 无 unrecoverable_iteration 字段
        assert not hasattr(result, "unrecoverable_iteration")


class TestReplayCheckpointResume:
    def test_missing_only_missing_tool(self, dbm):
        """S2.2：missing_tool_calls 仅含缺失的工具。"""
        _write_seed(dbm, "run-miss", [])
        _write_llm_chat(
            dbm,
            "run-miss",
            0,
            [
                {"id": "t1", "name": "search_bangumi", "input": {"title": "foo"}},
                {
                    "id": "t2",
                    "name": "get_subject_detail",
                    "input": {"subject_id": "2"},
                },
            ],
        )
        _write_tool_exec(dbm, "run-miss", 0, 1, "t1", "res1")
        # t2 故意不写

        result = trace.replay("run-miss")
        missing_ids = [tc["id"] for tc in result.missing_tool_calls]
        assert missing_ids == ["t2"]
        assert result.executed_iterations == 0
        # 已成功的不重跑：t1 的 tool_result 在 messages 中
        tr_ids = [
            m.content[0].tool_use_id
            for m in result.messages
            if m.role == "user"
            and isinstance(m.content, list)
            and any(isinstance(b, ToolResultBlock) for b in m.content)
        ]
        assert tr_ids == ["t1"]

    def test_incomplete_round_not_counted(self, dbm):
        """S2.2：不完整轮不计 executed_iterations、不追加预算消息。"""
        _write_seed(dbm, "run-inc", [])
        _write_llm_chat(
            dbm,
            "run-inc",
            0,
            [
                {"id": "t1", "name": "x", "input": {}},
                {"id": "t2", "name": "y", "input": {}},
            ],
        )
        _write_tool_exec(dbm, "run-inc", 0, 1, "t1", "r1")

        result = trace.replay("run-inc")
        assert result.executed_iterations == 0
        # 无预算消息（缺失轮不追加）
        budget_msgs = [
            m
            for m in result.messages
            if m.role == "user" and isinstance(m.content, str)
        ]
        assert budget_msgs == []

    def test_completed_rounds_not_replayed_as_missing(self, dbm):
        """S2.2：已完整执行的轮次不重跑，missing 仅最后一轮缺失项。"""
        _write_seed(dbm, "run-comp", [])
        # 轮 0 完整
        _write_llm_chat(dbm, "run-comp", 0, [{"id": "a", "name": "x", "input": {}}])
        _write_tool_exec(dbm, "run-comp", 0, 1, "a", "ra")
        # 轮 1 缺失
        _write_llm_chat(
            dbm,
            "run-comp",
            1,
            [
                {"id": "b", "name": "y", "input": {}},
                {"id": "c", "name": "z", "input": {}},
            ],
        )
        _write_tool_exec(dbm, "run-comp", 1, 1, "b", "rb")

        result = trace.replay("run-comp")
        assert result.executed_iterations == 1
        missing_ids = [tc["id"] for tc in result.missing_tool_calls]
        assert missing_ids == ["c"]


class TestReplayTerminalDispatch:
    def test_end_turn_returns_last_response(self, dbm):
        """S2.3：终局 end_turn 响应分派到 last_response。"""
        _write_seed(dbm, "run-end", [])
        _write_llm_chat(
            dbm, "run-end", 0, [], stop_reason="end_turn", content="no suggestion"
        )

        result = trace.replay("run-end")
        assert result.last_response is not None
        assert result.last_response["stop_reason"] == "end_turn"
        assert result.last_response["content"] == "no suggestion"
        assert result.executed_iterations == 0

    def test_break_on_empty_delta_no_exception(self, dbm):
        """S2.3：空 delta 的轮次从该轮 break，不抛异常。"""
        _write_seed(dbm, "run-empty", [])
        # 轮 0：llm_chat 的 replay_delta 为空（异常数据）
        trace.start_span("run-empty", "llm_chat", 0, 0)
        # 不写 end_span（replay_delta 为空字符串）

        # 不应抛异常
        result = trace.replay("run-empty")
        assert result.executed_iterations == 0
        assert result.last_response is None

    def test_break_on_missing_llm_chat_no_exception(self, dbm):
        """S2.3：某轮无 llm_chat（仅有 tool_execute）时 break，不抛异常。"""
        _write_seed(dbm, "run-nochat", [])
        # 轮 0 只有 tool_execute 无 llm_chat
        _write_tool_exec(dbm, "run-nochat", 0, 1, "t1", "r1")

        result = trace.replay("run-nochat")
        # 无 llm_chat → break → 不抛异常
        assert result.executed_iterations == 0


class TestReplaySortingStability:
    def test_seed_precedes_llm_chat_when_both_at_0_0(self, dbm):
        """seed 行与首轮 llm_chat 同 (0,0) 时，seed 必须排在 chat 之前。"""
        seed = [Message(role="system", content="sys")]
        _write_seed(dbm, "run-sort", seed)
        # seed 与 llm_chat 均为 iteration=0, sequence=0
        _write_llm_chat(dbm, "run-sort", 0, [{"id": "t1", "name": "x", "input": {}}])
        _write_tool_exec(dbm, "run-sort", 0, 1, "t1", "r1")
        result = trace.replay("run-sort")
        # seed 必须在前缀位置（首条消息 = system/seed）
        assert result.messages[0].role == "system"
        assert result.messages[0].content == "sys"


class TestReplayEncryptionTransparent:
    def test_replay_reads_encrypted_delta_transparently(self, dbm, crypto_on):
        """S2.4：replay_delta 加密落库，replay 读取时透明解密。"""
        seed = [Message(role="user", content="ctx")]
        _write_seed(dbm, "run-crypt", seed)
        _write_llm_chat(
            dbm,
            "run-crypt",
            0,
            [
                {
                    "id": "t1",
                    "name": "search_bangumi",
                    "input": {"title": "secret-title"},
                }
            ],
        )
        _write_tool_exec(dbm, "run-crypt", 0, 1, "t1", "secret-result")

        result = trace.replay("run-crypt")
        # 解密透明：能还原 assistant 消息
        assistant = result.messages[1]  # [0]=seed
        assert assistant.role == "assistant"
        assert assistant.content[0].input["title"] == "secret-title"
        # tool_result 也解密
        tr = [
            m
            for m in result.messages
            if m.role == "user" and isinstance(m.content, list)
        ]
        assert tr[0].content[0].content == "secret-result"

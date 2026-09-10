"""span 记录器 + replay_delta 加密 + epoch 时间测试。

覆盖：
- start_span / end_span 全字段写入 + 时间注入参数 + epoch 断言
- end_span 无 payload_json 参数（签名检查）
- 三处 replay_delta 写路径落库均为 BGS1: 密文（加密开启时）
- get_steps 解密后还原明文
- 无前缀明文容错（decrypt 对明文原样返回）
- record_budget_message 读改写后仍密文
- 无 32KB / error 标记机制
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.database import DatabaseManager, set_database_manager
from app.services.agent import trace


@pytest.fixture
def dbm(tmp_path: Path) -> DatabaseManager:
    instance = DatabaseManager(str(tmp_path / "trace_enc.db"))
    set_database_manager(instance)
    yield instance
    instance._connection._conn.close()
    set_database_manager(None)


@pytest.fixture
def crypto_on():
    """开启加密：注入测试 master secret。"""
    with patch(
        "app.core.config_secret_crypto._master_secret",
        return_value="test-master-secret-key-for-encryption",
    ):
        yield


def _chat_replay_delta(stop_reason, content, tool_calls):
    return {
        "response": {
            "stop_reason": stop_reason,
            "content": content,
            "tool_calls": tool_calls,
        }
    }


def _tool_replay_delta(tool_use_id, content, is_error=False):
    return {
        "tool_result": {
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }
    }


def _ensure_run(dbm, run_id: str):
    """创建一条 pending run（FK 要求 run 先于 steps 存在，与生产流程一致）。"""
    dbm.agent_runs.create_pending(run_id, "match")


class TestEndSpanSignature:
    def test_end_span_has_no_payload_json_param(self):
        """end_span 签名不含 payload_json 参数。"""
        sig = inspect.signature(trace.end_span)
        assert "payload_json" not in sig.parameters

    def test_end_span_has_epoch_time_params(self):
        """end_span 含可选 started_at / ended_at 整数时间注入参数。"""
        sig = inspect.signature(trace.end_span)
        assert "started_at" in sig.parameters
        assert "ended_at" in sig.parameters


class TestStartSpanEpoch:
    def test_start_span_default_epoch(self, dbm):
        """start_span 默认写入 epoch 整数 started_at。"""
        import time

        _ensure_run(dbm, "run-s")
        before = int(time.time())
        trace.start_span("run-s", "llm_chat", 0, 0)
        after = int(time.time())
        steps = dbm.agent_runs.get_steps("run-s")
        assert len(steps) == 1
        s = steps[0]
        assert isinstance(s["started_at"], int)
        assert before <= s["started_at"] <= after

    def test_start_span_inject_epoch(self, dbm):
        """start_span 接受注入的 started_at epoch。"""
        _ensure_run(dbm, "run-si")
        trace.start_span("run-si", "llm_chat", 0, 0, started_at=1_700_000_000)
        steps = dbm.agent_runs.get_steps("run-si")
        assert steps[0]["started_at"] == 1_700_000_000


class TestReplayDeltaEncryptedAtRest:
    def test_add_step_replay_delta_encrypted(self, dbm, crypto_on):
        """add_step 写入的 replay_delta 落库为 BGS1: 密文。"""
        _ensure_run(dbm, "run-e1")
        sid = trace.start_span("run-e1", "llm_chat", 0, 0)
        trace.end_span(
            sid,
            replay_delta=_chat_replay_delta(
                "tool_use", "go", [{"id": "t1", "name": "x", "input": {}}]
            ),
        )
        # 直接查库（不解密）
        conn = dbm._connection._get_connection()
        raw = conn.execute(
            "SELECT replay_delta FROM agent_steps WHERE span_id=?", (sid,)
        ).fetchone()[0]
        assert raw.startswith("BGS1:")
        # 解密后还原
        from app.core.config_secret_crypto import decrypt

        obj = json.loads(decrypt(raw))
        assert obj["response"]["tool_calls"][0]["id"] == "t1"

    def test_get_steps_decrypts_replay_delta(self, dbm, crypto_on):
        """get_steps 返回的 replay_delta 已解密为明文。"""
        _ensure_run(dbm, "run-e2")
        sid = trace.start_span("run-e2", "llm_chat", 0, 0)
        trace.end_span(
            sid,
            replay_delta=_chat_replay_delta(
                "tool_use", "go", [{"id": "t1", "name": "x", "input": {}}]
            ),
        )
        steps = dbm.agent_runs.get_steps("run-e2")
        # get_steps 解密：返回明文 JSON 字符串
        obj = json.loads(steps[0]["replay_delta"])
        assert obj["response"]["tool_calls"][0]["id"] == "t1"

    def test_update_step_replay_delta_encrypted(self, dbm, crypto_on):
        """update_step 写入的 replay_delta 落库为 BGS1: 密文。"""
        _ensure_run(dbm, "run-e3")
        sid = trace.start_span("run-e3", "tool_execute", 0, 1)
        dbm.agent_runs.update_step(
            sid,
            replay_delta=_tool_replay_delta("t1", "result"),
        )
        conn = dbm._connection._get_connection()
        raw = conn.execute(
            "SELECT replay_delta FROM agent_steps WHERE span_id=?", (sid,)
        ).fetchone()[0]
        assert raw.startswith("BGS1:")

    def test_plaintext_replay_delta_passes_through(self, dbm):
        """无前缀明文 replay_delta：decrypt 原样返回（历史数据兼容）。"""
        # 直接写一条明文 replay_delta（模拟历史数据）
        _ensure_run(dbm, "run-plain")
        sid = trace.start_span("run-plain", "llm_chat", 0, 0)
        conn = dbm._connection._get_connection()
        plain = json.dumps(_chat_replay_delta("end_turn", "hi", []))
        conn.execute(
            "UPDATE agent_steps SET replay_delta=? WHERE span_id=?",
            (plain, sid),
        )
        conn.commit()
        steps = dbm.agent_runs.get_steps("run-plain")
        # 解密容错：明文原样返回
        obj = json.loads(steps[0]["replay_delta"])
        assert obj["response"]["stop_reason"] == "end_turn"


class TestNo32KbErrorMechanism:
    def test_no_max_replay_delta_bytes_constant(self):
        """trace.py 不再定义 MAX_REPLAY_DELTA_BYTES（无 32KB 机制）。"""
        assert not hasattr(trace, "MAX_REPLAY_DELTA_BYTES")

    def test_large_replay_delta_not_marked_error(self, dbm, crypto_on):
        """超大 replay_delta 不标记 status=error（无截断/error 机制）。"""
        _ensure_run(dbm, "run-big")
        sid = trace.start_span("run-big", "llm_chat", 0, 0)
        huge = {"data": "x" * 100_000}
        trace.end_span(sid, replay_delta=huge)
        steps = dbm.agent_runs.get_steps("run-big")
        # status 仍为 ok（未被标记 error）
        assert steps[0]["status"] == "ok"
        # 且完整保存（解密后数据完整）
        obj = json.loads(steps[0]["replay_delta"])
        assert len(obj["data"]) == 100_000


class TestEndSpanDoesNotOverwriteStartedAt:
    def test_end_span_does_not_overwrite_started_at(self, dbm):
        """end_span 未传 started_at 时不覆盖 start_span 已写入的真实开始时间。"""
        _ensure_run(dbm, "run-keep-start")
        # start_span 注入一个明确的过去时间 t0
        t0 = 1_700_000_000
        sid = trace.start_span("run-keep-start", "llm_chat", 0, 0, started_at=t0)
        # 确保可区分：end_span 的 ended_at 必须与 t0 不同
        trace.end_span(sid, ended_at=t0 + 100)
        steps = dbm.agent_runs.get_steps("run-keep-start")
        assert len(steps) == 1
        s = steps[0]
        # started_at 必须保持 t0，不能被 end_span 覆盖
        assert s["started_at"] == t0
        # ended_at 正常写入
        assert s["ended_at"] == t0 + 100
        # 两者必须不同（证明 started_at 未被 ended_at 覆盖）
        assert s["started_at"] != s["ended_at"]

    def test_end_span_explicit_started_at_overwrites(self, dbm):
        """end_span 显式传入 started_at 时允许覆盖（向后兼容）。"""
        _ensure_run(dbm, "run-explicit-start")
        t0 = 1_700_000_000
        t_new = 1_700_000_500
        sid = trace.start_span("run-explicit-start", "llm_chat", 0, 0, started_at=t0)
        trace.end_span(sid, started_at=t_new, ended_at=t_new + 50)
        steps = dbm.agent_runs.get_steps("run-explicit-start")
        assert steps[0]["started_at"] == t_new
        assert steps[0]["ended_at"] == t_new + 50


class TestRecordBudgetMessage:
    def test_record_budget_message_encrypted_at_rest(self, dbm, crypto_on):
        """record_budget_message 读改写后落库仍为密文。"""
        _ensure_run(dbm, "run-bud")
        sid = trace.start_span("run-bud", "tool_execute", 0, 1)
        trace.end_span(sid, replay_delta=_tool_replay_delta("t1", "r"))
        trace.record_budget_message(sid, "[剩余轮次：2]")
        conn = dbm._connection._get_connection()
        raw = conn.execute(
            "SELECT replay_delta FROM agent_steps WHERE span_id=?", (sid,)
        ).fetchone()[0]
        assert raw.startswith("BGS1:")
        from app.core.config_secret_crypto import decrypt

        obj = json.loads(decrypt(raw))
        assert obj["budget_message"] == "[剩余轮次：2]"
        assert obj["tool_result"]["tool_use_id"] == "t1"

    def test_record_budget_message_preserves_raw_on_parse_error(self, dbm, crypto_on):
        """record_budget_message 解密/解析失败时保留原 raw，不覆盖。"""
        _ensure_run(dbm, "run-bud-err")
        sid = trace.start_span("run-bud-err", "tool_execute", 0, 1)
        trace.end_span(sid, replay_delta=_tool_replay_delta("t1", "original-result"))
        # 直接写一条损坏内容（非 BGS1: 前缀、非 JSON）模拟密钥轮换后无法解密
        conn = dbm._connection._get_connection()
        conn.execute(
            "UPDATE agent_steps SET replay_delta=? WHERE span_id=?",
            ("this-is-not-valid-json-or-ciphertext", sid),
        )
        conn.commit()
        # 调用 record_budget_message：应保留原 raw 不变
        trace.record_budget_message(sid, "[剩余轮次：2]")
        raw_after = conn.execute(
            "SELECT replay_delta FROM agent_steps WHERE span_id=?", (sid,)
        ).fetchone()[0]
        # 原值未被覆盖
        assert raw_after == "this-is-not-valid-json-or-ciphertext"

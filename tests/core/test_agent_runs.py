"""agent_runs / agent_steps 表与 repository 测试

 验证的核心行为：
1. 建表：agent_runs / agent_steps / agent_run_sync_records 表与索引存在；status 枚举不含 exhausted/applied/rejected
2. create_pending / atomic_claim（双调度器竞争）
3. mark_succeeded / mark_failed 终态（applied/rejected 已移除）
4. increment_attempts 计数与达 3 单点置 failed（携带 last_error）；refresh_started_at 支持时间戳注入
5. run↔sync_record 关联表（多对一）与 find_latest_by_sync_record 优先走关联表 + 旧指针回退
6. cleanup_terminal（先删 steps 再删 runs，无孤儿 steps）
7. get_steps 按 (iteration, sequence) 排序
8. list_pending / list_stale_processing 辅助
9. enqueue_match_run 业务键决策（created / in_flight / reuse_accepted / reuse_holding / exhausted）
10. 失败累计：每个 failed run 使 total_attempts += 1（同键 SUM 为累计失败次数）
"""

from pathlib import Path
from typing import Optional

import pytest

from app.core.database import DatabaseManager
from app.core.database.agent_runs import AgentRunsRepository

# enqueue_match_run 决策策略参数（配置类参数，必填；测试统一显式传入）
_REUSE_WINDOW_DAYS = 30
_MAX_TOTAL_ATTEMPTS = 10
_ACCEPTED_MAPPING_VALID = True


def _make_db(tmp_path: Path) -> DatabaseManager:
    """创建指向临时路径的 DatabaseManager 实例"""
    db_path = str(tmp_path / "test_agent_runs.db")
    return DatabaseManager(db_path)


def _enqueue(
    dbm: DatabaseManager,
    run_id: str,
    business_key: str,
    sync_record_id: Optional[int] = None,
    accepted_mapping_valid: bool = _ACCEPTED_MAPPING_VALID,
) -> dict:
    """测试辅助：显式传入决策策略参数调用 enqueue_match_run（无默认值兜底）。

    策略值：复用窗口 30 天、累计失败上限 10、映射校验由用例显式指定。
    """
    return dbm.agent_runs.enqueue_match_run(
        run_id=run_id,
        business_key=business_key,
        sync_record_id=sync_record_id,
        reuse_window_days=_REUSE_WINDOW_DAYS,
        max_total_attempts=_MAX_TOTAL_ATTEMPTS,
        accepted_mapping_valid=accepted_mapping_valid,
    )


def _set_status(dbm, run_id: str, status: str, ended_at: Optional[int] = None) -> None:
    """测试辅助：直接改写 run 状态（绕过业务方法，便于构造超期场景）。

    ``ended_at`` 为 epoch 秒整数（与当前 schema 一致）。
    """
    conn = dbm._connection._conn
    if ended_at is not None:
        conn.execute(
            "UPDATE agent_runs SET status=?, ended_at=? WHERE run_id=?",
            (status, ended_at, run_id),
        )
    else:
        conn.execute("UPDATE agent_runs SET status=? WHERE run_id=?", (status, run_id))
    conn.commit()


class TestAgentRunsSchema:
    """建表与索引验证（status 枚举不含 exhausted/applied/rejected）"""

    def test_tables_and_indexes_created(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('agent_runs', 'agent_steps', 'agent_run_sync_records')"
            )
            names = {r[0] for r in cur.fetchall()}
            assert names == {"agent_runs", "agent_steps", "agent_run_sync_records"}

            # status 枚举不含 exhausted（仅作 stop_reason）、不含业务特化 applied/rejected
            cur = conn.execute("SELECT sql FROM sqlite_master WHERE name='agent_runs'")
            sql = cur.fetchone()[0].lower()
            assert "exhausted" not in sql
            assert "applied" not in sql
            assert "rejected" not in sql

            # 索引存在
            cur = conn.execute("PRAGMA index_list('agent_runs')")
            idx = {r[1] for r in cur.fetchall()}
            assert "idx_agent_runs_sync_record_id" in idx
            assert "idx_agent_runs_status" in idx

            # 关联表反查索引存在
            cur = conn.execute("PRAGMA index_list('agent_run_sync_records')")
            link_idx = {r[1] for r in cur.fetchall()}
            assert "idx_agent_run_sync_records_record" in link_idx
        finally:
            dbm._connection._conn.close()

    def test_agent_run_sync_records_columns(self, tmp_path):
        """关联表列：run_id/sync_record_id/decision/created_at，主键 (run_id, sync_record_id)"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute("PRAGMA table_info('agent_run_sync_records')")
            cols = {r[1] for r in cur.fetchall()}
            assert {"run_id", "sync_record_id", "decision", "created_at"} <= cols
        finally:
            dbm._connection._conn.close()

    def test_agent_steps_columns_present(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute("PRAGMA table_info('agent_steps')")
            cols = {r[1] for r in cur.fetchall()}
            for c in (
                "run_id",
                "span_id",
                "parent_id",
                "name",
                "status",
                "model",
                "tokens",
                "latency_ms",
                "tool_name",
                "input_summary",
                "error",
                "iteration",
                "sequence",
                "replay_delta",
                "started_at",
                "ended_at",
            ):
                assert c in cols
            # payload_json 列已删除（观测摘要不再存储；截断仅存在于读取/展示侧）
            assert "payload_json" not in cols
        finally:
            dbm._connection._conn.close()


class TestCreatePendingAndClaim:
    """create_pending / atomic_claim（双调度器竞争）"""

    def test_create_pending_persists(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            row_id = dbm.agent_runs.create_pending("run-1", "match", 1)
            assert isinstance(row_id, int) and row_id > 0
            run = dbm.agent_runs.get_run("run-1")
            assert run is not None
            assert run["status"] == "pending"
            assert run["task_type"] == "match"
            assert run["sync_record_id"] == 1
        finally:
            dbm._connection._conn.close()

    def test_atomic_claim_competition(self, tmp_path):
        """先 claim 成功，再次 claim 同 run 返回 False（模拟双调度器）"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("run-compete", "match", 1)
            # 第一个调度器抢占成功
            assert dbm.agent_runs.atomic_claim("run-compete") is True
            assert dbm.agent_runs.get_run("run-compete")["status"] == "processing"
            # 第二个调度器重复抢占失败
            assert dbm.agent_runs.atomic_claim("run-compete") is False

            # 另一个 run 不受干扰
            dbm.agent_runs.create_pending("run-other", "match", 2)
            assert dbm.agent_runs.atomic_claim("run-other") is True
            assert dbm.agent_runs.atomic_claim("run-other") is False
        finally:
            dbm._connection._conn.close()


class TestStatusTransitions:
    """mark_succeeded / mark_failed 终态（applied/rejected 已移除）"""

    def test_applied_rejected_methods_removed(self):
        """业务特化终态方法与查询已删除（用户处理结果由 pending_candidates 承载）"""
        assert not hasattr(AgentRunsRepository, "mark_applied")
        assert not hasattr(AgentRunsRepository, "mark_rejected")
        assert not hasattr(AgentRunsRepository, "find_active_by_sync_record")

    def test_mark_failed_records_terminal_and_accumulates(self, tmp_path):
        """mark_failed → failed 终态，且 total_attempts 累计 +1"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rf", "match", 1)
            assert dbm.agent_runs.mark_failed("rf", "failed", "boom", 42) is True
            run = dbm.agent_runs.get_run("rf")
            assert run["status"] == "failed"
            assert run["stop_reason"] == "failed"
            assert run["last_error"] == "boom"
            assert run["total_tokens"] == 42
            assert run["ended_at"]  # 终态时间已记录
            assert run["total_attempts"] == 1
        finally:
            dbm._connection._conn.close()

    def test_mark_failed_idempotent_when_already_failed(self, tmp_path):
        """已 failed 的 run 再次 mark_failed 被状态守卫拒绝（返回 False，不重复累计）"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rf2", "match", 1)
            assert dbm.agent_runs.mark_failed("rf2", "failed", "boom") is True
            assert dbm.agent_runs.get_run("rf2")["total_attempts"] == 1
            # 重复调用（如 llm_assist 重试耗尽后又 mark_failed）→ 守卫拒绝
            assert dbm.agent_runs.mark_failed("rf2", "failed", "boom-again") is False
            assert dbm.agent_runs.get_run("rf2")["total_attempts"] == 1
        finally:
            dbm._connection._conn.close()

    @pytest.mark.parametrize("terminal", ["succeeded", "no_suggestion", "cancelled"])
    def test_mark_failed_guard_rejects_terminal_states(self, tmp_path, terminal):
        """状态守卫：非 pending/processing 终态不被 mark_failed 改写（返回 False）"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("guard", "match", 1)
            dbm.agent_runs.atomic_claim("guard")
            if terminal == "succeeded":
                dbm.agent_runs.mark_succeeded("guard")
            elif terminal == "no_suggestion":
                dbm.agent_runs.mark_no_suggestion("guard")
            else:
                dbm.agent_runs.update_run_status("guard", "cancelled")

            assert dbm.agent_runs.mark_failed("guard", "failed", "boom") is False
            run = dbm.agent_runs.get_run("guard")
            assert run["status"] == terminal
            assert run["total_attempts"] == 0
        finally:
            dbm._connection._conn.close()

    def test_mark_cancelled_from_processing_sets_terminal_and_ended_at(self, tmp_path):
        """processing → cancelled 成功，写 stop_reason 与 ended_at（epoch 秒）。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rc", "match", 1)
            assert dbm.agent_runs.atomic_claim("rc") is True  # → processing

            assert (
                dbm.agent_runs.mark_cancelled("rc", stop_reason="user_resolved") is True
            )
            run = dbm.agent_runs.get_run("rc")
            assert run["status"] == "cancelled"
            assert run["stop_reason"] == "user_resolved"
            assert run["ended_at"] > 0
        finally:
            dbm._connection._conn.close()

    def test_mark_cancelled_guard_rejects_terminal_states(self, tmp_path):
        """状态守卫：succeeded 等非活性态不被 mark_cancelled 改写（返回 False）。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rc2", "match", 1)
            assert dbm.agent_runs.atomic_claim("rc2") is True
            assert dbm.agent_runs.mark_succeeded("rc2") is True

            assert (
                dbm.agent_runs.mark_cancelled("rc2", stop_reason="user_resolved")
                is False
            )
            run = dbm.agent_runs.get_run("rc2")
            assert run["status"] == "succeeded"
            assert run["stop_reason"] == ""
        finally:
            dbm._connection._conn.close()

    def test_same_key_two_failed_runs_sum_is_two(self, tmp_path):
        """同键两条 failed run → SUM(total_attempts)=2（累计失败次数）"""
        dbm = _make_db(tmp_path)
        try:
            bk = "match|alice|test|1"
            dbm.agent_runs.create_pending("f1", "match", 1, business_key=bk)
            dbm.agent_runs.mark_failed("f1", "failed", "e1")
            dbm.agent_runs.create_pending("f2", "match", 2, business_key=bk)
            dbm.agent_runs.mark_failed("f2", "failed", "e2")

            conn = dbm._connection._conn
            total = conn.execute(
                "SELECT SUM(total_attempts) FROM agent_runs "
                "WHERE business_key=? AND status='failed'",
                (bk,),
            ).fetchone()[0]
            assert total == 2
        finally:
            dbm._connection._conn.close()


class TestIncrementAttempts:
    """increment_attempts：计数与达上限置终态单点完成"""

    def test_increment_attempts_reaches_limit_sets_terminal_with_last_error(
        self, tmp_path
    ):
        """达上限（3）时同一次调用落 failed/stop_reason/last_error/ended_at。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("r3", "match", 1)
            dbm.agent_runs.atomic_claim("r3")  # → processing
            assert dbm.agent_runs.increment_attempts("r3") == 1
            assert dbm.agent_runs.increment_attempts("r3") == 2
            assert dbm.agent_runs.get_run("r3")["status"] == "processing"

            # 第二次已达上限，单点调用直接置终态并携带 last_error
            a3 = dbm.agent_runs.increment_attempts("r3", last_error="boom")
            assert a3 == 3
            run = dbm.agent_runs.get_run("r3")
            assert run["attempts"] == 3
            assert run["status"] == "failed"
            assert run["stop_reason"] == "failed"
            assert run["last_error"] == "boom"
            assert run["ended_at"] > 0
            # 达上限置终态同一次调用累计 total_attempts
            assert run["total_attempts"] == 1
        finally:
            dbm._connection._conn.close()

    def test_increment_attempts_below_limit_only_counts(self, tmp_path):
        """未达上限仅计数：attempts=1 → 返回 2，状态仍 processing 且不覆盖 last_error。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("r2", "match", 1)
            dbm.agent_runs.atomic_claim("r2")  # → processing
            assert dbm.agent_runs.increment_attempts("r2") == 1
            # 预置一个哨兵值，验证未达上限时不写 last_error
            conn = dbm._connection._conn
            conn.execute(
                "UPDATE agent_runs SET last_error='prev' WHERE run_id=?", ("r2",)
            )
            conn.commit()

            assert dbm.agent_runs.increment_attempts("r2", last_error="temp") == 2
            run = dbm.agent_runs.get_run("r2")
            assert run["attempts"] == 2
            assert run["status"] == "processing"
            # 未达上限不落终态、不覆盖 last_error、不累计 total_attempts
            assert run["ended_at"] == 0
            assert run["last_error"] == "prev"
            assert run["total_attempts"] == 0
        finally:
            dbm._connection._conn.close()

    def test_increment_attempts_non_processing_returns_zero_and_unchanged(
        self, tmp_path
    ):
        """非 processing 态不计数：返回 0，状态 / attempts / last_error 均不变。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rs", "match", 1)
            dbm.agent_runs.atomic_claim("rs")
            dbm.agent_runs.mark_succeeded("rs")  # → succeeded
            conn = dbm._connection._conn
            conn.execute(
                "UPDATE agent_runs SET last_error='prev' WHERE run_id=?", ("rs",)
            )
            conn.commit()

            assert dbm.agent_runs.increment_attempts("rs", last_error="boom") == 0
            run = dbm.agent_runs.get_run("rs")
            assert run["status"] == "succeeded"
            assert run["attempts"] == 0
            assert run["last_error"] == "prev"
        finally:
            dbm._connection._conn.close()


class TestRefreshStartedAt:
    """refresh_started_at：支持调用方注入时间戳（重入防护统一时间戳）"""

    def test_refresh_started_at_uses_caller_timestamp(self, tmp_path):
        """调用方传入 ts=1000 → started_at=1000（不得取当前时间）。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rf-ts", "match", 1)
            dbm.agent_runs.atomic_claim("rf-ts")  # → processing

            assert dbm.agent_runs.refresh_started_at("rf-ts", 1000) is True
            assert dbm.agent_runs.get_run("rf-ts")["started_at"] == 1000
        finally:
            dbm._connection._conn.close()

    def test_refresh_started_at_none_falls_back_to_now(self, tmp_path):
        """ts=None → 取当前 epoch 秒。"""
        import time

        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rf-now", "match", 1)
            dbm.agent_runs.atomic_claim("rf-now")

            before = int(time.time())
            assert dbm.agent_runs.refresh_started_at("rf-now") is True
            after = int(time.time())
            started_at = dbm.agent_runs.get_run("rf-now")["started_at"]
            assert before <= started_at <= after
        finally:
            dbm._connection._conn.close()

    @pytest.mark.parametrize("bad_ts", [0, -100])
    def test_refresh_started_at_non_positive_ts_falls_back_to_now(
        self, tmp_path, bad_ts
    ):
        """ts<=0 → 取当前时间（保证 started_at>0，可被 list_stale_processing 拾取）"""
        import time

        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rf-bad", "match", 1)
            dbm.agent_runs.atomic_claim("rf-bad")

            before = int(time.time())
            assert dbm.agent_runs.refresh_started_at("rf-bad", bad_ts) is True
            after = int(time.time())
            started_at = dbm.agent_runs.get_run("rf-bad")["started_at"]
            assert started_at > 0
            assert before <= started_at <= after
        finally:
            dbm._connection._conn.close()

    def test_refresh_started_at_non_processing_returns_false(self, tmp_path):
        """非 processing 态不刷新：返回 False，started_at 不变。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rf-term", "match", 1)
            assert dbm.agent_runs.refresh_started_at("rf-term", 1000) is False
            assert dbm.agent_runs.get_run("rf-term")["started_at"] == 0
        finally:
            dbm._connection._conn.close()

    def test_refresh_started_at_cas_matching_expected_updates_and_returns_true(
        self, tmp_path
    ):
        """S1：expected_started_at 与当前值一致 → CAS 成功，started_at 被刷新。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("cas-ok", "match", 1)
            dbm.agent_runs.atomic_claim("cas-ok")  # → processing
            current = dbm.agent_runs.get_run("cas-ok")["started_at"]
            assert current > 0

            assert (
                dbm.agent_runs.refresh_started_at(
                    "cas-ok", 2000, expected_started_at=current
                )
                is True
            )
            assert dbm.agent_runs.get_run("cas-ok")["started_at"] == 2000
        finally:
            dbm._connection._conn.close()

    def test_refresh_started_at_cas_mismatch_returns_false_and_keeps_value(
        self, tmp_path
    ):
        """S2：expected_started_at 不匹配（已被其他执行者刷新）→ False，原值不变。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("cas-bad", "match", 1)
            dbm.agent_runs.atomic_claim("cas-bad")
            current = dbm.agent_runs.get_run("cas-bad")["started_at"]

            assert (
                dbm.agent_runs.refresh_started_at(
                    "cas-bad", 2000, expected_started_at=current + 1
                )
                is False
            )
            assert dbm.agent_runs.get_run("cas-bad")["started_at"] == current
        finally:
            dbm._connection._conn.close()

    def test_refresh_started_at_cas_non_processing_returns_false(self, tmp_path):
        """S2：CAS 同时要求 status='processing'，非活性态不生效。"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("cas-p", "match", 1)  # 仍为 pending
            assert (
                dbm.agent_runs.refresh_started_at("cas-p", 2000, expected_started_at=0)
                is False
            )
            assert dbm.agent_runs.get_run("cas-p")["started_at"] == 0
        finally:
            dbm._connection._conn.close()


class TestRunSyncRecordLinks:
    """run ↔ sync_record 关联表（多对一）与 find_latest_by_sync_record"""

    def test_add_link_multi_to_one_and_find_by_each_record(self, tmp_path):
        """3 条集级失败 record 关联同一剧集级 run；按任意 record 均命中该 run"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("run-1", "match", 900, business_key="bk")
            for sr_id in (101, 102, 103):
                assert (
                    dbm.agent_runs.add_run_sync_record_link(
                        "run-1", sr_id, decision="created"
                    )
                    == 1
                )

            for sr_id in (101, 102, 103):
                run = dbm.agent_runs.find_latest_by_sync_record(sr_id)
                assert run is not None
                assert run["run_id"] == "run-1"

            # 关联行共 3 条（多对一）
            conn = dbm._connection._conn
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_run_sync_records WHERE run_id='run-1'"
            ).fetchone()[0]
            assert count == 3
        finally:
            dbm._connection._conn.close()

    def test_add_link_insert_or_ignore_returns_zero_on_duplicate(self, tmp_path):
        """重复关联同一 (run, record) 幂等：第二次返回 0，不新增行"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("run-1", "match", 900)
            assert dbm.agent_runs.add_run_sync_record_link("run-1", 101) == 1
            assert dbm.agent_runs.add_run_sync_record_link("run-1", 101) == 0
        finally:
            dbm._connection._conn.close()

    def test_find_latest_prefers_link_table_over_legacy_pointer(self, tmp_path):
        """有关联行时优先返回关联表指向的 run（而非旧 sync_record_id 主指针）"""
        dbm = _make_db(tmp_path)
        try:
            # 旧主指针指向 legacy-run
            dbm.agent_runs.create_pending("legacy-run", "match", 500)
            # 关联表指向 newer-run（同时 newn-run 主指针为空）
            dbm.agent_runs.create_pending("newer-run", "match", None)
            dbm.agent_runs.add_run_sync_record_link("newer-run", 500)

            run = dbm.agent_runs.find_latest_by_sync_record(500)
            assert run["run_id"] == "newer-run"
        finally:
            dbm._connection._conn.close()

    def test_find_latest_falls_back_to_legacy_pointer_when_no_link(self, tmp_path):
        """无关联行时回退旧路径（按 agent_runs.sync_record_id 主指针）"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("legacy-run", "match", 600)
            run = dbm.agent_runs.find_latest_by_sync_record(600)
            assert run is not None
            assert run["run_id"] == "legacy-run"
        finally:
            dbm._connection._conn.close()

    def test_find_latest_returns_none_when_no_match(self, tmp_path):
        """既无关联行也无旧指针命中 → None"""
        dbm = _make_db(tmp_path)
        try:
            assert dbm.agent_runs.find_latest_by_sync_record(999) is None
        finally:
            dbm._connection._conn.close()

    def test_update_run_sync_record_id_refreshes_pointer(self, tmp_path):
        """update_run_sync_record_id 刷新调度主指针，返回 True；未知 run 返回 False"""
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("run-1", "match", 1)
            assert dbm.agent_runs.update_run_sync_record_id("run-1", 700) is True
            assert dbm.agent_runs.get_run("run-1")["sync_record_id"] == 700
            assert dbm.agent_runs.update_run_sync_record_id("missing", 1) is False
        finally:
            dbm._connection._conn.close()


class TestCleanupExpired:
    """按滑动窗口轮转清理过期 runs（单条 DELETE，FK 级联删 steps）。

    两腿 OR：
    - 终态腿：status IN (terminal) AND ended_at > 0 AND ended_at < cutoff
    - 活性腿：status IN (pending, processing) AND created_at < cutoff
    """

    def test_cleanup_expired_deletes_terminal_over_window_with_cascade(self, tmp_path):
        """终态超窗 run 被删且 agent_steps 级联消失（FK 取代两步删除）。"""
        dbm = _make_db(tmp_path)
        try:
            # run A：succeeded + 超期 + 2 条 steps
            dbm.agent_runs.create_pending("cleanup-a", "match", 1)
            dbm.agent_runs.atomic_claim("cleanup-a")
            dbm.agent_runs.mark_succeeded("cleanup-a")
            dbm.agent_runs.add_step(
                {
                    "run_id": "cleanup-a",
                    "span_id": "s1",
                    "name": "llm_chat",
                    "iteration": 0,
                    "sequence": 0,
                }
            )
            dbm.agent_runs.add_step(
                {
                    "run_id": "cleanup-a",
                    "span_id": "s2",
                    "name": "tool_execute",
                    "iteration": 0,
                    "sequence": 1,
                }
            )
            conn = dbm._connection._conn
            # 把 ended_at 改到远早于保留期（epoch 秒整数，2000-01-01）
            conn.execute(
                "UPDATE agent_runs SET ended_at=? WHERE run_id=?",
                (946684800, "cleanup-a"),
            )
            conn.commit()

            # run B：no_suggestion 未超期 + 1 条 step（不应被删）
            dbm.agent_runs.create_pending("cleanup-b", "match", 2)
            dbm.agent_runs.mark_no_suggestion("cleanup-b")
            dbm.agent_runs.add_step(
                {
                    "run_id": "cleanup-b",
                    "span_id": "s3",
                    "name": "llm_chat",
                    "iteration": 0,
                    "sequence": 0,
                }
            )

            deleted = dbm.agent_runs.cleanup_expired(7)
            assert deleted == 1

            # A 已删，B 仍在
            assert dbm.agent_runs.get_run("cleanup-a") is None
            assert dbm.agent_runs.get_run("cleanup-b") is not None

            # 无孤儿 steps：A 的 steps 已随 runs 级联删除
            assert dbm.agent_runs.get_steps("cleanup-a") == []
            # B 的 steps 保留
            assert len(dbm.agent_runs.get_steps("cleanup-b")) == 1
        finally:
            dbm._connection._conn.close()

    def test_cleanup_expired_deletes_over_window_pending_and_processing(self, tmp_path):
        """pending/processing 超窗被删（含 steps 级联）。"""
        dbm = _make_db(tmp_path)
        try:
            # run P：pending 超窗 + 1 step
            dbm.agent_runs.create_pending("old-pending", "match", 10)
            dbm.agent_runs.add_step(
                {
                    "run_id": "old-pending",
                    "span_id": "sp1",
                    "name": "llm_chat",
                    "iteration": 0,
                    "sequence": 0,
                }
            )
            # run C：processing 超窗 + 1 step
            dbm.agent_runs.create_pending("old-processing", "match", 11)
            dbm.agent_runs.atomic_claim("old-processing")
            dbm.agent_runs.add_step(
                {
                    "run_id": "old-processing",
                    "span_id": "sp2",
                    "name": "tool_execute",
                    "iteration": 0,
                    "sequence": 0,
                }
            )
            # run F：fresh pending（不应被删）
            dbm.agent_runs.create_pending("fresh-pending", "match", 12)

            conn = dbm._connection._conn
            # 把活性超窗 run 的 created_at 改到远早于保留期
            conn.execute(
                "UPDATE agent_runs SET created_at=? WHERE run_id IN (?, ?)",
                (946684800, "old-pending", "old-processing"),
            )
            conn.commit()

            deleted = dbm.agent_runs.cleanup_expired(7)
            assert deleted == 2

            # 超窗的 pending/processing 已删
            assert dbm.agent_runs.get_run("old-pending") is None
            assert dbm.agent_runs.get_run("old-processing") is None
            assert dbm.agent_runs.get_steps("old-pending") == []
            assert dbm.agent_runs.get_steps("old-processing") == []
            # fresh pending 保留
            assert dbm.agent_runs.get_run("fresh-pending") is not None
        finally:
            dbm._connection._conn.close()

    def test_cleanup_expired_keeps_in_window_any_status(self, tmp_path):
        """窗内（<30 天）任何状态不删；窗内 processing 不删（防回归）。"""
        import time

        dbm = _make_db(tmp_path)
        try:
            now = int(time.time())

            # 各终态均在窗内（ended_at = now，未超期）
            for rid, term_fn in [
                ("succeeded", dbm.agent_runs.mark_succeeded),
                ("failed", lambda r: dbm.agent_runs.mark_failed(r, "failed", "e", 0)),
                ("no_suggestion", dbm.agent_runs.mark_no_suggestion),
            ]:
                dbm.agent_runs.create_pending(rid, "match", 1)
                dbm.agent_runs.atomic_claim(rid)
                term_fn(rid)

            # cancelled 没有语义方法，直接改 status + ended_at
            dbm.agent_runs.create_pending("cancelled", "match", 1)
            _set_status(dbm, "cancelled", "cancelled", ended_at=now)

            # processing 在窗内（刚 claim，started_at = now，未超 30 天）
            dbm.agent_runs.create_pending("proc-in-window", "match", 1)
            dbm.agent_runs.atomic_claim("proc-in-window")

            assert dbm.agent_runs.cleanup_expired(30) == 0
            # 全部保留
            assert dbm.agent_runs.get_run("succeeded") is not None
            assert dbm.agent_runs.get_run("failed") is not None
            assert dbm.agent_runs.get_run("no_suggestion") is not None
            assert dbm.agent_runs.get_run("cancelled") is not None
            assert dbm.agent_runs.get_run("proc-in-window") is not None
        finally:
            dbm._connection._conn.close()

    def test_cleanup_expired_zero_or_negative_returns_0(self, tmp_path):
        """retention_days=0/负数不删、返回 0（永不清理语义）。"""
        dbm = _make_db(tmp_path)
        try:
            # 构造一条超期终态 run
            dbm.agent_runs.create_pending("zero-test", "match", 1)
            dbm.agent_runs.atomic_claim("zero-test")
            dbm.agent_runs.mark_succeeded("zero-test")
            conn = dbm._connection._conn
            conn.execute(
                "UPDATE agent_runs SET ended_at=? WHERE run_id=?",
                (946684800, "zero-test"),
            )
            conn.commit()

            assert dbm.agent_runs.cleanup_expired(0) == 0
            assert dbm.agent_runs.cleanup_expired(-5) == 0
            # 记录仍在
            assert dbm.agent_runs.get_run("zero-test") is not None
        finally:
            dbm._connection._conn.close()


class TestStepsOrdering:
    """get_steps 按 (iteration, sequence, id) 排序"""

    def test_steps_sorted_by_iteration_sequence(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("sort", "match", 1)
            dbm.agent_runs.add_step(
                {
                    "run_id": "sort",
                    "span_id": "x",
                    "name": "n",
                    "iteration": 1,
                    "sequence": 5,
                }
            )
            dbm.agent_runs.add_step(
                {
                    "run_id": "sort",
                    "span_id": "y",
                    "name": "n",
                    "iteration": 0,
                    "sequence": 2,
                }
            )
            dbm.agent_runs.add_step(
                {
                    "run_id": "sort",
                    "span_id": "z",
                    "name": "n",
                    "iteration": 0,
                    "sequence": 1,
                }
            )
            dbm.agent_runs.add_step(
                {
                    "run_id": "sort",
                    "span_id": "w",
                    "name": "n",
                    "iteration": 1,
                    "sequence": 0,
                }
            )
            steps = dbm.agent_runs.get_steps("sort")
            order = [(s["iteration"], s["sequence"]) for s in steps]
            assert order == sorted(order)
            # 首项应为 (0,1) 那条
            assert steps[0]["span_id"] == "z"
            assert steps[-1]["span_id"] == "x"
        finally:
            dbm._connection._conn.close()

    def test_steps_tie_break_by_id(self, tmp_path):
        """get_steps SQL 末级按 id 排序（同 (iteration, sequence) 稳定，replay 承诺）"""
        dbm = _make_db(tmp_path)

        class _RecordingConn:
            """记录 execute 语句的代理连接（SQLite 同键并列的顺序不可外部观测，故断言 SQL 契约）。"""

            def __init__(self, real):
                self._real = real
                self.statements: list[str] = []

            def execute(self, sql, *args, **kwargs):
                self.statements.append(sql)
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            def cursor(self):
                return self._real.cursor()

            def close(self):
                return self._real.close()

        try:
            dbm.agent_runs.create_pending("sort-tie", "match", 1)
            for span_id in ("t1", "t2", "t3"):
                dbm.agent_runs.add_step(
                    {
                        "run_id": "sort-tie",
                        "span_id": span_id,
                        "name": "n",
                        "iteration": 0,
                        "sequence": 0,
                    }
                )
            recorder = _RecordingConn(dbm._connection._conn)
            dbm._connection._conn = recorder

            steps = dbm.agent_runs.get_steps("sort-tie")
            assert [s["span_id"] for s in steps] == ["t1", "t2", "t3"]
            stmt = next(s for s in recorder.statements if "FROM agent_steps" in s)
            normalized = " ".join(stmt.split())
            assert normalized.endswith("ORDER BY iteration ASC, sequence ASC, id ASC")
        finally:
            dbm._connection._conn.close()


class TestSchedulerHelpers:
    """list_pending / list_stale_processing 供调度器使用"""

    def test_list_pending_returns_pending_runs(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("lp1", "match", 1)
            dbm.agent_runs.create_pending("lp2", "match", 2)
            pending = dbm.agent_runs.list_pending(10)
            assert len(pending) == 2
            assert {r["run_id"] for r in pending} == {"lp1", "lp2"}
        finally:
            dbm._connection._conn.close()

    def test_list_stale_processing(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("stale", "match", 3)
            dbm.agent_runs.atomic_claim("stale")  # started_at = now
            conn = dbm._connection._conn
            # 把 started_at 改到远早于超时阈值（epoch 秒整数，2000-01-01）
            conn.execute(
                "UPDATE agent_runs SET started_at=? WHERE run_id=?",
                (946684800, "stale"),
            )
            conn.commit()

            stale = dbm.agent_runs.list_stale_processing(120)
            assert any(r["run_id"] == "stale" for r in stale)

            # 刚 claim 的 run 不应出现在超期列表
            dbm.agent_runs.create_pending("fresh", "match", 4)
            dbm.agent_runs.atomic_claim("fresh")
            stale2 = dbm.agent_runs.list_stale_processing(120)
            assert not any(r["run_id"] == "fresh" for r in stale2)
        finally:
            dbm._connection._conn.close()


# ---------------------------------------------------------------------------
# enqueue_match_run 业务键决策（created / in_flight / reuse_accepted /
# reuse_holding / exhausted；失败累计按 SUM(total_attempts)）
# ---------------------------------------------------------------------------


def _local_days_ago(days: int) -> str:
    """测试辅助：返回 N 天前的本地时间字符串（与 resolved_at 存储格式一致）"""
    from datetime import datetime, timedelta

    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _add_pending_candidate(dbm, business_key: str) -> int:
    """测试辅助：沉淀一条 pending 候选行，返回 id"""
    candidate_id = dbm.log_pending_candidate(
        request_title="测试番剧",
        request_season=1,
        user_name="alice",
        source="plex",
        candidates=[{"subject_id": "111", "name": "测试番剧", "score": 0.9}],
        business_key=business_key,
    )
    assert candidate_id
    return candidate_id


def _set_candidate_status(
    dbm, candidate_id: int, status: str, resolved_at=None
) -> None:
    """测试辅助：直接改写候选状态与 resolved_at（构造窗口/已处理场景）"""
    conn = dbm._connection._conn
    conn.execute(
        "UPDATE pending_candidates SET status=?, resolved_at=? WHERE id=?",
        (status, resolved_at, candidate_id),
    )
    conn.commit()


def _seed_succeeded_run(
    dbm, run_id: str, business_key: str, sync_record_id: int
) -> None:
    """测试辅助：构造一条 succeeded 终态 run"""
    _enqueue(
        dbm, run_id=run_id, business_key=business_key, sync_record_id=sync_record_id
    )
    dbm.agent_runs.atomic_claim(run_id)
    dbm.agent_runs.mark_succeeded(run_id)


class TestEnqueueMatchRun:
    """enqueue_match_run：单事务内决策，返回 {"decision", "run_id"}"""

    BK = "match|alice|test|1"

    def test_no_history_creates_new(self, tmp_path):
        """S1 无历史 → created，返回传入 run_id，库中新增 pending run"""
        dbm = _make_db(tmp_path)
        try:
            res = _enqueue(
                dbm, run_id="new-run", business_key=self.BK, sync_record_id=100
            )
            assert res == {"decision": "created", "run_id": "new-run"}
            run = dbm.agent_runs.get_run("new-run")
            assert run is not None
            assert run["status"] == "pending"
            assert run["business_key"] == self.BK
            assert run["sync_record_id"] == 100
            assert run["total_attempts"] == 0
        finally:
            dbm._connection._conn.close()

    def test_processing_in_flight_returns_existing_and_refreshes(self, tmp_path):
        """S2 同键在途 processing → in_flight，返回已有 run_id，不新建并刷新主指针"""
        dbm = _make_db(tmp_path)
        try:
            _enqueue(dbm, run_id="run-b", business_key=self.BK, sync_record_id=100)
            dbm.agent_runs.atomic_claim("run-b")

            res = _enqueue(
                dbm, run_id="run-c", business_key=self.BK, sync_record_id=200
            )
            assert res == {"decision": "in_flight", "run_id": "run-b"}
            assert dbm.agent_runs.get_run("run-c") is None
            run = dbm.agent_runs.get_run("run-b")
            assert run["status"] == "processing"
            assert run["sync_record_id"] == 200
        finally:
            dbm._connection._conn.close()

    def test_pending_in_flight_returns_existing(self, tmp_path):
        """S2 同键 pending → in_flight，返回已有 run_id，不新建"""
        dbm = _make_db(tmp_path)
        try:
            _enqueue(dbm, run_id="run-a", business_key=self.BK, sync_record_id=100)
            res = _enqueue(
                dbm, run_id="run-a2", business_key=self.BK, sync_record_id=200
            )
            assert res == {"decision": "in_flight", "run_id": "run-a"}
            assert dbm.agent_runs.get_run("run-a2") is None
            conn = dbm._connection._conn
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE business_key=?", (self.BK,)
            ).fetchone()[0]
            assert count == 1
        finally:
            dbm._connection._conn.close()

    def test_failed_below_limit_creates_new_run(self, tmp_path):
        """S3 同键 failed 累计 2（<10）→ created，返回新 run_id，不复用旧行"""
        dbm = _make_db(tmp_path)
        try:
            for rid in ("f1", "f2"):
                _enqueue(dbm, run_id=rid, business_key=self.BK, sync_record_id=1)
                dbm.agent_runs.mark_failed(rid, "failed", "e")

            res = _enqueue(dbm, run_id="f3", business_key=self.BK, sync_record_id=300)
            assert res == {"decision": "created", "run_id": "f3"}
            new_run = dbm.agent_runs.get_run("f3")
            assert new_run["status"] == "pending"
            assert new_run["sync_record_id"] == 300
            # 旧 failed 行不被复用/改写
            assert dbm.agent_runs.get_run("f1")["status"] == "failed"
            assert dbm.agent_runs.get_run("f2")["status"] == "failed"
            conn = dbm._connection._conn
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE business_key=?", (self.BK,)
            ).fetchone()[0]
            assert count == 3
        finally:
            dbm._connection._conn.close()

    def test_failed_at_limit_exhausted_no_new_run(self, tmp_path):
        """S4 同键 failed 累计达上限（10）→ exhausted，不写库"""
        dbm = _make_db(tmp_path)
        try:
            for i in range(10):
                rid = f"f{i}"
                _enqueue(dbm, run_id=rid, business_key=self.BK, sync_record_id=i)
                dbm.agent_runs.mark_failed(rid, "failed", "e")

            res = _enqueue(
                dbm, run_id="f-new", business_key=self.BK, sync_record_id=999
            )
            assert res == {"decision": "exhausted", "run_id": "f9"}
            assert dbm.agent_runs.get_run("f-new") is None
            conn = dbm._connection._conn
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE business_key=?", (self.BK,)
            ).fetchone()[0]
            assert count == 10
        finally:
            dbm._connection._conn.close()

    def test_succeeded_with_pending_candidate_reuses_holding_forever(self, tmp_path):
        """S5 succeeded + 候选 pending → reuse_holding（无限期，不复用窗口限制）"""
        dbm = _make_db(tmp_path)
        try:
            _seed_succeeded_run(dbm, "r1", self.BK, 100)
            # 候选 pending 且 resolved_at 极旧（pending 行无 resolved_at，构造旧值）
            cid = _add_pending_candidate(dbm, self.BK)
            _set_candidate_status(dbm, cid, "pending", resolved_at=_local_days_ago(365))

            res = _enqueue(dbm, run_id="r2", business_key=self.BK, sync_record_id=200)
            assert res == {"decision": "reuse_holding", "run_id": "r1"}
            assert dbm.agent_runs.get_run("r2") is None
            assert dbm.agent_runs.get_run("r1")["sync_record_id"] == 200
        finally:
            dbm._connection._conn.close()

    def test_succeeded_with_rejected_candidate_within_window_reuses(self, tmp_path):
        """S6 rejected 且 resolved_at 20 天前（<30 天）→ reuse_holding"""
        dbm = _make_db(tmp_path)
        try:
            _seed_succeeded_run(dbm, "r1", self.BK, 100)
            cid = _add_pending_candidate(dbm, self.BK)
            _set_candidate_status(dbm, cid, "rejected", resolved_at=_local_days_ago(20))

            res = _enqueue(dbm, run_id="r2", business_key=self.BK, sync_record_id=200)
            assert res == {"decision": "reuse_holding", "run_id": "r1"}
            assert dbm.agent_runs.get_run("r2") is None
        finally:
            dbm._connection._conn.close()

    def test_succeeded_with_rejected_candidate_outside_window_creates(self, tmp_path):
        """S6 rejected 且 resolved_at 40 天前（>30 天）→ created 重新评估"""
        dbm = _make_db(tmp_path)
        try:
            _seed_succeeded_run(dbm, "r1", self.BK, 100)
            cid = _add_pending_candidate(dbm, self.BK)
            _set_candidate_status(dbm, cid, "rejected", resolved_at=_local_days_ago(40))

            res = _enqueue(dbm, run_id="r2", business_key=self.BK, sync_record_id=200)
            assert res == {"decision": "created", "run_id": "r2"}
            assert dbm.agent_runs.get_run("r2")["status"] == "pending"
        finally:
            dbm._connection._conn.close()

    def test_succeeded_with_confirmed_candidate_valid_mapping_reuses_accepted(
        self, tmp_path
    ):
        """S7 confirmed + accepted_mapping_valid=True → reuse_accepted（不限时间）"""
        dbm = _make_db(tmp_path)
        try:
            _seed_succeeded_run(dbm, "r1", self.BK, 100)
            cid = _add_pending_candidate(dbm, self.BK)
            _set_candidate_status(
                dbm, cid, "confirmed", resolved_at=_local_days_ago(900)
            )

            res = _enqueue(
                dbm,
                run_id="r2",
                business_key=self.BK,
                sync_record_id=200,
                accepted_mapping_valid=True,
            )
            assert res == {"decision": "reuse_accepted", "run_id": "r1"}
            assert dbm.agent_runs.get_run("r2") is None
        finally:
            dbm._connection._conn.close()

    def test_succeeded_with_confirmed_candidate_invalid_mapping_creates(self, tmp_path):
        """S7 confirmed + accepted_mapping_valid=False（映射已删除）→ created"""
        dbm = _make_db(tmp_path)
        try:
            _seed_succeeded_run(dbm, "r1", self.BK, 100)
            cid = _add_pending_candidate(dbm, self.BK)
            _set_candidate_status(dbm, cid, "confirmed", resolved_at=_local_days_ago(5))

            res = _enqueue(
                dbm,
                run_id="r2",
                business_key=self.BK,
                sync_record_id=200,
                accepted_mapping_valid=False,
            )
            assert res == {"decision": "created", "run_id": "r2"}
            assert dbm.agent_runs.get_run("r2")["status"] == "pending"
        finally:
            dbm._connection._conn.close()

    def test_succeeded_without_candidate_creates(self, tmp_path):
        """succeeded 终态但无候选行 → created"""
        dbm = _make_db(tmp_path)
        try:
            _seed_succeeded_run(dbm, "r1", self.BK, 100)
            res = _enqueue(dbm, run_id="r2", business_key=self.BK, sync_record_id=200)
            assert res == {"decision": "created", "run_id": "r2"}
        finally:
            dbm._connection._conn.close()

    def test_succeeded_with_distant_candidate_creates(self, tmp_path):
        """同键候选存在但 business_key 不同 → 不影响决策（无候选 → created）"""
        dbm = _make_db(tmp_path)
        try:
            _seed_succeeded_run(dbm, "r1", self.BK, 100)
            _add_pending_candidate(dbm, "match|bob|other|1")
            res = _enqueue(dbm, run_id="r2", business_key=self.BK, sync_record_id=200)
            assert res == {"decision": "created", "run_id": "r2"}
        finally:
            dbm._connection._conn.close()

    def test_cancelled_creates(self, tmp_path):
        """cancelled / 其他终态 → created"""
        dbm = _make_db(tmp_path)
        try:
            _enqueue(dbm, run_id="r1", business_key=self.BK, sync_record_id=100)
            _set_status(dbm, "r1", "cancelled", ended_at=946684800)

            res = _enqueue(dbm, run_id="r2", business_key=self.BK, sync_record_id=200)
            assert res == {"decision": "created", "run_id": "r2"}
        finally:
            dbm._connection._conn.close()

    def test_empty_business_key_creates_each_time(self, tmp_path):
        """business_key 为空（去重禁用）→ 每次 created，不去重"""
        dbm = _make_db(tmp_path)
        try:
            res1 = _enqueue(dbm, run_id="r1", business_key="", sync_record_id=100)
            res2 = _enqueue(dbm, run_id="r2", business_key="", sync_record_id=200)
            assert res1 == {"decision": "created", "run_id": "r1"}
            assert res2 == {"decision": "created", "run_id": "r2"}
            conn = dbm._connection._conn
            count = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
            assert count == 2
        finally:
            dbm._connection._conn.close()

    def test_in_flight_none_sync_record_keeps_pointer(self, tmp_path):
        """in_flight 且 sync_record_id=None（前移场景）→ 主指针不变"""
        dbm = _make_db(tmp_path)
        try:
            _enqueue(dbm, run_id="run-a", business_key=self.BK, sync_record_id=100)
            res = _enqueue(
                dbm, run_id="run-a2", business_key=self.BK, sync_record_id=None
            )
            assert res == {"decision": "in_flight", "run_id": "run-a"}
            assert dbm.agent_runs.get_run("run-a")["sync_record_id"] == 100
        finally:
            dbm._connection._conn.close()

    def test_policy_params_are_required_keywords(self, tmp_path):
        """P2-5 决策策略参数必填（禁默认值兜底）：任一缺失 → TypeError"""
        dbm = _make_db(tmp_path)
        try:
            with pytest.raises(TypeError):
                dbm.agent_runs.enqueue_match_run(run_id="r", business_key=self.BK)
            with pytest.raises(TypeError):
                dbm.agent_runs.enqueue_match_run(
                    run_id="r",
                    business_key=self.BK,
                    reuse_window_days=_REUSE_WINDOW_DAYS,
                    max_total_attempts=_MAX_TOTAL_ATTEMPTS,
                )
            with pytest.raises(TypeError):
                dbm.agent_runs.enqueue_match_run(
                    run_id="r",
                    business_key=self.BK,
                    reuse_window_days=_REUSE_WINDOW_DAYS,
                    accepted_mapping_valid=True,
                )
        finally:
            dbm._connection._conn.close()

    def test_internal_exception_propagates_not_fake_created(
        self, tmp_path, monkeypatch
    ):
        """P2-1 内部异常必须抛出（不得谎报 created），由调用方降级 enqueue_failed"""
        dbm = _make_db(tmp_path)
        try:

            def _boom(conn, business_key):
                raise RuntimeError("db down")

            monkeypatch.setattr(
                AgentRunsRepository,
                "_find_latest_by_business_key",
                staticmethod(_boom),
            )
            with pytest.raises(RuntimeError, match="db down"):
                _enqueue(dbm, run_id="r", business_key=self.BK)
            assert dbm.agent_runs.get_run("r") is None
        finally:
            dbm._connection._conn.close()

    def test_integrity_error_falls_back_to_in_flight(self, tmp_path, monkeypatch):
        """P2-1 并发唯一索引冲突（IntegrityError）→ 兜底复用已在途 run，不抛出"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            conn.execute(
                "INSERT INTO agent_runs "
                "(run_id, task_type, sync_record_id, business_key, status, "
                " attempts, total_attempts, created_at, last_attempt_at) "
                "VALUES ('concurrent-run', 'match', 100, ?, 'pending', 0, 0, 1000, 1000)",
                (self.BK,),
            )
            conn.commit()
            # 模拟并发决策：本连接查询看不到并发写入的在途行 → INSERT 触发唯一索引冲突
            monkeypatch.setattr(
                AgentRunsRepository,
                "_find_latest_by_business_key",
                staticmethod(lambda conn, business_key: None),
            )

            res = _enqueue(dbm, run_id="new-run", business_key=self.BK)
            assert res == {"decision": "in_flight", "run_id": "concurrent-run"}
            assert dbm.agent_runs.get_run("new-run") is None
        finally:
            dbm._connection._conn.close()


# ---------------------------------------------------------------------------
# P2-4：进程级 active 集合由 tests/conftest.py 的 autouse fixture 统一隔离
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _seed_scheduler_active_runs():
    """S5：模块首条测试前预先污染进程级 active 集合。

    conftest 的 autouse fixture 在每条测试 setup 阶段清理；模块级 fixture 的
    setup 早于函数级 fixture，因此首条用例即可观测到 conftest 清理已生效。
    """
    import app.services.llm_match_scheduler as sched_module

    sched_module._active_run_ids.add("s5-leftover")
    yield


def test_scheduler_active_runs_cleared_by_conftest_fixture():
    """S5：预置的 active run 元素被 conftest 的 autouse fixture 清空。"""
    import app.services.llm_match_scheduler as sched_module

    assert sched_module._active_run_ids == set()

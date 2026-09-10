"""agent_runs / agent_steps 表与 repository 测试

验证 T7 的核心行为：
1. 建表：agent_runs / agent_steps 表与索引存在；status 枚举不含 exhausted
2. create_pending / atomic_claim（双调度器竞争）
3. mark_succeeded / mark_applied / mark_rejected 守卫（仅 succeeded 可 applied/rejected）
4. increment_attempts 到 3 置 failed
5. 去重查询 find_active_by_sync_record（pending/processing/succeeded/no_suggestion 命中；超保留期 no_suggestion 不命中）
6. requeue_failed（total_attempts<=10 重置；>10 拒绝）
7. cleanup_terminal（先删 steps 再删 runs，无孤儿 steps）
8. get_steps 按 (iteration, sequence) 排序
9. list_pending / list_stale_processing / find_failed_by_sync_record 辅助
"""

from pathlib import Path
from typing import Optional

from app.core.database import DatabaseManager


def _make_db(tmp_path: Path) -> DatabaseManager:
    """创建指向临时路径的 DatabaseManager 实例"""
    db_path = str(tmp_path / "test_agent_runs.db")
    return DatabaseManager(db_path)


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
    """建表与索引验证（status 枚举不含 exhausted）"""

    def test_tables_and_indexes_created(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('agent_runs', 'agent_steps')"
            )
            names = {r[0] for r in cur.fetchall()}
            assert names == {"agent_runs", "agent_steps"}

            # status 枚举不含 exhausted（exhausted 仅作 stop_reason，不作 status）
            cur = conn.execute("SELECT sql FROM sqlite_master WHERE name='agent_runs'")
            sql = cur.fetchone()[0].lower()
            assert "exhausted" not in sql

            # 索引存在
            cur = conn.execute("PRAGMA index_list('agent_runs')")
            idx = {r[1] for r in cur.fetchall()}
            assert "idx_agent_runs_sync_record_id" in idx
            assert "idx_agent_runs_status" in idx
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
    """mark_succeeded / mark_applied / mark_rejected 守卫"""

    def test_applied_rejected_only_from_succeeded(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            # 非 succeeded 时 applied 应返回 False 且不改变状态
            dbm.agent_runs.create_pending("r1", "match", 1)
            assert dbm.agent_runs.mark_applied("r1") is False
            assert dbm.agent_runs.get_run("r1")["status"] == "pending"

            # succeeded 后可 applied
            assert dbm.agent_runs.mark_succeeded("r1") is True
            assert dbm.agent_runs.get_run("r1")["status"] == "succeeded"
            assert dbm.agent_runs.mark_applied("r1") is True
            assert dbm.agent_runs.get_run("r1")["status"] == "applied"

            # applied 之后不能再 rejected（守卫：仅 succeeded 可流转）
            assert dbm.agent_runs.mark_rejected("r1") is False

            # 另一条：succeeded 后 rejected
            dbm.agent_runs.create_pending("r2", "match", 1)
            dbm.agent_runs.mark_succeeded("r2")
            assert dbm.agent_runs.mark_rejected("r2") is True
            assert dbm.agent_runs.get_run("r2")["status"] == "rejected"
        finally:
            dbm._connection._conn.close()

    def test_mark_failed_records_terminal(self, tmp_path):
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
        finally:
            dbm._connection._conn.close()


class TestIncrementAttempts:
    """increment_attempts 到 3 置 failed"""

    def test_increment_to_three_sets_failed(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("r3", "match", 1)
            dbm.agent_runs.atomic_claim("r3")  # → processing
            a1 = dbm.agent_runs.increment_attempts("r3")
            assert a1 == 1
            assert dbm.agent_runs.get_run("r3")["status"] == "processing"
            a2 = dbm.agent_runs.increment_attempts("r3")
            assert a2 == 2
            assert dbm.agent_runs.get_run("r3")["status"] == "processing"
            a3 = dbm.agent_runs.increment_attempts("r3")
            assert a3 == 3
            run = dbm.agent_runs.get_run("r3")
            assert run["status"] == "failed"
            assert run["stop_reason"] == "failed"
            assert run["ended_at"]
        finally:
            dbm._connection._conn.close()


class TestFindActiveBySyncRecord:
    """去重查询：pending/processing/succeeded/no_suggestion（未超保留期）均命中；超期不命中"""

    def test_pending_processing_succeeded_hit(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            # pending
            dbm.agent_runs.create_pending("dp", "match", 100)
            assert dbm.agent_runs.find_active_by_sync_record(100)["status"] == "pending"

            # processing
            dbm.agent_runs.create_pending("dp2", "match", 101)
            dbm.agent_runs.atomic_claim("dp2")
            assert (
                dbm.agent_runs.find_active_by_sync_record(101)["status"] == "processing"
            )

            # succeeded
            dbm.agent_runs.create_pending("dp3", "match", 102)
            dbm.agent_runs.atomic_claim("dp3")
            dbm.agent_runs.mark_succeeded("dp3")
            assert (
                dbm.agent_runs.find_active_by_sync_record(102)["status"] == "succeeded"
            )
        finally:
            dbm._connection._conn.close()

    def test_no_suggestion_within_retention_hits(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("dns", "match", 200)
            dbm.agent_runs.mark_no_suggestion("dns")  # ended_at = now
            assert (
                dbm.agent_runs.find_active_by_sync_record(200)["status"]
                == "no_suggestion"
            )
        finally:
            dbm._connection._conn.close()

    def test_no_suggestion_exceeded_retention_misses(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("dnx", "match", 201)
            dbm.agent_runs.mark_no_suggestion("dnx")
            # 把 ended_at 改到远早于保留期（epoch 秒整数，2000-01-01）
            _set_status(dbm, "dnx", "no_suggestion", ended_at=946684800)
            assert dbm.agent_runs.find_active_by_sync_record(201) is None
        finally:
            dbm._connection._conn.close()


class TestRequeueFailed:
    """requeue_failed：total_attempts<=10 重置成功；>10 拒绝"""

    def test_requeue_resets_attempts(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rq", "match", 1)
            dbm.agent_runs.mark_failed("rq", "failed", "err", 0)
            assert dbm.agent_runs.get_run("rq")["status"] == "failed"
            assert dbm.agent_runs.get_run("rq")["total_attempts"] == 0

            assert dbm.agent_runs.requeue_failed("rq") is True
            run = dbm.agent_runs.get_run("rq")
            assert run["status"] == "pending"
            assert run["attempts"] == 0
            assert run["total_attempts"] == 1
        finally:
            dbm._connection._conn.close()

    def test_requeue_rejects_when_total_attempts_exceeded(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("rq2", "match", 1)
            dbm.agent_runs.mark_failed("rq2", "failed", "err", 0)
            conn = dbm._connection._conn
            # 边界：total_attempts=10 仍允许（→ 11）
            conn.execute(
                "UPDATE agent_runs SET total_attempts=10, status='failed', attempts=5 WHERE run_id=?",
                ("rq2",),
            )
            conn.commit()
            assert dbm.agent_runs.requeue_failed("rq2") is True
            assert dbm.agent_runs.get_run("rq2")["total_attempts"] == 11
            assert dbm.agent_runs.get_run("rq2")["status"] == "pending"

            # 再次失败 → total_attempts=11 → 拒绝再入队
            dbm.agent_runs.mark_failed("rq2", "failed", "err", 0)
            assert dbm.agent_runs.requeue_failed("rq2") is False
            assert dbm.agent_runs.get_run("rq2")["status"] == "failed"
            assert dbm.agent_runs.get_run("rq2")["total_attempts"] == 11
        finally:
            dbm._connection._conn.close()


class TestFindFailedBySyncRecord:
    def test_returns_failed_row_with_total_attempts(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("ff", "match", 999)
            dbm.agent_runs.mark_failed("ff", "failed", "boom", 7)
            row = dbm.agent_runs.find_failed_by_sync_record(999)
            assert row is not None
            assert row["status"] == "failed"
            assert row["total_attempts"] == 0
            assert row["last_error"] == "boom"
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
    """get_steps 按 (iteration, sequence) 排序"""

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

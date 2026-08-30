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


def _set_status(dbm, run_id: str, status: str, ended_at: Optional[str] = None) -> None:
    """测试辅助：直接改写 run 状态（绕过业务方法，便于构造超期场景）"""
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
                "payload_json",
                "replay_delta",
                "started_at",
                "ended_at",
            ):
                assert c in cols
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
            # 把 ended_at 改到远早于保留期
            _set_status(dbm, "dnx", "no_suggestion", ended_at="2000-01-01 00:00:00")
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


class TestCleanupTerminal:
    """终态超保留期：先删 steps 再删 runs，无孤儿 steps"""

    def test_cleanup_deletes_steps_then_runs(self, tmp_path):
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
            conn.execute(
                "UPDATE agent_runs SET ended_at='2000-01-01 00:00:00' WHERE run_id=?",
                ("cleanup-a",),
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

            deleted = dbm.agent_runs.cleanup_terminal(7)
            assert deleted == 1

            # A 已删，B 仍在
            assert dbm.agent_runs.get_run("cleanup-a") is None
            assert dbm.agent_runs.get_run("cleanup-b") is not None

            # 无孤儿 steps：A 的 steps 已随 runs 删除
            assert dbm.agent_runs.get_steps("cleanup-a") == []
            # B 的 steps 保留
            assert len(dbm.agent_runs.get_steps("cleanup-b")) == 1
        finally:
            dbm._connection._conn.close()

    def test_cleanup_keeps_recent_terminal(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            dbm.agent_runs.create_pending("keep", "match", 1)
            dbm.agent_runs.mark_succeeded("keep")
            # ended_at = now（未超期）
            assert dbm.agent_runs.cleanup_terminal(7) == 0
            assert dbm.agent_runs.get_run("keep") is not None
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
            conn.execute(
                "UPDATE agent_runs SET started_at='2000-01-01 00:00:00' WHERE run_id=?",
                ("stale",),
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

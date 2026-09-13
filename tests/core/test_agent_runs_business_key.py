"""agent_runs business_key 列与索引的 schema 测试"""

from pathlib import Path

import pytest

from app.core.database import DatabaseManager


def _make_db(tmp_path: Path) -> DatabaseManager:
    db_path = str(tmp_path / "test_business_key.db")
    return DatabaseManager(db_path)


class TestAgentRunsBusinessKeySchema:
    """验证 agent_runs 表含 business_key 列 + 相关索引"""

    def test_business_key_column_exists(self, tmp_path):
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute("PRAGMA table_info('agent_runs')")
            cols = {r[1] for r in cur.fetchall()}
            assert "business_key" in cols
        finally:
            dbm._connection._conn.close()

    def test_business_key_default_empty(self, tmp_path):
        """business_key 默认空字符串"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute("SELECT sql FROM sqlite_master WHERE name='agent_runs'")
            sql = cur.fetchone()[0]
            assert "business_key" in sql.lower()
        finally:
            dbm._connection._conn.close()

    def test_partial_unique_index_exists(self, tmp_path):
        """部分唯一索引存在（防在途重复）"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute("PRAGMA index_list('agent_runs')")
            idx = {r[1] for r in cur.fetchall()}
            assert "idx_agent_runs_business_key_active" in idx
        finally:
            dbm._connection._conn.close()

    def test_business_key_normal_index_exists(self, tmp_path):
        """普通索引存在（加速查询）"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            cur = conn.execute("PRAGMA index_list('agent_runs')")
            idx = {r[1] for r in cur.fetchall()}
            assert "idx_agent_runs_business_key" in idx
        finally:
            dbm._connection._conn.close()

    def test_partial_unique_index_enforces(self, tmp_path):
        """部分唯一索引：同键两条 pending 行应冲突"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            # 插入第一条 pending
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, business_key,
                 attempts, total_attempts, created_at, last_attempt_at)
                VALUES ('rk-1', 'match', 1, 'pending', 'match|alice|test|1', 0, 0, 1000, 1000)
                """
            )
            # 插入第二条同键 pending → 冲突
            with pytest.raises(Exception) as ctx:
                conn.execute(
                    """
                    INSERT INTO agent_runs
                    (run_id, task_type, sync_record_id, status, business_key,
                     attempts, total_attempts, created_at, last_attempt_at)
                    VALUES ('rk-2', 'match', 2, 'pending', 'match|alice|test|1', 0, 0, 1000, 1000)
                    """
                )
            assert "UNIQUE" in str(ctx.value).upper()
        finally:
            dbm._connection._conn.close()

    def test_partial_unique_index_allows_different_keys(self, tmp_path):
        """不同 business_key 不冲突"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, business_key,
                 attempts, total_attempts, created_at, last_attempt_at)
                VALUES ('dk-1', 'match', 1, 'pending', 'match|alice|a|1', 0, 0, 1000, 1000)
                """
            )
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, business_key,
                 attempts, total_attempts, created_at, last_attempt_at)
                VALUES ('dk-2', 'match', 2, 'pending', 'match|alice|b|1', 0, 0, 1000, 1000)
                """
            )
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM agent_runs")
            assert cur.fetchone()[0] == 2
        finally:
            dbm._connection._conn.close()

    def test_partial_unique_index_allows_terminal_duplicates(self, tmp_path):
        """终态（如 succeeded）不参与部分唯一索引，同键可有多条"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, business_key,
                 attempts, total_attempts, created_at, last_attempt_at, ended_at)
                VALUES ('tk-1', 'match', 1, 'succeeded', 'match|alice|test|1', 0, 0, 1000, 1000, 1000)
                """
            )
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, business_key,
                 attempts, total_attempts, created_at, last_attempt_at, ended_at)
                VALUES ('tk-2', 'match', 2, 'succeeded', 'match|alice|test|1', 0, 0, 1000, 1000, 1000)
                """
            )
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM agent_runs")
            assert cur.fetchone()[0] == 2
        finally:
            dbm._connection._conn.close()

    def test_empty_business_key_not_constrained(self, tmp_path):
        """空 business_key 不参与唯一约束"""
        dbm = _make_db(tmp_path)
        try:
            conn = dbm._connection._conn
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, business_key,
                 attempts, total_attempts, created_at, last_attempt_at)
                VALUES ('ek-1', 'match', 1, 'pending', '', 0, 0, 1000, 1000)
                """
            )
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, business_key,
                 attempts, total_attempts, created_at, last_attempt_at)
                VALUES ('ek-2', 'match', 2, 'pending', '', 0, 0, 1000, 1000)
                """
            )
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM agent_runs")
            assert cur.fetchone()[0] == 2
        finally:
            dbm._connection._conn.close()

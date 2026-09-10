"""Agent 通用会话仓库（agent_runs / agent_steps）

承载通用 Agent 会话状态机与可重放 span 日志：
- agent_runs：一次会话（pending -> processing -> succeeded / no_suggestion / failed ...）
- agent_steps：每轮 LLM 调用 / 每次工具执行的 span（按 iteration, sequence 排序）

关键并发与守卫语义：
- atomic_claim：原子 UPDATE `WHERE status='pending'`，受影响行数=0 视为抢占失败
- mark_applied / mark_rejected：仅当 status='succeeded' 可流转（WHERE 守卫）
- increment_attempts：调度轮次失败计数，>=3 转 failed
- requeue_failed：total_attempts<=10 才重置入队，>10 拒绝
- cleanup_expired：滑动窗口轮转，单条 DELETE（FK 级联删 steps）
"""

import json
import time
from typing import Any, Optional

from .base_repository import BaseRepository

# 终态：除 pending / processing 外的全部状态
_TERMINAL_STATUSES = (
    "succeeded",
    "no_suggestion",
    "failed",
    "cancelled",
    "applied",
    "rejected",
)

# 活性态：pending / processing（cleanup_expired 的活性腿用）
_ACTIVE_STATUSES = ("pending", "processing")


def _now() -> int:
    """当前 epoch 秒整数（与 agent_steps/agent_runs 时间列格式一致，便于整数比较）。"""
    return int(time.time())


def _encrypt_replay_delta(raw: Any) -> str:
    """加密 replay_delta（best-effort：失败降级存明文，不中断主流程）。

    接受 str / dict / list 等类型：非字符串先序列化为 JSON 再加密。
    """
    if not raw:
        return raw if isinstance(raw, str) else ""
    try:
        from ..config_secret_crypto import encrypt

        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        return encrypt(text)
    except Exception as e:  # best-effort：加密失败降级存明文
        import logging

        logging.getLogger(__name__).warning(
            f"[agent_runs] replay_delta 加密失败（已降级存明文）: {e}"
        )
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


def _decrypt_replay_delta(stored: Any) -> str:
    """解密 replay_delta（容错无前缀明文：原样返回）。"""
    if not stored:
        return stored if isinstance(stored, str) else ""
    try:
        from ..config_secret_crypto import decrypt

        return decrypt(stored)
    except Exception:  # 容错：解密失败原样返回
        return stored if isinstance(stored, str) else ""


class AgentRunsRepository(BaseRepository):
    """agent_runs / agent_steps 的增删改查与状态机流转"""

    # ------------------------------------------------------------------
    # 写入：会话创建与状态流转
    # ------------------------------------------------------------------

    def create_pending(
        self, run_id: str, task_type: str, sync_record_id: Optional[int] = None
    ) -> int:
        """沉淀一条 pending 会话，返回记录 id（失败时 0）"""

        def _write(conn):
            ts = _now()
            cursor = conn.execute(
                """
                INSERT INTO agent_runs
                (run_id, task_type, sync_record_id, status, attempts,
                 total_attempts, created_at, last_attempt_at)
                VALUES (?, ?, ?, 'pending', 0, 0, ?, ?)
                """,
                (run_id, task_type, sync_record_id, ts, ts),
            )
            return cursor.lastrowid

        return self._run_write(_write, error_msg="创建 agent_run 失败", default=0)

    def atomic_claim(self, run_id: str) -> bool:
        """原子抢占：仅当 status='pending' 时置 processing。

        受影响行数=0（已被其它调度器抢占）返回 False；否则 True。
        """

        def _write(conn):
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status='processing', started_at=?
                WHERE run_id=? AND status='pending'
                """,
                (_now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="原子拾取 agent_run 失败", default=False
        )

    def mark_succeeded(
        self, run_id: str, stop_reason: str = "", total_tokens: int = 0
    ) -> bool:
        """标记成功（产出建议并通过校验），记录终态时间"""

        def _write(conn):
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status='succeeded', stop_reason=?, total_tokens=?, ended_at=?
                WHERE run_id=?
                """,
                (stop_reason, total_tokens, _now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="标记 agent_run succeeded 失败", default=False
        )

    def mark_no_suggestion(
        self, run_id: str, stop_reason: str = "", last_error: str = ""
    ) -> bool:
        """标记无建议（预算耗尽 / 校验失败 / 无候选），终态"""

        def _write(conn):
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status='no_suggestion', stop_reason=?, last_error=?, ended_at=?
                WHERE run_id=?
                """,
                (stop_reason, last_error, _now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="标记 agent_run no_suggestion 失败", default=False
        )

    def mark_failed(
        self, run_id: str, stop_reason: str, last_error: str, total_tokens: int = 0
    ) -> bool:
        """标记失败（LLM 调用失败 attempts 达上限），记录终态时间"""

        def _write(conn):
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status='failed', stop_reason=?, last_error=?, total_tokens=?,
                    ended_at=?
                WHERE run_id=?
                """,
                (stop_reason, last_error, total_tokens, _now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="标记 agent_run failed 失败", default=False
        )

    def mark_applied(self, run_id: str) -> bool:
        """标记建议被应用（用户确认建议）。

        守卫：仅当 status='succeeded' 可流转，行数=0（非 succeeded）返回 False。
        """

        def _write(conn):
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status='applied', ended_at=?
                WHERE run_id=? AND status='succeeded'
                """,
                (_now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="标记 agent_run applied 失败", default=False
        )

    def mark_rejected(self, run_id: str) -> bool:
        """标记建议被忽略（用户忽略）。

        守卫：仅当 status='succeeded' 可流转，行数=0（非 succeeded）返回 False。
        """

        def _write(conn):
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status='rejected', ended_at=?
                WHERE run_id=? AND status='succeeded'
                """,
                (_now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="标记 agent_run rejected 失败", default=False
        )

    def increment_attempts(self, run_id: str) -> int:
        """累加调度轮次失败次数（仅 processing 态有效），返回最新 attempts。

        attempts>=3 时置 failed（stop_reason='failed'）并记终态时间。
        """

        def _write(conn):
            cursor = conn.execute(
                "UPDATE agent_runs SET attempts = attempts + 1, last_attempt_at=? "
                "WHERE run_id=? AND status='processing'",
                (_now(), run_id),
            )
            if cursor.rowcount == 0:
                return 0
            row = conn.execute(
                "SELECT attempts FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            attempts = row[0] if row else 0
            if attempts >= 3:
                conn.execute(
                    "UPDATE agent_runs SET status='failed', stop_reason='failed', "
                    "ended_at=? WHERE run_id=?",
                    (_now(), run_id),
                )
            return attempts

        return self._run_write(
            _write, error_msg="累加 agent_run attempts 失败", default=0
        )

    def refresh_started_at(self, run_id: str) -> bool:
        """恢复扫描时刷新 started_at=now()（仅 processing 态有效）。

        防止下一轮重复恢复同一崩溃遗留 run；行数=0（非 processing）返回 False。
        """

        def _write(conn):
            cursor = conn.execute(
                "UPDATE agent_runs SET started_at=? "
                "WHERE run_id=? AND status='processing'",
                (_now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="刷新 agent_run started_at 失败", default=False
        )

    def update_run_status(self, run_id: str, status: str) -> bool:
        """通用状态改写辅助（写入辅助；业务流转优先用上面的语义化方法）"""

        def _write(conn):
            cursor = conn.execute(
                "UPDATE agent_runs SET status=? WHERE run_id=?", (status, run_id)
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="更新 agent_run 状态失败", default=False
        )

    # ------------------------------------------------------------------
    # 重新入队与清理
    # ------------------------------------------------------------------

    def requeue_failed(self, run_id: str) -> bool:
        """失败会话重新入队：total_attempts<=10 时重置 attempts 并 +1 total_attempts。

        >10（即已 11 次）拒绝再入队，返回 False；否则返回 True。
        """

        def _write(conn):
            row = conn.execute(
                "SELECT total_attempts FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                return False
            if row[0] > 10:
                return False
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status='pending', attempts=0, total_attempts=total_attempts + 1,
                    created_at=?, last_attempt_at=?, started_at=NULL,
                    ended_at=NULL, last_error=''
                WHERE run_id=?
                """,
                (_now(), _now(), run_id),
            )
            return cursor.rowcount > 0

        return self._run_write(
            _write, error_msg="重新入队 agent_run 失败", default=False
        )

    def cleanup_expired(self, retention_days: int) -> int:
        """按滑动窗口轮转清理过期 runs（单条 DELETE，FK 级联删 steps）。

        两腿 OR：
        - 终态腿：status IN (terminal) AND ended_at > 0 AND ended_at < cutoff
        - 活性腿：status IN (pending, processing) AND created_at < cutoff（过期死行一并删）

        retention_days <= 0 → 直接 return 0（永不清理语义，参照 sync_records.cleanup_old_records）。
        时间比较统一为 epoch 秒整数。
        """

        if retention_days <= 0:
            return 0

        cutoff = _now() - retention_days * 86400
        terminal_ph = ",".join("?" * len(_TERMINAL_STATUSES))
        active_ph = ",".join("?" * len(_ACTIVE_STATUSES))

        def _write(conn):
            cursor = conn.execute(
                f"""
                DELETE FROM agent_runs
                WHERE (status IN ({terminal_ph}) AND ended_at > 0 AND ended_at < ?)
                   OR (status IN ({active_ph}) AND created_at < ?)
                """,
                _TERMINAL_STATUSES + (cutoff,) + _ACTIVE_STATUSES + (cutoff,),
            )
            return cursor.rowcount

        return self._run_write(_write, error_msg="清理过期 agent_run 失败", default=0)

    # ------------------------------------------------------------------
    # 查询：去重 / 调度器辅助
    # ------------------------------------------------------------------

    def find_active_by_sync_record(
        self, sync_record_id: int, retention_days: int = 7
    ) -> Optional[dict]:
        """按 sync_record_id 查活跃会话（去重用）。

        命中：status IN (pending, processing, succeeded)；或 no_suggestion 且
        ended_at 在保留期内（未超保留期也算活跃）。无则返回 None。
        时间比较统一为 epoch 秒整数。
        """
        cutoff = _now() - retention_days * 86400

        def _read(conn):
            cursor = conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE sync_record_id=? AND status IN
                    ('pending', 'processing', 'succeeded')
                ORDER BY id DESC LIMIT 1
                """,
                (sync_record_id,),
            )
            row = cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                return dict(zip(cols, row))

            # no_suggestion 仅在保留期内算活跃
            cursor = conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE sync_record_id=? AND status='no_suggestion'
                  AND ended_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (sync_record_id, cutoff),
            )
            row = cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                return dict(zip(cols, row))
            return None

        return self._run_read(_read, error_msg="查询活跃 agent_run 失败", default=None)

    def find_latest_by_sync_record(self, sync_record_id: int) -> Optional[dict]:
        """按 sync_record_id 查最新一条会话（任意状态，按 id DESC），无则 None。

        用于前端候选页：返回最新 agent run 的状态与 run_id（徽标 / 评估过程折叠区）。
        与 find_active_by_sync_record 不同，本方法不过滤状态，只要存在即返回，
        以便「AI 评估过程」折叠区在 run 终态（succeeded/applied/failed 等）后仍可回看。
        """

        def _read(conn):
            cursor = conn.execute(
                "SELECT * FROM agent_runs WHERE sync_record_id=? "
                "ORDER BY id DESC LIMIT 1",
                (sync_record_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

        return self._run_read(_read, error_msg="查询最新 agent_run 失败", default=None)

    def find_failed_by_sync_record(self, sync_record_id: int) -> Optional[dict]:
        """按 sync_record_id 查最近一条 failed 会话（重新入队用）"""

        def _read(conn):
            cursor = conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE sync_record_id=? AND status='failed'
                ORDER BY id DESC LIMIT 1
                """,
                (sync_record_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

        return self._run_read(_read, error_msg="查询失败 agent_run 失败", default=None)

    def list_pending(self, limit: int = 50) -> list:
        """列出 pending 会话（供调度器拾取），按 id 升序"""

        def _read(conn):
            cursor = conn.execute(
                "SELECT * FROM agent_runs WHERE status='pending' "
                "ORDER BY id ASC LIMIT ?",
                (limit,),
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

        return self._run_read(
            _read, error_msg="列出 pending agent_run 失败", default=[]
        )

    def list_stale_processing(self, timeout_seconds: int = 120) -> list:
        """列出 started_at 超时的 processing 会话（崩溃遗留恢复用）。
        时间比较统一为 epoch 秒整数。
        """

        cutoff = _now() - timeout_seconds

        def _read(conn):
            cursor = conn.execute(
                "SELECT * FROM agent_runs WHERE status='processing' "
                "AND started_at > 0 AND started_at < ? ORDER BY id ASC",
                (cutoff,),
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

        return self._run_read(
            _read, error_msg="列出超时 processing agent_run 失败", default=[]
        )

    def get_run(self, run_id: str) -> Optional[dict]:
        """按 run_id 查询单条会话"""

        def _read(conn):
            cursor = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

        return self._run_read(_read, error_msg="查询 agent_run 失败", default=None)

    # ------------------------------------------------------------------
    # agent_steps 写入与查询
    # ------------------------------------------------------------------

    def add_step(self, step: dict) -> int:
        """写入一条 span（agent_steps），返回记录 id（失败时 0）。

        replay_delta 写入前统一加密（best-effort：失败降级存明文）。
        started_at/ended_at 为 epoch 秒整数（0 表示未设置，写入时取当前时间）。
        """

        started_at = step.get("started_at") or _now()
        ended_at = step.get("ended_at") or _now()
        replay_delta = _encrypt_replay_delta(step.get("replay_delta", ""))

        def _write(conn):
            cursor = conn.execute(
                """
                INSERT INTO agent_steps
                (run_id, span_id, parent_id, name, status, model, tokens,
                 latency_ms, tool_name, input_summary, error, iteration,
                 sequence, replay_delta, started_at, ended_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    step.get("run_id", ""),
                    step.get("span_id", ""),
                    step.get("parent_id", ""),
                    step.get("name", ""),
                    step.get("status", "ok"),
                    step.get("model", ""),
                    step.get("tokens", 0),
                    step.get("latency_ms", 0),
                    step.get("tool_name", ""),
                    step.get("input_summary", ""),
                    step.get("error", ""),
                    step.get("iteration", 0),
                    step.get("sequence", 0),
                    replay_delta,
                    started_at,
                    ended_at,
                ),
            )
            return cursor.lastrowid

        return self._run_write(_write, error_msg="写入 agent_step 失败", default=0)

    def update_step(self, span_id: str, **fields: Any) -> bool:
        """更新一条 span（agent_steps），供 trace.end_span / record_budget_message 调用。

        支持的字段：status, model, tokens, latency_ms, tool_name, input_summary,
        error, replay_delta, started_at, ended_at。
        replay_delta 写入前统一加密（best-effort：失败降级存明文）。
        返回是否成功（无变化或异常均返回 False）。
        """
        allowed = {
            "status",
            "model",
            "tokens",
            "latency_ms",
            "tool_name",
            "input_summary",
            "error",
            "replay_delta",
            "started_at",
            "ended_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        if "replay_delta" in updates:
            updates["replay_delta"] = _encrypt_replay_delta(updates["replay_delta"])

        cols = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [span_id]

        def _write(conn):
            cursor = conn.execute(
                f"UPDATE agent_steps SET {cols} WHERE span_id=?", values
            )
            return cursor.rowcount > 0

        return self._run_write(_write, error_msg="更新 agent_step 失败", default=False)

    def get_steps(self, run_id: str) -> list:
        """按 run_id 查询 span 列表，按 (iteration, sequence) 排序。

        replay_delta 读取时统一解密（容错无前缀明文：原样返回）。
        """

        def _read(conn):
            cursor = conn.execute(
                "SELECT * FROM agent_steps WHERE run_id=? "
                "ORDER BY iteration ASC, sequence ASC",
                (run_id,),
            )
            cols = [d[0] for d in cursor.description]
            rows = []
            for r in cursor.fetchall():
                d = dict(zip(cols, r))
                d["replay_delta"] = _decrypt_replay_delta(d.get("replay_delta"))
                rows.append(d)
            return rows

        return self._run_read(_read, error_msg="查询 agent_steps 失败", default=[])

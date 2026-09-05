"""Span 记录器与断点重放（otel 概念，自建不引 SDK）。

提供：
- ``start_span`` / ``end_span``：写入 ``agent_steps``（独立 best-effort 事务，失败仅日志），
  承载可重放会话日志。
- ``record_budget_message``：将透明预算消息并入最后一条 ``tool_execute`` 的 ``replay_delta``。
- ``replay``：从 ``agent_steps`` 按 ``(iteration, sequence)`` 重放会话增量，
  重建可续跑的 ``messages``（断点恢复重建规则）。

replay_delta 写入语义：
- ``llm_chat.end_span``: ``replay_delta = {response: {stop_reason, content, tool_calls}}``
  （tool_calls 为本轮全部工具调用的聚合——重建一条 assistant 消息的唯一来源）。
- ``tool_execute.end_span``: ``replay_delta = {tool_result: {...}}``（仅 tool_result）。
- 预算消息：由 ``record_budget_message`` 并入同轮最后一个 ``tool_execute`` 的
  ``replay_delta``（``budget_message`` 字段）。

职责分离：``payload_json`` 仅观测摘要（结构化截断 ≤2KB 保 JSON 合法）；
``replay_delta`` 为断点重放增量（完整，超 32KB 标记该 span ``status=error`` 视为不可恢复点）。
``input_summary`` 仅记录参数名与类型（不记录参数值）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.core.database import get_database_manager
from app.core.logging import logger
from app.services.llm.models import Message, ToolResultBlock, ToolUseBlock

# 观测摘要上限（payload_json 截断 ≤2KB）
MAX_PAYLOAD_JSON_BYTES = 2 * 1024
# replay_delta 上限（超 32KB 标记该 span status=error，视为不可恢复点）
MAX_REPLAY_DELTA_BYTES = 32 * 1024
# input_summary 上限（≤500 字符）
MAX_INPUT_SUMMARY_CHARS = 500


def _now() -> str:
    """本地时间字符串（与 repository 写入的 started_at/ended_at 同格式，便于字符串比较）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# 观测摘要结构化截断（保 JSON 合法）
# ----------------------------------------------------------------------


def _shrink(obj: Any, max_bytes: int) -> Any:
    """递归截断字符串叶子，使 JSON 序列化后 ≤ max_bytes（近似自底向上）。"""
    if len(json.dumps(obj, ensure_ascii=False).encode("utf-8")) <= max_bytes:
        return obj
    if isinstance(obj, str):
        return obj[: max(0, max_bytes - 16)]
    if isinstance(obj, dict):
        return {k: _shrink(v, max_bytes) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shrink(v, max_bytes) for v in obj]
    return obj


def truncate_json(payload: Any, max_bytes: int = MAX_PAYLOAD_JSON_BYTES) -> str:
    """将 payload 序列化为 JSON 字符串，并在超出 max_bytes 时结构化截断，保证结果仍是合法 JSON。

    - 小 payload：原样返回。
    - 大 payload：先尝试递归缩短字符串叶子以保留原始结构；若仍超限，降级为
      ``{"truncated": true, "preview": "<前缀>"}`` 包壳，确保大小上限与 JSON 合法性。
    """
    text = (
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    )
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    try:
        obj = json.loads(text) if isinstance(payload, str) else payload
    except (ValueError, TypeError):
        obj = text
    shrunk = _shrink(obj, max_bytes)
    result = json.dumps(shrunk, ensure_ascii=False)
    if len(result.encode("utf-8")) <= max_bytes:
        return result
    # 兜底：包成截断预览，保证合法 JSON 与大小上限
    preview = text[: max(0, max_bytes - 64)]
    return json.dumps({"truncated": True, "preview": preview}, ensure_ascii=False)


# ----------------------------------------------------------------------
# span 记录（独立 best-effort 事务）
# ----------------------------------------------------------------------


def start_span(
    run_id: str,
    name: str,
    iteration: int,
    sequence: int,
    parent_id: str = "",
) -> str:
    """开始一条 span，写入 ``agent_steps`` 并返回 span_id（uuid hex）。

    失败（DB 异常）仅记录日志并返回生成的 span_id，不影响主流程。
    """
    span_id = uuid.uuid4().hex
    try:
        dbm = get_database_manager()
        dbm.agent_runs.add_step(
            {
                "run_id": run_id,
                "span_id": span_id,
                "parent_id": parent_id,
                "name": name,
                "status": "ok",
                "iteration": iteration,
                "sequence": sequence,
            }
        )
    except Exception as e:  # best-effort：失败不影响主流程
        logger.error(f"[trace] start_span 写入失败（已忽略）: {e}")
    return span_id


def _normalize_replay_delta(replay_delta: Any) -> str:
    if isinstance(replay_delta, str):
        return replay_delta
    return json.dumps(replay_delta, ensure_ascii=False)


def end_span(
    span_id: str,
    *,
    status: str = "ok",
    model: str = "",
    tokens: int = 0,
    latency_ms: int = 0,
    tool_name: str = "",
    input_summary: str = "",
    error: str = "",
    payload_json: Any = "",
    replay_delta: Any = "",
) -> None:
    """结束一条 span，更新 ``agent_steps``（独立 best-effort 事务，失败仅日志）。

    - ``payload_json``：经结构化截断（≤2KB 保 JSON 合法）。
    - ``replay_delta``：序列化后若超 32KB，将该 span 标记为 ``status=error``（不可恢复点）。
    - ``input_summary``：截断至 500 字符（仅参数名与类型，不记录参数值）。
    """
    try:
        payload_str = (
            truncate_json(payload_json) if payload_json not in ("", None) else ""
        )
        delta_str = (
            _normalize_replay_delta(replay_delta)
            if replay_delta not in ("", None)
            else ""
        )
        final_status = status
        if delta_str and len(delta_str.encode("utf-8")) > MAX_REPLAY_DELTA_BYTES:
            final_status = "error"
        input_summary_str = (input_summary or "")[:MAX_INPUT_SUMMARY_CHARS]

        dbm = get_database_manager()

        def _write(conn):
            conn.execute(
                """
                UPDATE agent_steps
                SET status=?, model=?, tokens=?, latency_ms=?, tool_name=?,
                    input_summary=?, error=?, payload_json=?, replay_delta=?, ended_at=?
                WHERE span_id=?
                """,
                (
                    final_status,
                    model,
                    tokens,
                    latency_ms,
                    tool_name,
                    input_summary_str,
                    error,
                    payload_str,
                    delta_str,
                    _now(),
                    span_id,
                ),
            )

        dbm.agent_runs._run_write(
            _write, error_msg="[trace] end_span 更新失败（已忽略）"
        )
    except Exception as e:  # best-effort：失败不影响主流程
        logger.error(f"[trace] end_span 失败（已忽略）: {e}")


def record_budget_message(span_id: str, budget_message: str) -> None:
    """将透明预算消息并入指定 span（通常是同轮最后一个 ``tool_execute``）的 replay_delta。

    在现有 replay_delta 上追加 ``budget_message`` 字段。独立 best-effort 事务。
    """
    try:
        dbm = get_database_manager()

        def _write(conn):
            cur = conn.execute(
                "SELECT replay_delta FROM agent_steps WHERE span_id=?", (span_id,)
            )
            row = cur.fetchone()
            if not row:
                return
            raw = row[0] or "{}"
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                obj = {}
            obj["budget_message"] = budget_message
            conn.execute(
                "UPDATE agent_steps SET replay_delta=? WHERE span_id=?",
                (json.dumps(obj, ensure_ascii=False), span_id),
            )

        dbm.agent_runs._run_write(
            _write, error_msg="[trace] record_budget_message 失败（已忽略）"
        )
    except Exception as e:  # best-effort
        logger.error(f"[trace] record_budget_message 失败（已忽略）: {e}")


# ----------------------------------------------------------------------
# 断点重放
# ----------------------------------------------------------------------


@dataclass
class ReplayResult:
    """断点重放结果。

    - ``messages``：重建的可续跑消息列表（seed 前缀 + 各轮重建的消息）。
    - ``executed_iterations``：已完整重放的轮数（调用方可据此计算剩余轮次）。
    - ``missing_tool_calls``：最后一轮 llm_chat 声明的工具调用中、尚未记录
      tool_execute 的缺失项（供调用方补执行；readonly 校验由调用方做）。
    - ``last_response``：最后一条完整 llm_chat 的响应（当其未产生工具/已终止时，
      调用方直接消费分派 end_turn / tool_use / submit；为 None 表示应直接进入下一轮 chat）。
    - ``unrecoverable_iteration``：存在 status=error 的 span 时，返回其 iteration，
      供调用方从该轮重新 chat 丢弃其后所有已存响应；无则为 None。
    """

    messages: list = field(default_factory=list)
    executed_iterations: int = 0
    missing_tool_calls: list = field(default_factory=list)
    last_response: dict | None = None
    unrecoverable_iteration: int | None = None

    # 类型注解（运行期仍为 list，便于混合 seed 与重建消息）：
    # messages: list[Message] —— seed 前缀（调用方提供，应为 Message）+ 各轮重建的 Message
    #   - assistant: Message(role="assistant", content=list[ToolUseBlock])
    #   - 每条 tool_result: Message(role="user", content=[ToolResultBlock(...)])(逐条不合并)
    #   - 预算: Message(role="user", content=f"[剩余轮次：N]")
    # 该列表可直接作为 loop.run(seed_messages=...) 的入参。


def _parse_json(raw: str, default: Any = None) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default


def _parse_response(chat_step: dict) -> dict:
    obj = _parse_json(chat_step.get("replay_delta"), {})
    return obj.get("response", {}) if isinstance(obj, dict) else {}


def _parse_tool_result(step: dict) -> dict | None:
    obj = _parse_json(step.get("replay_delta"), {})
    if not isinstance(obj, dict):
        return None
    tr = obj.get("tool_result")
    return tr if isinstance(tr, dict) else None


def _extract_budget_message(step: dict) -> str | None:
    obj = _parse_json(step.get("replay_delta"), {})
    if not isinstance(obj, dict):
        return None
    bm = obj.get("budget_message")
    return bm if isinstance(bm, str) else None


def replay(
    run_id: str,
    seed_builder: Callable[[], list],
    max_iterations: int | None = None,
) -> ReplayResult:
    """按 (iteration, sequence) 重放会话增量，重建可续跑 ``list[Message]``。

    ``seed_builder`` 返回种子消息（system + user 列表），由场景层提供
    （llm_assist 从 sync_records 还原），原样保留（调用方应返回 ``Message`` 实例，
    以便整体可直接作为 ``loop.run(seed_messages=...)`` 消费的列表）。

    重建规则（与原执行 ``loop.run`` 完全一致）：
    - 每轮从 ``llm_chat.replay_delta`` 重建**一条** assistant 消息：
      ``Message(role="assistant", content=[ToolUseBlock(...) for tc in tool_calls])``
      （content 为 ``list[ToolUseBlock]``，与原执行对齐）。
    - 逐条追加各 ``tool_execute.replay_delta.tool_result`` 重建的
      ``Message(role="user", content=[ToolResultBlock(...)])``（每条工具结果独立成消息，不合并）。
    - 预算消息：``Message(role="user", content=f"[剩余轮次：N]")``。
      优先用存储的 ``budget_message``（同轮最后 tool_execute 已并入）；
      缺失时按 ``N = max_iterations - executed_iterations`` 计算（用已执行轮数而非
      iteration 索引，避免稀疏 iteration 时算错剩余轮次）。
    - 缺失工具识别（S(tool_calls) - R(已记录 tool_execute)）逻辑不变；命中缺失的该轮
      不追加预算消息、不计入 executed_iterations，交回调用方补执行。
    """
    dbm = get_database_manager()
    steps = dbm.agent_runs.get_steps(run_id)

    steps_by_iter: dict[int, list] = {}
    for s in steps:
        steps_by_iter.setdefault(s["iteration"], []).append(s)

    # 不可恢复点：任何 status=error 的 span 标记其 iteration（取最小）
    unrecoverable: int | None = None
    for s in steps:
        if s.get("status") == "error":
            it = s["iteration"]
            if unrecoverable is None or it < unrecoverable:
                unrecoverable = it

    seed = list(seed_builder()) if seed_builder else []
    messages: list = list(seed)
    executed_iterations = 0
    missing_tool_calls: list = []
    last_response: dict | None = None

    for it in sorted(steps_by_iter.keys()):
        if unrecoverable is not None and it >= unrecoverable:
            # 到达不可恢复点：停止重建，交回调用方从该轮重新 chat
            break

        isteps = steps_by_iter[it]
        chat = next((s for s in isteps if s["name"] == "llm_chat"), None)
        if chat is None:
            # 该轮无 llm_chat（异常数据），视为不可恢复
            unrecoverable = it
            break

        response = _parse_response(chat)
        tool_calls = response.get("tool_calls") or []
        # 重建 assistant 消息：content 为 list[ToolUseBlock]（与原执行对齐）
        assistant_msg = Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    input=tc.get("input", {}) or {},
                )
                for tc in tool_calls
            ],
        )

        if tool_calls:
            messages.append(assistant_msg)
            recorded_ids: set = set()
            budget_message: str | None = None
            for t in sorted(isteps, key=lambda x: x["sequence"]):
                if t["name"] != "tool_execute":
                    continue
                tr = _parse_tool_result(t)
                if tr is None:
                    continue
                # 逐条 tool_result 独立成 Message（不合并），与原执行一致
                messages.append(
                    Message(
                        role="user",
                        content=[
                            ToolResultBlock(
                                tool_use_id=tr.get("tool_use_id", ""),
                                content=tr.get("content", ""),
                                is_error=bool(tr.get("is_error", False)),
                            )
                        ],
                    )
                )
                recorded_ids.add(tr.get("tool_use_id"))
                bm = _extract_budget_message(t)
                if bm is not None:
                    budget_message = bm

            missing = [tc for tc in tool_calls if tc.get("id") not in recorded_ids]
            if missing:
                # 最后一轮工具未全部执行完：返回缺失项供调用方补执行；
                # 该轮未完整，不追加预算消息（避免与调用方重跑该轮产生的预算重复）
                missing_tool_calls = missing
                last_response = response
                break

            # 本轮完整执行：计入 executed_iterations 并追加预算消息
            executed_iterations += 1
            if budget_message is None and max_iterations is not None:
                # 确定性重建预算消息：用已执行轮数而非 iteration 索引
                budget_message = (
                    f"[剩余轮次：{max(0, max_iterations - executed_iterations)}]"
                )
            if budget_message is not None:
                messages.append(Message(role="user", content=budget_message))
            last_response = None
        else:
            # 无工具调用（end_turn / submit_suggestion）：终局响应，直接交回调用方消费
            last_response = response
            break

    return ReplayResult(
        messages=messages,
        executed_iterations=executed_iterations,
        missing_tool_calls=missing_tool_calls,
        last_response=last_response,
        unrecoverable_iteration=unrecoverable,
    )

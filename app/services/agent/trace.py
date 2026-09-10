"""Span 记录器与断点重放（otel 概念，自建不引 SDK）。

存储形态（单表双职责）：
- ``agent_steps`` 是唯一存储；trace 是它的全部，replay 是它的一个读取视角。
- **类型化列**（status / model / tokens / latency_ms / tool_name / input_summary /
  error / iteration / sequence）：payload 摘要，供观测与索引。
- **replay_delta**：重放所需全部增量（seed / response / tool_result / budget_message），
  完整、Fernet 加密（BGS1: 前缀）、永不截断、无大小上限、无 error 标记机制。

提供：
- ``start_span`` / ``end_span``：写入 ``agent_steps``（独立 best-effort 事务，失败仅日志），
  承载可重放会话日志。时间列统一 epoch 秒整数。
- ``record_budget_message``：将透明预算消息并入最后一条 ``tool_execute`` 的 ``replay_delta``。
- ``replay``：从 ``agent_steps`` 按 ``(iteration, sequence)`` 重放会话增量，
  重建可续跑的 ``messages``（断点恢复重建规则）。

replay_delta 写入语义：
- ``llm_chat.end_span``: ``replay_delta = {response: {stop_reason, content, tool_calls}}``
  （tool_calls 为本轮全部工具调用的聚合——重建一条 assistant 消息的唯一来源）。
- ``tool_execute.end_span``: ``replay_delta = {tool_result: {...}}``（仅 tool_result）。
- 预算消息：由 ``record_budget_message`` 并入同轮最后一个 ``tool_execute`` 的
  ``replay_delta``（``budget_message`` 字段）。

读取/展示侧的截断（含 ``...[shrinked]`` 标记）由 ``app.utils.truncate`` 承担；
写入路径零截断。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config_secret_crypto import decrypt, encrypt
from app.core.database import get_database_manager
from app.core.logging import logger
from app.services.llm.models import Message, ToolResultBlock, ToolUseBlock

# input_summary 上限（≤500 字符，仅参数名与类型，不记录参数值）
MAX_INPUT_SUMMARY_CHARS = 500


def _now() -> int:
    """当前 epoch 秒整数（与 agent_steps/agent_runs 时间列格式一致）。"""
    return int(time.time())


# ----------------------------------------------------------------------
# span 记录（独立 best-effort 事务）
# ----------------------------------------------------------------------


def start_span(
    run_id: str,
    name: str,
    iteration: int,
    sequence: int,
    parent_id: str = "",
    *,
    started_at: int | None = None,
) -> str:
    """开始一条 span，写入 ``agent_steps`` 并返回 span_id（uuid hex）。

    ``started_at`` 为 epoch 秒整数（None → 当前时间）。
    失败（DB 异常）仅记录日志并返回生成的 span_id，不影响主流程。
    """
    span_id = uuid.uuid4().hex
    ts = started_at if started_at is not None else _now()
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
                "started_at": ts,
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
    replay_delta: Any = "",
    started_at: int | None = None,
    ended_at: int | None = None,
) -> None:
    """结束一条 span，更新 ``agent_steps``（独立 best-effort 事务，失败仅日志）。

    - ``replay_delta``：完整保存，无大小上限、无截断、无 error 标记（加密由仓储层统一处理）。
    - ``input_summary``：截断至 500 字符（仅参数名与类型，不记录参数值）。
    - ``started_at``：epoch 秒整数；**仅显式传入时**才写回，未传时 UPDATE 不触碰
      ``start_span`` 已写入的真实开始时间。
    - ``ended_at``：epoch 秒整数（None → 当前时间）。
    """
    try:
        delta_str = (
            _normalize_replay_delta(replay_delta)
            if replay_delta not in ("", None)
            else ""
        )
        input_summary_str = (input_summary or "")[:MAX_INPUT_SUMMARY_CHARS]
        ts_ended = ended_at if ended_at is not None else _now()

        fields: dict[str, Any] = dict(
            status=status,
            model=model,
            tokens=tokens,
            latency_ms=latency_ms,
            tool_name=tool_name,
            input_summary=input_summary_str,
            error=error,
            replay_delta=delta_str,
            ended_at=ts_ended,
        )
        # 仅显式传入 started_at 时才写回，避免覆盖 start_span 已写入的真实开始时间
        if started_at is not None:
            fields["started_at"] = started_at

        dbm = get_database_manager()
        dbm.agent_runs.update_step(span_id, **fields)
    except Exception as e:  # best-effort：失败不影响主流程
        logger.error(f"[trace] end_span 失败（已忽略）: {e}")


def record_budget_message(span_id: str, budget_message: str) -> None:
    """将透明预算消息并入指定 span（通常是同轮最后一个 ``tool_execute``）的 replay_delta。

    在现有 replay_delta 上追加 ``budget_message`` 字段。独立 best-effort 事务。
    读改写路径：SELECT → 解密 → 改 → 加密写回（加密由仓储层 decrypt/encrypt 处理）。

    安全约束：任何解密/解析失败路径都**保留原 raw 不变**（不写库），仅记 warning 日志。
    仅当成功解析为 dict 时才合并 budget_message 并写回。
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
            raw = row[0] or ""
            # 解密（容错无前缀明文）；解析失败保留原 raw，不写库
            try:
                obj = json.loads(decrypt(raw))
            except Exception:
                logger.warning(
                    "[trace] record_budget_message 解密/解析失败，保留原 raw 不写库"
                )
                return
            if not isinstance(obj, dict):
                logger.warning(
                    "[trace] record_budget_message 解析结果非 dict，保留原 raw 不写库"
                )
                return
            obj["budget_message"] = budget_message
            try:
                new_delta = encrypt(json.dumps(obj, ensure_ascii=False))
            except Exception:
                new_delta = json.dumps(obj, ensure_ascii=False)
            conn.execute(
                "UPDATE agent_steps SET replay_delta=? WHERE span_id=?",
                (new_delta, span_id),
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
    """

    messages: list = field(default_factory=list)
    executed_iterations: int = 0
    missing_tool_calls: list = field(default_factory=list)
    last_response: dict | None = None


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


def replay(run_id: str) -> ReplayResult:
    """按 (iteration, sequence, id) 重放会话增量，重建可续跑 ``list[Message]``。

    排序以 ``(iteration, sequence)`` 为主键、``id`` 为末级 tie-break，确保 seed 行
    与首轮 llm_chat 同 ``(0, 0)`` 时顺序稳定。

    种子消息从 ``name="seed"`` 行的 ``replay_delta.seed_messages`` 还原
    （seed 行由写入方在 run 启动时写入），无需调用方提供额外入参。

    重建规则（与原执行 ``loop.run`` 完全一致）：
    - 每轮从 ``llm_chat.replay_delta`` 重建**一条** assistant 消息：
      ``Message(role="assistant", content=[ToolUseBlock(...) for tc in tool_calls])``
      （content 为 ``list[ToolUseBlock]``，与原执行对齐）。
    - 逐条追加各 ``tool_execute.replay_delta.tool_result`` 重建的
      ``Message(role="user", content=[ToolResultBlock(...)])``（每条工具结果独立成消息，不合并）。
    - 预算消息：优先用存储的 ``budget_message``（同轮最后 tool_execute 已并入）；
       缺失时不追加预算消息。
    - 缺失工具识别（S(tool_calls) - R(已记录 tool_execute)）逻辑不变；命中缺失的该轮
       不追加预算消息、不计入 executed_iterations，交回调用方补执行。
    - 行缺失 / 空 delta：在该轮 break（executed_iterations 不含该轮，调用方从该轮重新 chat）。
    """
    dbm = get_database_manager()
    steps = dbm.agent_runs.get_steps(run_id)

    steps_by_iter: dict[int, list] = {}
    seed_messages: list[Message] = []
    for s in steps:
        if s["name"] == "seed":
            # 提取种子消息
            obj = _parse_json(s.get("replay_delta"), {})
            if isinstance(obj, dict):
                for m in obj.get("seed_messages") or []:
                    if isinstance(m, dict):
                        try:
                            seed_messages.append(Message.model_validate(m))
                        except Exception:
                            pass
            continue
        steps_by_iter.setdefault(s["iteration"], []).append(s)

    messages: list = list(seed_messages)
    executed_iterations = 0
    missing_tool_calls: list = []
    last_response: dict | None = None

    for it in sorted(steps_by_iter.keys()):
        isteps = steps_by_iter[it]
        chat = next((s for s in isteps if s["name"] == "llm_chat"), None)
        if chat is None:
            # 该轮无 llm_chat（异常数据）：在该轮 break，交回调用方从该轮重新 chat
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

        if not response:
            # 空 delta（异常数据）：在该轮 break，交回调用方从该轮重新 chat
            break

        if tool_calls:
            messages.append(assistant_msg)
            recorded_ids: set = set()
            budget_message: str | None = None
            for t in sorted(isteps, key=lambda x: (x["sequence"], x["id"])):
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
                # 该轮未完整，不追加预算消息、不计入 executed_iterations
                missing_tool_calls = missing
                last_response = response
                break

            # 本轮完整执行：计入 executed_iterations 并追加预算消息
            executed_iterations += 1
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
    )

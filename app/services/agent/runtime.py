"""通用 Agent 运行时：run 编排与恢复续跑状态机。

与业务无关：全部场景差异通过 :class:`~app.services.agent.scenario.ScenarioHooks`
注入，运行时只透传 ``ctx``（场景上下文）给场景钩子，不感知其内部结构。

对外入口：

- :func:`run`：原子抢占 → 工具注册 → 构建 seed → 通用循环 → 终局处理。
- :func:`continue_run`：崩溃恢复续跑单一入口（replay → 分派 → 补执行 →
  续跑 → 终局处理）；异常在函数内部消化，不向调用方抛出。

失败语义（与既有行为一致）：

- ``LLMCallError(retryable=False)`` → ``mark_failed(stop_reason='llm_error')``
- ``LLMCallError(retryable=True)`` → ``increment_attempts``（达上限由仓储单点置 failed）
- 其他异常 → error 日志 + ``increment_attempts``
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from app.core.database import get_database_manager
from app.core.logging import logger
from app.services.agent import trace
from app.services.agent.loop import RunResult, run as loop_run
from app.services.agent.recorder import TraceRecorder
from app.services.agent.scenario import ScenarioHooks
from app.services.llm.client import LLMCallError
from app.services.llm.models import Message, ToolResultBlock, ToolUseBlock
from app.services.llm.tools import ToolRegistry, serialize_tool_result

# 非 read（write/terminal/未注册）缺失工具的占位 tool_result 文案
# ——不重放副作用，仅闭合会话协议，真实调用由续跑 loop 触发
_SKIP_PLACEHOLDER_CONTENT = "skipped: will be re-invoked in continuation"


async def run(
    run_id: str,
    *,
    hooks: ScenarioHooks,
    ctx: Any,
    thinking_level: str,
    chat_fn: Callable | None = None,
    notification_service: Any | None = None,
    span_recorder: Any | None = None,
) -> str:
    """执行一次 Agent 场景任务（通用编排），返回终态 status 字符串。

    status 取值：``succeeded`` / ``no_suggestion`` / ``failed`` / ``processing``
    （调度轮次重试中）/ ``skipped``（并发抢占失败，由调用方忽略）。
    """
    dbm = get_database_manager()

    # 原子抢占（F3）：失败表示已被其它调度器处理
    if not dbm.agent_runs.atomic_claim(run_id):
        return "skipped"

    # per-run ToolRegistry（评论#7）：每个 run 独立实例，handler 闭包只绑定本次
    # ctx（含访问凭据），避免并发入口下互相覆盖导致串账号。
    registry = ToolRegistry()
    defns = hooks.register_tools(registry, ctx)
    tools_schemas = [d.to_schema() for d in defns]

    seed = hooks.build_seed(ctx)

    if span_recorder is None:
        span_recorder = TraceRecorder(run_id, start_iteration=0)

    # 轮次预算：场景侧读取集中配置（显式覆盖 > thinking_level 策略 > 默认兜底），
    # 由场景保证单一来源。
    max_iterations = hooks.resolve_max_iterations(thinking_level)

    if chat_fn is None:
        chat_fn = hooks.build_chat_fn(thinking_level)

    # 包装 chat_fn（chat span）并写 seed 行
    wrapped_chat_fn = span_recorder.wrap_chat_fn(chat_fn)
    span_recorder.write_seed_row(seed)

    # LLM 调用异常（chat_fn 抛错）→ 按可重试性分流
    try:
        result = await loop_run(
            chat_fn=wrapped_chat_fn,
            tools_schemas=tools_schemas,
            tool_calls_fn=functools.partial(
                registry.execute_batch, recorder=span_recorder
            ),
            max_iterations=max_iterations,
            tool_choice_terminal=hooks.terminal_tool,
            seed_messages=seed,
            recorder=span_recorder,
        )
    except LLMCallError as e:
        # LLMCallError 携带 retryable 标志区分可重试/确定性失败
        if not e.retryable:
            # 确定性失败（401/403/400/refusal）→ 立即标记 failed，不浪费重试次数
            logger.error(f"[{hooks.task_type}] run {run_id} 确定性 LLM 失败: {e}")
            dbm.agent_runs.mark_failed(
                run_id,
                stop_reason="llm_error",
                last_error=str(e),
                total_tokens=span_recorder.total_tokens,
            )
            return "failed"
        # 可重试（429/5xx/超时）→ 累加 attempts；达上限时由仓储在**同一次调用事务内**
        # 单点置终态（status='failed' + last_error），此处不得再 mark_failed（避免双写）。
        logger.error(f"[{hooks.task_type}] run {run_id} 可重试 LLM 失败: {e}")
        attempts = dbm.agent_runs.increment_attempts(run_id, last_error=str(e))
        return "failed" if attempts >= 3 else "processing"
    except Exception as e:
        logger.error(f"[{hooks.task_type}] run {run_id} LLM 调用异常: {e}")
        attempts = dbm.agent_runs.increment_attempts(run_id, last_error=str(e))
        return "failed" if attempts >= 3 else "processing"

    return await hooks.handle_terminal(
        dbm,
        run_id,
        result,
        ctx,
        total_tokens=span_recorder.total_tokens,
        notification_service=notification_service,
    )


async def continue_run(
    run_id: str,
    *,
    hooks: ScenarioHooks,
    ctx: Any,
    notification_service: Any | None = None,
) -> None:
    """恢复续跑单一入口（通用状态机）：replay → 补执行 → 续跑 → 终局处理。

    流程：
    1. 场景钩子解析 thinking_level / max_iterations（集中配置单一来源）
    2. ``trace.replay`` 重建可续跑消息列表与终局响应
    3. ``last_response`` 终局优先分派（**先于**预算耗尽判定）：
       - ``end_turn`` → 直接 mark_no_suggestion（不调 LLM）
       - ``terminal_tool``（或 tool_calls 含终止工具）→ 走场景终局处理
       - ``None``（全部轮次已完整记录）→ 若预算耗尽则落终态，否则续跑 loop
       - 含 tool_use → 补执行缺失只读工具 + 回填结果后续跑 loop
    4. ``remaining <= 0`` 且非终局 → 落终态 no_suggestion/exhausted
       （否则 run 永久滞留 processing）

    顺序说明：末轮可能已产出 submit 但 run 中断，若先判预算耗尽会误判 exhausted
    丢失提交，故终局语义必须先消费。

    失败分流（与既有语义一致）：
    - ``LLMCallError(retryable=False)`` → ``mark_failed(stop_reason='llm_error')``
    - ``LLMCallError(retryable=True)`` → ``increment_attempts(last_error=...)``
    - 其他异常 → error 日志 + ``increment_attempts(last_error=...)``

    异常在本函数内部消化，不向调用方抛出（调用方仅负责执行权释放与兜底日志）。
    """
    dbm = get_database_manager()
    repo = dbm.agent_runs

    try:
        # 思考强度与轮次预算统一由场景从集中配置读取（G3：非法/非正数覆盖值
        # 由场景配置解析统一告警并回退）
        thinking_level = hooks.resolve_thinking_level()
        max_iterations = hooks.resolve_max_iterations(thinking_level)

        # seed 由 replay 从 agent_steps 的 seed 行提取，无需此处重建
        replay_result = trace.replay(run_id)
        remaining = max_iterations - replay_result.executed_iterations

        last_response = replay_result.last_response
        if last_response is None:
            # 全部轮次已完整记录 → 预算耗尽则落终态，否则以剩余轮次续跑通用循环
            if remaining <= 0:
                _mark_exhausted(
                    repo,
                    run_id,
                    note="replay 前",
                    total_tokens=replay_result.total_tokens,
                )
                return
            await _execute_continuation(
                dbm,
                run_id,
                replay_result,
                thinking_level,
                remaining=remaining,
                hooks=hooks,
                ctx=ctx,
                notification_service=notification_service,
                anchor_replayed_round=bool(replay_result.missing_tool_calls),
            )
            return

        # F2：终局响应直接分派（**先于**预算耗尽判定），避免无谓重调 LLM 与
        # 末轮已 submit 却被误判 exhausted 丢失提交。
        stop = last_response.get("stop_reason")
        tcs = last_response.get("tool_calls") or []

        if stop == "end_turn":
            # 无建议：直接标记，不调 LLM（透传 replay 累计 tokens，口径同其它终态）
            repo.mark_no_suggestion(
                run_id,
                stop_reason="end_turn",
                total_tokens=replay_result.total_tokens,
            )
            return

        # 捕获终止工具调用（终局）→ 走场景校验落库路径
        submit_tc = next(
            (tc for tc in tcs if tc.get("name") == hooks.terminal_tool), None
        )
        if stop == hooks.terminal_tool or submit_tc is not None:
            sug = (submit_tc or {}).get("input") or {}
            result = RunResult(
                stop_reason=hooks.terminal_tool,
                suggestion=sug,
                last_response=None,
            )
            await hooks.handle_terminal(
                dbm,
                run_id,
                result,
                ctx,
                # 恢复路径无实时 recorder：已发生轮次的 tokens 由 replay 从
                # agent_steps 的 llm_chat span 累计回传（口径见 ReplayResult.total_tokens）。
                total_tokens=replay_result.total_tokens,
                notification_service=notification_service,
            )
            return

        # 含 tool_use（非终局，存在缺失工具）→ 补执行 + 回填后继续 loop
        # 该轮 LLM 已发生过，计入预算（F2：remaining 已减）
        remaining = max(0, remaining - 1)
        if remaining <= 0:
            _mark_exhausted(
                repo,
                run_id,
                note="补执行后",
                total_tokens=replay_result.total_tokens,
            )
            return

        await _execute_continuation(
            dbm,
            run_id,
            replay_result,
            thinking_level,
            remaining=remaining,
            hooks=hooks,
            ctx=ctx,
            notification_service=notification_service,
            anchor_replayed_round=True,
        )
    except LLMCallError as e:
        # LLM 调用失败：按可重试性分流
        if not e.retryable:
            logger.error(f"🤖 恢复续跑 {run_id} 确定性 LLM 失败: {e}")
            repo.mark_failed(run_id, stop_reason="llm_error", last_error=str(e))
        else:
            logger.error(f"🤖 恢复续跑 {run_id} 可重试 LLM 失败: {e}")
            repo.increment_attempts(run_id, last_error=str(e))
    except Exception as e:
        # P2-3：附带当前 run 状态，便于区分「处理中异常」与「已终态后的异常」
        logger.error(
            f"🤖 恢复续跑 {run_id} 异常（当前 run 状态="
            f"{_current_run_status(repo, run_id)}）: {e}"
        )
        repo.increment_attempts(run_id, last_error=str(e))


def _mark_exhausted(repo, run_id: str, *, note: str, total_tokens: int = 0) -> None:
    """预算耗尽且无终局语义 → 落终态 no_suggestion/exhausted。

    G4：不能直接 return（否则 run 永久滞留 processing，下一轮恢复扫描又会重复捞起）。
    ``total_tokens`` 为 replay 累计的历史轮次用量，透传至终态记录（口径与其它终态一致）。
    """
    logger.warning(
        f"🤖 恢复(replay)路径预算耗尽，run {run_id} {note}已无剩余轮次，"
        f"标记 no_suggestion"
    )
    repo.mark_no_suggestion(run_id, stop_reason="exhausted", total_tokens=total_tokens)


def _current_run_status(repo, run_id: str) -> str:
    """best-effort 读取 run 当前 status，供最外层异常日志定位。

    读取失败/无记录时返回可读占位（``unknown`` / ``missing``），并记 warning，
    绝不因此抛错掩盖原始异常。
    """
    try:
        row = repo.get_run(run_id)
    except Exception as e:  # best-effort：状态读取失败不遮蔽原始异常
        logger.warning(f"🤖 恢复续跑读取 run {run_id} 状态失败（日志降级）: {e}")
        return "unknown"
    if not row:
        return "missing"
    if isinstance(row, dict):
        return str(row.get("status") or "unknown")
    return str(getattr(row, "status", "unknown"))


async def _execute_continuation(
    dbm,
    run_id: str,
    replay_result,
    thinking_level: str,
    *,
    remaining: int,
    hooks: ScenarioHooks,
    ctx: Any,
    notification_service: Any | None,
    anchor_replayed_round: bool,
) -> None:
    """注册工具 → 建 recorder → 补执行缺失工具 → 续跑 loop → 终局处理。

    ``anchor_replayed_round=True`` 用于 last_response 非 None 的补执行场景：
    recorder 需锚定到已发生的轮次（补执行 tool span 与既有 chat span 同轮），
    并让续跑 chat 从 ``executed_iterations + 1`` 开始。
    """
    # per-run ToolRegistry（评论#7）：续跑同样使用独立实例
    registry = ToolRegistry()
    defns = hooks.register_tools(registry, ctx)
    tools_schemas = [d.to_schema() for d in defns]

    span_recorder = TraceRecorder(
        run_id, start_iteration=replay_result.executed_iterations
    )
    if anchor_replayed_round:
        # begin_replayed_round 内部已设 _next_iteration = iteration + 1，
        # 无需再手动推进（避免私有属性赋值封装泄露）
        span_recorder.begin_replayed_round(replay_result.executed_iterations)

    # 补执行缺失工具并落 tool_execute span（二次 replay 不再判缺失）
    seq = 0
    for tc in replay_result.missing_tool_calls:
        await _replay_missing_tool(
            tc,
            registry,
            replay_result.messages,
            span_recorder=span_recorder,
            sequence=seq,
        )
        seq += 1

    wrapped_chat_fn = span_recorder.wrap_chat_fn(hooks.build_chat_fn(thinking_level))
    result = await loop_run(
        chat_fn=wrapped_chat_fn,
        tools_schemas=tools_schemas,
        tool_calls_fn=functools.partial(registry.execute_batch, recorder=span_recorder),
        max_iterations=remaining,
        tool_choice_terminal=hooks.terminal_tool,
        seed_messages=replay_result.messages,
        recorder=span_recorder,
    )
    await hooks.handle_terminal(
        dbm,
        run_id,
        result,
        ctx,
        # 全口径累计：replay 历史轮次（agent_steps 的 llm_chat span 累计）+
        # 本次新产生轮次（实时 recorder 累计），避免恢复续跑只记新轮、丢历史。
        total_tokens=replay_result.total_tokens + span_recorder.total_tokens,
        notification_service=notification_service,
    )


async def _replay_missing_tool(
    tool_call: dict,
    registry,
    messages: list,
    *,
    span_recorder=None,
    sequence: int = 0,
) -> None:
    """补执行单条缺失的只读工具调用（readonly 校验）。

    F4：执行结果作为 ``Message(role="user", content=[ToolResultBlock(...)])``
    追加到 ``messages``，保证 assistant(tool_use) 后存在对应的 tool_result，
    符合会话协议（每条 tool_use 有且仅有一条 tool_result）。

    G5：非只读（write/terminal）与未注册工具**不重放副作用**，但仍回填占位
    tool_result 闭合协议（否则 assistant 的 tool_use 悬空，provider 报协议错误）；
    真实调用留给续跑 loop 自然触发。

    当 ``span_recorder`` 不为 None 时，为每条缺失工具写 ``tool_execute`` span
    （iteration 由调用方通过 ``begin_replayed_round`` 锚定），保证二次 replay
    不再判缺失（replay 自包含）。
    """
    name = (tool_call or {}).get("name")
    if not name:
        return
    args = (tool_call or {}).get("input") or {}
    tool_use_id = (tool_call or {}).get("id", "")
    defn = registry.get(name)

    # 落 tool_execute span（如果提供了 recorder）
    span_id = None
    if span_recorder is not None:
        tool_use_block = ToolUseBlock(id=tool_use_id, name=name, input=args or {})
        span_id = span_recorder.start_tool(tool_use_block, sequence=sequence)

    if defn is None or defn.access != "read":
        logger.debug(f"🤖 恢复补执行：工具 {name} 非只读/未注册，回填占位结果")
        _append_tool_result(
            messages, tool_use_id, _SKIP_PLACEHOLDER_CONTENT, is_error=False
        )
        if span_id is not None:
            span_recorder.end_tool(
                span_id,
                result=ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=_SKIP_PLACEHOLDER_CONTENT,
                    is_error=False,
                ),
            )
        return

    try:
        result = await registry.execute(name, args)
        # 与 execute_batch 统一协议契约：工具结果 JSON 序列化（评论#6b）
        content = serialize_tool_result(result)
        is_error = False
    except Exception as e:
        logger.debug(f"🤖 恢复补执行工具 {name} 失败: {e}")
        content = f"工具执行失败: {type(e).__name__}"
        is_error = True
    _append_tool_result(messages, tool_use_id, content, is_error=is_error)
    if span_id is not None:
        span_recorder.end_tool(
            span_id,
            result=ToolResultBlock(
                tool_use_id=tool_use_id, content=content, is_error=is_error
            ),
        )


def _append_tool_result(
    messages: list, tool_use_id: str, content: str, *, is_error: bool
) -> None:
    """追加一条 tool_result 消息（闭合 assistant 的 tool_use，F4/G5）。"""
    messages.append(
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=tool_use_id, content=content, is_error=is_error
                )
            ],
        )
    )

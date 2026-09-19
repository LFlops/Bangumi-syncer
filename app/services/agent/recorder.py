"""通用 Agent 观测记录器（trace span 编排）。

``TraceRecorder`` 是 agent 运行时的观测设施（与业务场景无关）：

- **chat span 包装**：``wrap_chat_fn`` 返回包装后的 chat_fn，每轮 start_span
  → await → end_span（model/tokens 写专用列，不再塞 payload_json）。
- **ToolSpanRecorder 实现**：``start_tool`` / ``end_tool`` 供 execute_batch
  包裹层调用；幂等安全。
- **seed 行**：run 启动时写一条 ``name="seed"`` span，供 replay 显式提取。
- **budget 钩子**：``record_budget`` 定位本轮最后 tool span（无则回退 chat span）。

由场景层（如 ``app/services/matching/llm_assist``）在 run 编排中实例化；
span 的读写细节由 :mod:`app.services.agent.trace` 承担。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Callable

from app.core.logging import logger
from app.services.agent.loop import ChatFn
from app.services.agent.trace import (
    end_span as trace_end_span,
    record_budget_message as trace_record_budget_message,
    start_span as trace_start_span,
)
from app.services.llm.models import Message, ToolResultBlock, ToolUseBlock


def _default_clock() -> float:
    """默认时钟：秒级时间戳（浮点）。"""
    return datetime.now().timestamp()


def _input_summary(inp: dict) -> str:
    """输入摘要：仅记录参数名与类型（不记录参数值）。"""
    return ", ".join(f"{k}:{type(v).__name__}" for k, v in (inp or {}).items())


class TraceRecorder:
    """统一 trace 记录器（chat 包装 / tool span / seed 行 / budget 钩子）。

    职责：
    - **chat span 包装**：``wrap_chat_fn`` 返回包装后的 chat_fn，每轮 start_span
      → await → end_span（model/tokens 写专用列，不再塞 payload_json）。
    - **ToolSpanRecorder 实现**：``start_tool`` / ``end_tool`` 供 execute_batch
      包裹层调用；幂等安全。
    - **seed 行**：run 启动时写一条 ``name="seed"`` span，供 replay 显式提取。
    - **budget 钩子**：``record_budget`` 定位本轮最后 tool span（无则回退 chat span）。
    """

    def __init__(
        self,
        run_id: str,
        *,
        start_iteration: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.run_id = run_id
        self._clock = clock or _default_clock
        self._next_iteration: int = start_iteration
        # 当前轮的 iteration（wrap_chat_fn 开始时设定，start_tool / record_budget 读取）
        self._current_iteration: int = 0
        self._chat_span_id: str | None = None
        self._last_tool_span_id: str | None = None
        # 全轮累计 token 用量（每轮 chat 响应 usage 累加，供终态落库使用）
        self.total_tokens: int = 0
        # span_id → (tool_use, t0)，供 end_tool 检索后清除（幂等）
        self._tool_state: dict[str, tuple] = {}

    # -- chat span 包装 -----------------------------------------------------

    def wrap_chat_fn(self, chat_fn: ChatFn) -> ChatFn:
        """包装 chat_fn：每轮 start_span → await → end_span。

        iteration 状态机：
        - 每轮开始时设定 ``_current_iteration`` 为 ``_next_iteration`` 的当前值，
          然后推进 ``_next_iteration``（供下一轮使用）。
        - ``start_tool`` / ``record_budget`` 读取 ``_current_iteration``，
          保证同轮内 chat span 与全部 tool span 的 iteration 一致。
        - ``resp`` 在 ``try`` 前初始化为 ``None``，避免 chat_fn 抛异常时
          ``finally`` 引用未绑定变量（UnboundLocalError 覆盖原始异常）。
        """
        recorder = self

        async def wrapped(messages, *, tools=None, tool_choice=None):
            # 设定当前轮并推进单调计数（仅在新一轮 chat 开始时推进）
            recorder._current_iteration = recorder._next_iteration
            recorder._next_iteration += 1
            iteration = recorder._current_iteration
            span_id = trace_start_span(
                recorder.run_id, name="llm_chat", iteration=iteration, sequence=0
            )
            recorder._chat_span_id = span_id
            recorder._last_tool_span_id = None  # 新轮重置
            t0 = recorder._clock()
            resp = None
            try:
                resp = await chat_fn(messages, tools=tools, tool_choice=tool_choice)
                return resp
            finally:
                latency_ms = int((recorder._clock() - t0) * 1000)
                if resp is not None:
                    tokens = resp.usage.total_tokens if resp.usage is not None else 0
                    # 全轮累计：终态落库取累计值而非仅末轮
                    recorder.total_tokens += tokens
                    tool_calls = [
                        b.model_dump() if hasattr(b, "model_dump") else asdict(b)
                        for b in resp.blocks
                        if isinstance(b, ToolUseBlock)
                    ]
                    # 全量 blocks（含 thinking/text）：思考模型的 thinking 块必须随
                    # tool_use 回传，恢复路径 replay 据此重建 assistant 消息，否则
                    # 断点续跑的下一轮请求会 400（与 live loop 的 list(resp.blocks) 对齐）。
                    # 仅追加字段、不升级 schema：旧数据无 blocks 时读取方走 tool_calls 回退。
                    blocks = [b.model_dump() for b in resp.blocks]
                    trace_end_span(
                        span_id,
                        status="ok",
                        model=resp.model,
                        tokens=tokens,
                        latency_ms=latency_ms,
                        replay_delta={
                            "response": {
                                "stop_reason": resp.stop_reason,
                                "content": resp.content,
                                "tool_calls": tool_calls,
                                "blocks": blocks,
                            }
                        },
                    )
                else:
                    # chat_fn 抛异常：写 error span 但不遮掩原始异常
                    logger.warning(
                        f"[agent] chat_fn 异常（iteration={iteration}），写 error span"
                    )
                    trace_end_span(
                        span_id,
                        status="error",
                        latency_ms=latency_ms,
                        error="chat_fn raised before response",
                    )

        return wrapped

    # -- ToolSpanRecorder 协议 ----------------------------------------------

    def start_tool(self, tool_use: ToolUseBlock, *, sequence: int) -> str | None:
        """工具执行开始：创建 tool_execute span 并记录 t0。

        读取 ``_current_iteration``（当前轮），保证与本轮 chat span 的 iteration 一致。
        """
        span_id = trace_start_span(
            self.run_id,
            name="tool_execute",
            iteration=self._current_iteration,
            sequence=sequence,
            parent_id=self._chat_span_id or "",
        )
        self._tool_state[span_id] = (tool_use, self._clock())
        self._last_tool_span_id = span_id
        return span_id

    def end_tool(
        self,
        span_id: str,
        *,
        result: ToolResultBlock | None = None,
        error: str = "",
    ) -> None:
        """工具执行结束：幂等安全（同 span_id 二次调用不崩溃）。"""
        state = self._tool_state.pop(span_id, None)
        if state is None:
            # 已处理过（幂等）
            return
        tool_use, t0 = state
        latency_ms = int((self._clock() - t0) * 1000)

        if result is None and error:
            status = "error"
        else:
            status = "ok"

        replay_delta: dict | None = None
        if result is not None:
            replay_delta = {
                "tool_result": {
                    "tool_use_id": result.tool_use_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
            }

        trace_end_span(
            span_id,
            status=status,
            tool_name=tool_use.name,
            input_summary=_input_summary(tool_use.input),
            latency_ms=latency_ms,
            replay_delta=replay_delta,
            error=error,
        )

    # -- seed 行 ------------------------------------------------------------

    def write_seed_row(self, seed_messages: list[Message]) -> None:
        """run 启动时写一条 name='seed' span 行。"""
        span_id = trace_start_span(self.run_id, name="seed", iteration=0, sequence=0)
        seed_delta = [m.model_dump() for m in seed_messages]
        trace_end_span(
            span_id,
            status="ok",
            replay_delta={"seed_messages": seed_delta},
        )

    # -- 恢复续跑锚定 ------------------------------------------------------

    def begin_replayed_round(self, iteration: int) -> None:
        """锚定到指定轮次，供恢复路径补执行缺失工具落 span 使用。

        将 ``_current_iteration`` 设为 ``iteration``，并使 ``_next_iteration``
        设为 ``iteration + 1``（保证后续 ``wrap_chat_fn`` 从正确轮次开始推进，
        避免与补执行的 tool span 撞号）。
        """
        self._current_iteration = iteration
        self._next_iteration = iteration + 1
        self._last_tool_span_id = None

    # -- budget 钩子 -------------------------------------------------------

    def record_budget(self, budget_message: str) -> None:
        """预算钩子：并入本轮最后 tool span，无则回退 chat span。"""
        target = self._last_tool_span_id or self._chat_span_id
        if target:
            trace_record_budget_message(target, budget_message)

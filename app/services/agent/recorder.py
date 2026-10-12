"""通用 Agent 观测记录器（trace span 编排）。

``TraceRecorder`` 是 agent 运行时的观测设施（与业务场景无关）：

- **chat span 上下文**：``chat_span()`` 产出一轮的 ``async with`` 上下文（``_ChatSpan``）：
  进入时 start_span → ``feed`` 单点聚合 → 退出时**唯一**一次 finalize + end_span
  （model/tokens 写专用列，不再塞 payload_json）。
- **ToolSpanRecorder 实现**：``start_tool`` / ``end_tool`` 供 execute_batch
  包裹层调用；幂等安全。
- **seed 行**：run 启动时写一条 ``name="seed"`` span，供 replay 显式提取。
- **budget 钩子**：``record_budget`` 定位本轮最后 tool span（无则回退 chat span）。

由场景层（如 ``app/services/matching/llm_assist``）在 run 编排中实例化；
span 的读写细节由 :mod:`app.services.agent.trace` 承担。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime
from types import TracebackType

from app.core.logging import logger
from app.services.agent.protocols import BudgetRecorder, ChatSpan
from app.services.agent.trace import (
    end_span as trace_end_span,
    record_budget_message as trace_record_budget_message,
    start_span as trace_start_span,
)
from app.services.llm.models import (
    ChatResponse,
    Message,
    StreamAggregator,
    StreamChunk,
    ToolResultBlock,
    ToolUseBlock,
)


def _default_clock() -> float:
    """默认时钟：秒级时间戳（浮点）。"""
    return datetime.now().timestamp()


def _input_summary(inp: dict) -> str:
    """输入摘要：仅记录参数名与类型（不记录参数值）。"""
    return ", ".join(f"{k}:{type(v).__name__}" for k, v in (inp or {}).items())


class TraceRecorder(BudgetRecorder):
    """统一 trace 记录器（chat span / tool span / seed 行 / budget 钩子）。

    职责：
    - **chat span 上下文**：``chat_span()`` 产出一轮的 ``async with`` 上下文
      （``_ChatSpan``）：进入时 start_span、``feed`` 单点聚合、退出时**唯一**一次
      finalize + end_span（model/tokens 写专用列，不再塞 payload_json）。
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
        # 当前轮的 iteration（chat span 进入时设定，start_tool / record_budget 读取）
        self._current_iteration: int = 0
        self._chat_span_id: str | None = None
        self._last_tool_span_id: str | None = None
        # 全轮累计 token 用量（每轮 chat 响应 usage 累加，供终态落库使用）
        self.total_tokens: int = 0
        # span_id → (tool_use, t0)，供 end_tool 检索后清除（幂等）
        self._tool_state: dict[str, tuple] = {}

    # -- chat span 上下文 ---------------------------------------------------

    def chat_span(self) -> ChatSpan:
        """开一轮 chat span（``async with``）：唯一聚合点 + 唯一 finalize + 轮末入库。

        轮次编号由本记录器自持（构造时的 ``start_iteration`` 与
        ``begin_replayed_round`` 锚定）：loop 的本地轮次与持久化历史编号不同源
        （续跑从 ``executed_iterations`` 起算），故**不接收**调用方下标。

        落库时机保持轮粒度（崩溃恢复依赖）：
        ``__aenter__`` 立即 start_span；``__aexit__`` 在轮末立即 end_span。
        """
        return _ChatSpan(self)

    def _begin_chat_span(self) -> str:
        """开启新一轮 chat span：推进 iteration 并重置本轮 tool 追踪。"""
        self._current_iteration = self._next_iteration
        self._next_iteration += 1
        span_id = trace_start_span(
            self.run_id, name="llm_chat", iteration=self._current_iteration, sequence=0
        )
        self._chat_span_id = span_id
        self._last_tool_span_id = None  # 新轮重置
        return span_id

    def _finish_chat_span(self, span_id, *, iteration: int, t0: float, resp) -> None:
        """写 chat span 终态（成功写 response delta；失败写 error span）。"""
        latency_ms = int((self._clock() - t0) * 1000)
        if resp is not None:
            tokens = resp.usage.total_tokens if resp.usage is not None else 0
            # 全轮累计：终态落库取累计值而非仅末轮
            self.total_tokens += tokens
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
            # LLM 调用异常 / 流中断：写 error span 但不遮掩原始异常
            logger.warning(
                f"[agent] LLM 调用异常（iteration={iteration}），写 error span"
            )
            trace_end_span(
                span_id,
                status="error",
                latency_ms=latency_ms,
                error="llm call raised before response",
            )

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

    def write_seed_row(self, seed_messages: Sequence[Message]) -> None:
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
        设为 ``iteration + 1``（保证后续 ``chat_span`` 从正确轮次开始推进，
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


class _ChatSpan(ChatSpan):
    """一轮 chat span 上下文（``TraceRecorder.chat_span()`` 产出）：聚合 + 落库。

    单聚合契约：``feed`` 是**唯一**聚合点，``__aexit__`` 做**唯一**一次
    ``StreamAggregator.finalize()``——调用方（loop）在块外读 ``resp`` 供控制流，
    与落库内容同源。

    落库时机（轮粒度，崩溃恢复依赖）：``__aenter__`` 立即 start_span（使流进行中
    提前执行的工具 span 与本轮 chat span 同 iteration）；``__aexit__`` 立即 end_span
    （成功写 response delta / model / tokens；异常写 error span，不吞异常）。
    """

    def __init__(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder
        self._span_id = ""
        self._iteration = 0
        self._t0 = 0.0
        self._aggregator = StreamAggregator()
        self._resp: ChatResponse | None = None

    async def __aenter__(self) -> _ChatSpan:
        recorder = self._recorder
        self._span_id = recorder._begin_chat_span()
        self._iteration = recorder._current_iteration
        self._t0 = recorder._clock()
        return self

    def feed(self, chunk: StreamChunk) -> None:
        self._aggregator.feed(chunk)

    @property
    def resp(self) -> ChatResponse | None:
        return self._resp

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        # 异常退出（流异常 / 取消）：不 finalize——半截流不得记成成功响应；
        # 复用 _finish_chat_span 的 error 分支落 span，原异常照常透传。
        resp = None if exc_type is not None else self._aggregator.finalize()
        self._resp = resp
        try:
            self._recorder._finish_chat_span(
                self._span_id, iteration=self._iteration, t0=self._t0, resp=resp
            )
        except Exception as e:
            # 观测层 best-effort：落库自身失败绝不遮蔽主流程（尤其原始流异常）
            logger.warning(f"[agent] chat span 落库失败（已忽略）: {e}")
        return False

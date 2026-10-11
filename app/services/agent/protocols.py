"""Agent 层协议与类型定义（纯协议，无实现）。

集中定义 agent 通用骨架与场景层 / 记录器之间的契约，消除循环依赖：

- ``loop.py``（通用骨架）与 ``recorder.py``（记录器）都从这里引用协议，
  避免 ``loop ↔ recorder`` 互相 import。
- ``scenario.py`` / ``runtime.py`` / ``registry.py`` 等场景与运行时模块统一引用。

新增 agent 层 Protocol 时，优先放在此文件。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from app.services.llm.models import StreamChunk, ToolResultBlock, ToolUseBlock

# ---------------------------------------------------------------------------
# 流式 LLM 调用
# ---------------------------------------------------------------------------

#: 流式 LLM 调用：``(messages, tools=, tool_choice=) -> AsyncIterator[StreamChunk]``。
StreamFn = Callable[..., AsyncIterator[StreamChunk]]

# ---------------------------------------------------------------------------
# 流式工具执行器
# ---------------------------------------------------------------------------


class StreamingExecutor(Protocol):
    """流式工具执行器协议。

    本地声明（不 import 具体 ``StreamingToolExecutor``），保持通用骨架对执行器实现的
    运行时解耦；返回值需实现 ``feed`` / ``finalize``。
    """

    def feed(self, chunk: StreamChunk) -> None: ...

    async def finalize(self) -> list[ToolResultBlock]: ...


# ---------------------------------------------------------------------------
# 记录器协议（工具 span / 预算）
# ---------------------------------------------------------------------------


class ToolSpanRecorder(Protocol):
    """工具执行 span 记录协议（``start_tool`` / ``end_tool`` 单一来源）。

    只需「为工具执行落 span」的组件（``tools.execute_batch`` /
    ``streaming_tool_executor``）依赖本协议即可，**不要求** budget 能力。
    实现者（如 ``recorder.TraceRecorder``）需持有可注入的时钟（构造参数注入 clock，
    默认真实时钟），在 ``start_tool`` 记 t0、``end_tool`` 记 t1；包裹层只负责在正确
    时机（await 前 start、完成后 end）调用，不持有时钟、不感知时间。
    """

    def start_tool(self, tool_use: ToolUseBlock, *, sequence: int) -> str | None:
        """记录工具执行开始；返回 span_id（或 None 表示无需记录）。"""

    def end_tool(
        self,
        span_id: str,
        *,
        result: ToolResultBlock | None = None,
        error: str = "",
    ) -> None:
        """记录工具执行结束。

        - ``result=None 且 error 非空``：执行异常（异常类型名）
        - ``result=ToolResultBlock``：正常结果（含 is_error 的占位块）
        - ``result=None 且 error=""``：terminal 捕获等非结果路径
        """


class BudgetRecorder(ToolSpanRecorder, Protocol):
    """通用循环要求的记录器协议：预算消息 + 工具 span（继承 ``ToolSpanRecorder``）。

    仅 ``loop.run`` 需要 ``record_budget``；``start_tool`` / ``end_tool`` 的签名
    **不在此重复声明**，统一由基类 :class:`ToolSpanRecorder` 提供（避免此前
    ``tools.ToolSpanRecorder`` 与本协议逐字重复的两份定义）。
    """

    def record_budget(self, budget_message: str) -> None: ...

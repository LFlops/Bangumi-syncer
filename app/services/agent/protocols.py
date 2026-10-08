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
# 预算与工具 span 记录器
# ---------------------------------------------------------------------------


class BudgetRecorder(Protocol):
    """预算与工具 span 记录器协议。

    本地声明（不 import 具体 ``TraceRecorder``），避免通用骨架与 recorder 循环依赖；
    签名与 ``recorder.TraceRecorder`` 一致。
    """

    def record_budget(self, budget_message: str) -> None: ...

    def start_tool(self, tool_use: ToolUseBlock, *, sequence: int) -> str | None: ...

    def end_tool(
        self,
        span_id: str,
        *,
        result: ToolResultBlock | None = None,
        error: str = "",
    ) -> None: ...

"""Agent 场景协议：通用运行时（runtime）与业务场景之间的契约。

具体的 Agent 场景（如 LLM 匹配增强）通过 :class:`ScenarioHooks` 提供全部
业务差异点；通用运行时（:mod:`app.services.agent.runtime`）只负责 run 编排
与恢复续跑状态机，并原样透传 ``ctx``（场景上下文，结构由场景自定义）。

设计约束：

- 运行时**不感知**任何场景概念（sync_record / bgm / 候选表 / 业务键等），
  这些全部封装在场景钩子与 ``ctx`` 内。
- 钩子中的依赖查询约定：场景侧钩子实现内部通过**场景模块全局名**引用依赖
  （而非闭包硬绑定），便于测试 patch 与后续替换。
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable

from app.services.agent.loop import ChatFn
from app.services.llm.models import Message
from app.services.llm.tools import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class ScenarioHooks:
    """场景钩子集合：run / continue_run 的全部业务差异点。"""

    #: 任务类型（观测/日志用，如 "match"）
    task_type: str
    #: 终止工具名（loop 的 ``tool_choice_terminal``；恢复分派据此识别终局）
    terminal_tool: str
    #: 注册场景工具（``registry`` + ``ctx`` → 工具定义列表）
    register_tools: Callable[[ToolRegistry, Any], list[ToolDefinition]]
    #: 构建种子消息（``ctx`` → ``[system, user, ...]``）
    build_seed: Callable[[Any], list[Message]]
    #: 构建默认 chat 函数（``thinking_level`` → ``ChatFn``；场景决定 job 归属与思考强度透传）
    build_chat_fn: Callable[[str], ChatFn]
    #: 解析思考强度（从场景集中配置读取；恢复续跑路径使用）
    resolve_thinking_level: Callable[[], str]
    #: 解析轮次预算（``thinking_level`` → ``max_iterations``；含场景配置覆盖）
    resolve_max_iterations: Callable[[str], int]
    #: 终局处理（校验/落库/通知），返回终态 status 字符串
    handle_terminal: Callable[..., Awaitable[str]]

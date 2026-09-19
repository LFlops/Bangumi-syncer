"""Agent 场景注册表（composition root）。

通用组件（如调度器）按 ``task_type`` 获取场景运行入口
(:class:`ScenarioRuntime`)；具体场景以「模块路径 + 工厂函数名」惰性登记，
避免通用层反向依赖场景模块（场景模块可自由 import 本注册表）。

新增场景：实现 ``ScenarioHooks`` 与 ``make_ctx``，并在
``_SCENARIO_PROVIDERS`` 登记 ``task_type -> (模块路径, 工厂函数名)``。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable

from app.services.agent import runtime as agent_runtime
from app.services.agent.scenario import ScenarioHooks


@dataclass(frozen=True)
class ScenarioRuntime:
    """场景运行入口：调度器等通用组件通过它执行 / 恢复场景 run。"""

    task_type: str
    hooks: ScenarioHooks
    #: (sync_record, bgm) -> ctx（场景上下文，runtime 原样透传）
    make_ctx: Callable[[dict, Any], Any]

    async def run(
        self,
        run_id: str,
        *,
        sync_record: dict,
        bgm: Any,
        thinking_level: str,
        chat_fn: Callable | None = None,
        notification_service: Any | None = None,
        span_recorder: Any | None = None,
    ) -> str:
        """执行一次场景任务（转发通用运行时）。"""
        return await agent_runtime.run(
            run_id,
            hooks=self.hooks,
            ctx=self.make_ctx(sync_record, bgm),
            thinking_level=thinking_level,
            chat_fn=chat_fn,
            notification_service=notification_service,
            span_recorder=span_recorder,
        )

    async def continue_run(
        self,
        run_id: str,
        *,
        sync_record: dict,
        bgm: Any,
        notification_service: Any | None = None,
    ) -> None:
        """恢复续跑（转发通用状态机）。"""
        await agent_runtime.continue_run(
            run_id,
            hooks=self.hooks,
            ctx=self.make_ctx(sync_record, bgm),
            notification_service=notification_service,
        )


# task_type → (场景模块路径, 工厂函数名)；工厂返回 ScenarioRuntime
_SCENARIO_PROVIDERS: dict[str, tuple[str, str]] = {
    "match": ("app.services.matching.llm_assist", "get_scenario_runtime"),
}


def get_scenario(task_type: str) -> ScenarioRuntime:
    """按 task_type 获取场景运行入口（惰性加载场景模块）。"""
    try:
        module_path, factory_name = _SCENARIO_PROVIDERS[task_type]
    except KeyError as e:
        raise KeyError(f"未注册的 Agent 场景: {task_type!r}") from e
    module = importlib.import_module(module_path)
    factory = getattr(module, factory_name)
    return factory()

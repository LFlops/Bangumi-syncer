"""Agent 场景注册表（纯机制）。

通用组件（如调度器）按 ``task_type`` 获取场景运行入口
(:class:`ScenarioRuntime`)。本模块只提供「登记 / 查询」机制，**不感知任何具体
场景**：场景运行入口由**应用装配层**（``app/services/scenarios.py``，
Composition Root）静态 import 场景工厂后调用 :func:`register_scenario` 注入。
如此既让场景工厂的静态分析可见（LSP 可解析调用），又保留「通用层不反向依赖
场景模块」的解耦（装配层位于应用层，不是 agent 通用层）。

新增场景：实现 ``ScenarioHooks`` 与 ``make_ctx``，提供 ``ScenarioRuntime``
工厂，并在装配层 ``wire_scenarios`` 静态 import 该工厂并登记 ``task_type``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.core.logging import logger
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


# task_type → 已装配的场景运行入口；由应用装配层 register_scenario 注入
_scenarios: dict[str, ScenarioRuntime] = {}


def register_scenario(task_type: str, runtime: ScenarioRuntime) -> None:
    """登记场景运行入口（覆盖语义，幂等）。

    重复登记属预期（应用启动与测试 conftest 可能各装配一次），只打
    debug/info，**不打 warning**。
    """
    if task_type in _scenarios:
        logger.debug(f"[registry] 覆盖已登记的场景: {task_type!r}")
    else:
        logger.info(f"[registry] 登记 Agent 场景: {task_type!r}")
    _scenarios[task_type] = runtime


def get_scenario(task_type: str) -> ScenarioRuntime:
    """按 task_type 获取已装配的场景运行入口。

    - 表为空：抛 ``KeyError``，提示需先装配（应用启动由
      ``scheduler_bootstrap.register_all()`` 触发，测试由 conftest 触发）
    - 未注册该 task_type：抛 ``KeyError``，消息含已登记列表
    """
    if not _scenarios:
        raise KeyError(
            f"场景未装配: {task_type!r}（应用启动由 "
            f"scheduler_bootstrap.register_all() 触发，测试由 conftest 触发）"
        )
    try:
        return _scenarios[task_type]
    except KeyError as e:
        registered = sorted(_scenarios)
        raise KeyError(
            f"未注册的 Agent 场景: {task_type!r}（已注册: {registered}）"
        ) from e

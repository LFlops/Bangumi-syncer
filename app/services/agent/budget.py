"""预算策略：IterationStrategy 注册表。

轻量策略模式：每个 task_type 注册一个「思考强度 → 轮次上限」的映射，
通用 Agent 骨架（loop.py）只消费计算好的 max_iterations，不关心映射来源。

优先级：
    1. config_override（[sync] llm_match_max_iterations 显式整体覆盖，最高）
    2. task_type 对应策略映射
    3. 默认兜底 {"off":1, "low":2, "medium":3, "high":5}
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# 默认兜底：未知 task_type 或未注册策略时使用
_DEFAULT_FALLBACK: dict[str, int] = {"off": 1, "low": 2, "medium": 3, "high": 5}
_DEFAULT_FALLBACK_LEVEL = 3

# task_type -> IterationStrategy 注册表
_ITERATION_STRATEGIES: dict[str, IterationStrategy] = {}


@runtime_checkable
class IterationStrategy(Protocol):
    """思考强度 → 轮次上限的映射协议（轻量策略模式，不过度工程化）。"""

    def max_iterations(self, thinking_level: str) -> int: ...


class MatchIterationStrategy:
    """match 场景预设（骨架默认注册）。"""

    PRESET = {"off": 1, "low": 2, "medium": 3, "high": 5}

    def max_iterations(self, level: str) -> int:
        return self.PRESET.get(level, 3)  # 未知 level 兜底 medium=3


def register_iteration_strategy(task_type: str, strategy: IterationStrategy) -> None:
    """注册（或覆盖）某 task_type 的迭代策略。"""
    _ITERATION_STRATEGIES[task_type] = strategy


def get_max_iterations(
    task_type: str,
    thinking_level: str,
    config_override: int | None = None,
) -> int:
    """按优先级计算循环轮次上限。

    优先级：config_override > task_type 策略映射 > 默认兜底。
    """
    if config_override is not None:
        return config_override

    strategy = _ITERATION_STRATEGIES.get(task_type)
    if strategy is not None:
        return strategy.max_iterations(thinking_level)

    return _DEFAULT_FALLBACK.get(thinking_level, _DEFAULT_FALLBACK_LEVEL)


def register_defaults() -> None:
    """注册骨架内置默认策略（幂等，供后续扩展或测试显式调用）。"""
    register_iteration_strategy("match", MatchIterationStrategy())


# 模块导入即注册 match 默认策略，确保通用骨架开箱可用
register_defaults()

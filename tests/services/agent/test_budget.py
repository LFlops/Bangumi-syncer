"""TDD 红阶段：IterationStrategy 预算策略注册表测试（场景 M27）。

覆盖：
- MatchIterationStrategy 预设 off=1/low=2/medium=3/high=5，未知 level 兜底 3
- register 后 get_max_iterations 返回对应策略；不同 task_type 互不影响；重复注册覆盖
- 优先级：config_override 最高 > 策略映射 > 默认兜底
"""

from __future__ import annotations

import pytest

from app.services.agent.budget import (
    _ITERATION_STRATEGIES,
    MatchIterationStrategy,
    get_max_iterations,
    register_iteration_strategy,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    """隔离全局注册表，保证测试之间互不影响。"""
    saved = dict(_ITERATION_STRATEGIES)
    yield
    _ITERATION_STRATEGIES.clear()
    _ITERATION_STRATEGIES.update(saved)


class _DummyStrategy:
    """用于验证注册表行为的简单策略。"""

    def __init__(self, preset: dict[str, int], fallback: int = 3) -> None:
        self.PRESET = preset
        self._fallback = fallback

    def max_iterations(self, level: str) -> int:
        return self.PRESET.get(level, self._fallback)


# --- MatchIterationStrategy 预设 ---


def test_match_iteration_strategy_off_returns_1():
    strategy = MatchIterationStrategy()
    assert strategy.max_iterations("off") == 1


def test_match_iteration_strategy_low_returns_2():
    strategy = MatchIterationStrategy()
    assert strategy.max_iterations("low") == 2


def test_match_iteration_strategy_medium_returns_3():
    strategy = MatchIterationStrategy()
    assert strategy.max_iterations("medium") == 3


def test_match_iteration_strategy_high_returns_5():
    strategy = MatchIterationStrategy()
    assert strategy.max_iterations("high") == 5


def test_match_iteration_strategy_unknown_level_returns_3():
    strategy = MatchIterationStrategy()
    assert strategy.max_iterations("bogus") == 3
    assert strategy.max_iterations("") == 3
    assert strategy.max_iterations("ULTRA") == 3


# --- 默认注册（match 应随模块导入即注册） ---


def test_match_strategy_registered_by_default():
    assert "match" in _ITERATION_STRATEGIES
    assert isinstance(_ITERATION_STRATEGIES["match"], MatchIterationStrategy)


def test_get_max_iterations_match_default_preset_levels():
    assert get_max_iterations("match", "off") == 1
    assert get_max_iterations("match", "low") == 2
    assert get_max_iterations("match", "medium") == 3
    assert get_max_iterations("match", "high") == 5


# --- 注册与 get_max_iterations 行为 ---


def test_get_max_iterations_registered_task_type_returns_strategy_value():
    # fallback=6 用于验证：未知 level 走「策略自身兜底」而非全局默认（同为 3 时无法区分）
    register_iteration_strategy(
        "diagnostic",
        _DummyStrategy({"off": 2, "low": 4, "medium": 6, "high": 10}, fallback=6),
    )
    assert get_max_iterations("diagnostic", "off") == 2
    assert get_max_iterations("diagnostic", "high") == 10
    assert get_max_iterations("diagnostic", "weird") == 6  # 策略自身兜底


def test_get_max_iterations_different_task_types_isolated():
    register_iteration_strategy(
        "diagnostic", _DummyStrategy({"off": 9, "medium": 9, "high": 9})
    )
    # match 始终保持自己的预设，不受 diagnostic 影响
    assert get_max_iterations("match", "off") == 1
    assert get_max_iterations("match", "high") == 5
    assert get_max_iterations("diagnostic", "off") == 9


def test_register_iteration_strategy_duplicate_overrides():
    first = _DummyStrategy({"off": 1, "medium": 1})
    second = _DummyStrategy({"off": 7, "medium": 7})
    register_iteration_strategy("custom", first)
    assert get_max_iterations("custom", "off") == 1
    register_iteration_strategy("custom", second)  # 重复注册覆盖
    assert get_max_iterations("custom", "off") == 7
    assert get_max_iterations("custom", "medium") == 7


# --- 默认兜底（未注册 task_type） ---


def test_get_max_iterations_unregistered_task_type_returns_default_fallback():
    assert get_max_iterations("unknown_task", "off") == 1
    assert get_max_iterations("unknown_task", "low") == 2
    assert get_max_iterations("unknown_task", "medium") == 3
    assert get_max_iterations("unknown_task", "high") == 5


def test_get_max_iterations_unregistered_task_type_unknown_level_returns_3():
    assert get_max_iterations("unknown_task", "bogus") == 3


# --- 优先级：config_override > 策略 > 默认兜底 ---


def test_get_max_iterations_config_override_highest_priority():
    register_iteration_strategy("diagnostic", _DummyStrategy({"off": 5, "high": 5}))
    # config_override 应覆盖已注册策略
    assert get_max_iterations("diagnostic", "off", config_override=42) == 42
    # config_override 也应覆盖未注册 task_type 的默认兜底
    assert get_max_iterations("unknown_task", "high", config_override=99) == 99


def test_get_max_iterations_config_override_none_falls_through_to_strategy():
    register_iteration_strategy("diagnostic", _DummyStrategy({"off": 5}))
    # 显式传 None 应与不带参数一致（走策略/默认）
    assert (
        get_max_iterations("diagnostic", "off", config_override=None)
        == get_max_iterations("diagnostic", "off")
        == 5
    )

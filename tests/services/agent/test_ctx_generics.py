"""场景 ctx 泛型化的结构守卫（``CtxT`` 贯穿通用层，但不泄漏场景私有类型）。

背景：``ctx`` 此前在通用层一律标注 ``Any``，导致场景签名错配在类型层面完全静默；
``handle_terminal`` 更因需表达「场景私有 ctx + 仅关键字参数」而退化为
``Callable[..., Awaitable[str]]``（零检查）。改用泛型后：

- ``ScenarioHooks[CtxT]`` / ``ScenarioRuntime[CtxT]`` 让**单链路内**的ctx 类型自洽；
- ``TerminalHandler(Protocol[CtxT])`` 用 ``__call__`` Protocol 逐字受检终局签名，
  且因ctx 是**类型变量**而非场景具体类型，通用层仍不引用 ``_MatchContext``
  ——解耦与收紧不再互斥。

局限（必须诚实）：泛型在运行时被擦除，``get_scenario(task_type)`` 动态查表仍返回
``Any``；本文件只能守住「注解形状」与「解耦不被破坏」，无法校验跨场景分发的正确性。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import typing
from pathlib import Path

from app.services.agent import registry as registry_mod, scenario as scenario_mod
from app.services.agent.registry import ScenarioRuntime
from app.services.agent.scenario import CtxT, ScenarioHooks, TerminalHandler
from app.services.matching import llm_assist

_AGENT_DIR = Path("app/services/agent")


# ---------------------------------------------------------------------------
# 1. ctx 泛型化：通用层签名不再用 Any 收ctx
# ---------------------------------------------------------------------------


def test_run_and_continue_run_ctx_annotated_with_typevar():
    """``ScenarioRuntime.run`` / ``continue_run`` 的 ctx 注解是 ``CtxT`` 而非 Any。"""
    for fn in (ScenarioRuntime.run, ScenarioRuntime.continue_run):
        hints = typing.get_type_hints(fn)
        assert hints["ctx"] is CtxT, (
            f"{fn.__name__} 的 ctx 应标注 CtxT（随场景泛型绑定），实际 {hints['ctx']!r}"
        )
        assert hints["ctx"] is not typing.Any


def test_agent_runtime_ctx_annotated_with_typevar():
    """``agent_runtime.run`` / ``continue_run`` 内部同样按 ``CtxT`` 收ctx。"""
    from app.services.agent import runtime as runtime_mod

    for fn in (runtime_mod.run, runtime_mod.continue_run):
        hints = typing.get_type_hints(fn)
        assert hints["ctx"] is CtxT, f"{fn.__name__} 的 ctx 应标注 CtxT"


def test_scenario_hooks_ctx_hooks_are_typed():
    """``register_tools`` 的 ctx 位置标注 ``CtxT``。"""
    hints = typing.get_type_hints(ScenarioHooks)
    annotation = hints["register_tools"]
    # Callable[[..., CtxT], ...] —— get_args 返回 (参数列表, 返回类型)
    params = typing.get_args(annotation)[0]
    assert CtxT in params, f"register_tools 应以 CtxT 标注 ctx，实际 {annotation!r}"


# ---------------------------------------------------------------------------
# 2b. seed 建模为「数据」而非「行为」（对应评审 runtime.py:81）
# ---------------------------------------------------------------------------


def test_ctx_typevar_bounded_to_run_inputs():
    """``CtxT`` 受 ``RunInputs`` 约束：通用层对 ctx 的**唯一**要求是 seed。"""
    from app.services.agent.scenario import RunInputs

    assert CtxT.__bound__ is RunInputs, (
        f"CtxT 应受 RunInputs 约束（否则通用层读 ctx.seed 无类型保障），"
        f"实际 bound={CtxT.__bound__!r}"
    )
    # RunInputs 只要求 seed 这一个通用能力，不得夹带业务字段
    hints = typing.get_type_hints(RunInputs)
    business_fields = set(hints) - {"seed"}
    assert business_fields == set(), (
        f"RunInputs 只应声明 seed（通用概念），实际还声明了 {business_fields}"
    )


def test_scenario_hooks_no_longer_exposes_build_seed():
    """``build_seed`` 已从 hooks 移除：seed 是数据，由 ``new_ctx`` 在初始化时产出。"""
    assert "build_seed" not in {f.name for f in dataclasses.fields(ScenarioHooks)}, (
        "seed 应归入 ctx（数据），不应留在 hooks（行为）"
    )


def test_runtime_reads_seed_from_ctx_not_from_hooks():
    """通用层从 ``ctx.seed`` 取 seed，不再回调场景派生（AST 级断言）。"""
    runtime_src = (_AGENT_DIR / "runtime.py").read_text(encoding="utf-8")
    assert "hooks.build_seed" not in runtime_src, "通用层不应再驱动场景派生 seed"
    assert "ctx.seed" in runtime_src, "run() 应直接取 ctx.seed"


def test_scene_ctx_carries_seed():
    """匹配场景 ctx 携带 seed（与 sync_record / bgm 同类的本次 run 数据）。"""
    fields = {f.name for f in dataclasses.fields(llm_assist._MatchContext)}
    assert {"sync_record", "bgm", "seed"} <= fields, (
        f"_MatchContext 应含 sync_record/bgm/seed，实际 {fields}"
    )


def test_new_ctx_returns_ctx_type():
    """``new_ctx`` 工厂返回 ``CtxT``（通用层只交付输入，收回场景 ctx）。"""
    hints = typing.get_type_hints(ScenarioRuntime)
    annotation = hints["new_ctx"]
    assert typing.get_args(annotation)[-1] is CtxT, (
        f"new_ctx 应返回 CtxT，实际 {annotation!r}"
    )


# ---------------------------------------------------------------------------
# 2. hooks / runtime 确为泛型；handle_terminal 升级为受检 Protocol
# ---------------------------------------------------------------------------


def test_hooks_and_runtime_are_generic():
    """``ScenarioHooks`` / ``ScenarioRuntime`` 继承 ``Generic``（可参数化）。"""
    from typing import Generic

    assert Generic in ScenarioHooks.__mro__
    assert Generic in ScenarioRuntime.__mro__
    # 可参数化且产出带参数形式的 _GenericAlias
    assert ScenarioHooks.__class_getitem__ is not None
    assert ScenarioRuntime.__class_getitem__ is not None


def test_handle_terminal_is_protocol_not_bare_callable():
    """``handle_terminal`` 从 ``Callable[..., ...]`` 升级为 ``TerminalHandler[CtxT]``。"""
    hints = typing.get_type_hints(ScenarioHooks)
    expected = TerminalHandler[CtxT]
    assert hints["handle_terminal"] == expected, (
        f"handle_terminal 应标注 TerminalHandler[CtxT]，实际 {hints['handle_terminal']!r}"
    )
    # 旧写法（零检查）不得复现
    assert typing.get_args(hints["handle_terminal"]) != (...,)


def test_terminal_handler_protocol_declares_call():
    """``TerminalHandler`` 是带 ``__call__`` 的 Protocol（可逐字受检签名）。"""
    assert getattr(TerminalHandler, "_is_protocol", False), (
        "TerminalHandler 应为 Protocol"
    )
    assert callable(TerminalHandler.__call__)


def test_scene_impl_matches_terminal_handler_signature():
    """匹配场景实现与 Protocol 逐字一致（参数名/顺序/仅关键字）。

    Protocol 受检的实价值：多出 ``dbm`` 这类多余实参会在此暴露。
    """
    proto = inspect.signature(TerminalHandler.__call__)
    impl = inspect.signature(llm_assist._match_handle_terminal)
    proto_names = [p for p in proto.parameters if p != "self"]
    impl_names = [p for p in impl.parameters if p != "self"]
    assert proto_names == impl_names, (
        f"终局处理签名必须与 Protocol 一致：协议 {proto_names} vs 实现 {impl_names}"
    )
    proto_kwonly = [
        n
        for n, p in proto.parameters.items()
        if n != "self" and p.kind == p.KEYWORD_ONLY
    ]
    impl_kwonly = [
        n
        for n, p in impl.parameters.items()
        if n != "self" and p.kind == p.KEYWORD_ONLY
    ]
    assert proto_kwonly == impl_kwonly == ["total_tokens", "notification_service"]


def test_scene_runtime_factory_declares_concrete_ctx():
    """场景工厂的返回注解绑定具体 ctx 类型（把类型信息带进通用层入口）。"""
    hints = typing.get_type_hints(llm_assist.get_scenario_runtime)
    assert hints["return"] == registry_mod.ScenarioRuntime[llm_assist._MatchContext]


# ---------------------------------------------------------------------------
# 3. 解耦不被破坏：通用层不得出现场景私有类型
# ---------------------------------------------------------------------------


def test_agent_layer_never_mentions_scene_private_ctx_type():
    """``app/services/agent/**`` 源码不得出现 ``_MatchContext`` 等场景私有类型。"""
    offenders: dict[str, list[str]] = {}
    for path in sorted(_AGENT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = [
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "_MatchContext"
        ]
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"agent 通用层不得引用场景私有 ctx 类型，实际 {offenders}"


def test_ctx_typevar_defined_in_scenario_contract_module():
    """``CtxT`` 定义在场景契约模块（protocols/scenario），供hooks 与 registry 共用。"""
    assert hasattr(scenario_mod, "CtxT")
    assert registry_mod.CtxT is scenario_mod.CtxT, "registry 应复用同一个 CtxT"

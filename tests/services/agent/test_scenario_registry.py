"""场景装配（Composition Root）与注册表纯机制化测试。

BDD 场景：

1. ``wire_scenarios()`` 后 ``get_scenario("match")`` 返回绑定匹配场景 hooks 的
   ``ScenarioRuntime``
2. 重复 ``wire_scenarios()`` 幂等（不抛异常、不打 warning，仍可获取）
4. 装配层必须**静态 import** 场景工厂（禁止 importlib / 字符串路径），注册表
   不得残留字符串表 ``_SCENARIO_PROVIDERS``
5. 未注册 task_type → ``KeyError``（消息含未知 task_type 与已注册列表）
6. 未装配（表为空）→ ``KeyError``（消息明确提示需先装配）
7. 装配层 / 注册表 / 场景模块在**干净子进程**中以任意顺序导入均不触发
   循环依赖（``subprocess`` 隔离验证 import 顺序无关）

说明：注册表 ``_scenarios`` 为进程级共享状态。测试中通过 ``monkeypatch`` 隔离，
不新增任何「仅测试用」的重置 API（monkeypatch 自动还原）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import scenarios as scenarios_module
from app.services.agent import registry as registry_module
from app.services.agent.registry import get_scenario
from app.services.matching import llm_assist


class TestWireScenarios:
    """场景 1/2：装配后可用 + 幂等。"""

    def test_wire_scenarios_get_match_returns_runtime_bound_to_match_hooks(self):
        """wire_scenarios() 后 get_scenario("match") 返回绑定 _MATCH_HOOKS 的运行入口。"""
        scenarios_module.wire_scenarios()

        runtime = get_scenario("match")

        assert runtime.task_type == "match"
        # _MATCH_HOOKS 为模块级单例：用 is 断言装配实例与场景工厂同源
        assert runtime.hooks is llm_assist._MATCH_HOOKS

    def test_wire_scenarios_called_twice_is_idempotent_and_logs_no_warning(self):
        """重复 wire_scenarios() 幂等：不抛异常、不打 warning、仍可获取。"""
        from app.core.logging import logger

        seen: list[tuple[str, str]] = []

        def _listener(line: str, level: str) -> None:
            seen.append((line, level))

        scenarios_module.wire_scenarios()
        logger.add_listener(_listener)
        try:
            scenarios_module.wire_scenarios()
        finally:
            logger.remove_listener(_listener)

        runtime = get_scenario("match")
        assert runtime.task_type == "match"
        assert runtime.hooks is llm_assist._MATCH_HOOKS

        warnings = [(line, level) for line, level in seen if level == "WARNING"]
        assert warnings == [], f"重复装配属预期，不应打 warning，实际 {warnings}"


class TestGetScenarioErrors:
    """场景 5/6：未注册 / 未装配的错误契约。"""

    def test_get_scenario_unknown_task_raises_keyerror_with_registered_list(self):
        """未注册 task_type → KeyError，消息含未知 task_type 与已注册列表。"""
        scenarios_module.wire_scenarios()

        with pytest.raises(KeyError) as exc_info:
            get_scenario("no_such_task")

        message = exc_info.value.args[0]
        assert "no_such_task" in message
        assert "match" in message, f"错误消息应含已注册列表，实际 {message!r}"

    def test_get_scenario_before_wiring_raises_keyerror_hinting_wiring(
        self, monkeypatch
    ):
        """未装配（表为空）→ KeyError，消息明确提示需先装配（monkeypatch 隔离）。"""
        monkeypatch.setattr(registry_module, "_scenarios", {})

        with pytest.raises(KeyError) as exc_info:
            get_scenario("match")

        message = exc_info.value.args[0]
        assert "未装配" in message, f"错误消息应提示未装配，实际 {message!r}"


class TestStaticWiringGuard:
    """场景 4：静态装配 / 纯机制化的 AST 守卫（防回归）。"""

    _REPO_ROOT = Path(__file__).resolve().parents[3]

    def _parse(self, relative_path: str) -> ast.AST:
        source = (self._REPO_ROOT / relative_path).read_text(encoding="utf-8")
        return ast.parse(source, filename=relative_path)

    def test_scenarios_module_static_import_of_factory_not_importlib(self):
        """装配层必须静态 import 场景工厂，禁止 importlib 动态加载。"""
        tree = self._parse("app/services/scenarios.py")

        importlib_hits: list[str] = []
        factory_imported = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "importlib" for alias in node.names):
                    importlib_hits.append(ast.unparse(node))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "importlib":
                    importlib_hits.append(ast.unparse(node))
                if node.module == "app.services.matching.llm_assist" and any(
                    alias.name == "get_scenario_runtime" for alias in node.names
                ):
                    factory_imported = True

        assert importlib_hits == [], (
            f"装配层禁止 importlib 动态加载，实际：{importlib_hits}"
        )
        assert factory_imported, "装配层必须静态 import get_scenario_runtime"

    def test_registry_module_has_no_importlib_or_provider_table(self):
        """注册表纯机制化：不得残留 importlib 与字符串提供者表。"""
        tree = self._parse("app/services/agent/registry.py")

        importlib_hits: list[str] = []
        provider_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "importlib" for alias in node.names):
                    importlib_hits.append(ast.unparse(node))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "importlib":
                    importlib_hits.append(ast.unparse(node))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == (
                        "_SCENARIO_PROVIDERS"
                    ):
                        provider_names.append(ast.unparse(node))

        assert importlib_hits == [], f"注册表不应依赖 importlib，实际：{importlib_hits}"
        assert provider_names == [], (
            f"注册表不应保留字符串提供者表，实际：{provider_names}"
        )


class TestImportOrderGuard:
    """场景 7：装配层 / 注册表 / 场景模块的导入顺序无关（无循环依赖）。

    在**干净子进程**中分别按不同顺序导入三模块：若存在循环导入或装配层被
    场景模块反向依赖，子进程会 ImportError 退出（returncode != 0）。
    ``cwd`` 设为仓库根，使 ``python -c`` 的 ``sys.path[0]`` 指向仓库；
    ``env`` 继承当前测试进程——conftest 已设 ``CONFIG_FILE`` 指向测试临时配置，
    子进程因此不会读写本地 ``config.ini``。
    """

    _REPO_ROOT = Path(__file__).resolve().parents[3]

    _IMPORT_ORDERS: dict[str, tuple[str, ...]] = {
        # A: 装配层优先（触发其静态 import 链）
        "composition_root_first": (
            "import app.services.scenarios",
            "import app.services.agent.registry",
            "import app.services.matching.llm_assist",
        ),
        # B: 注册表优先
        "registry_first": (
            "import app.services.agent.registry",
            "import app.services.matching.llm_assist",
            "import app.services.scenarios",
        ),
        # C: 场景模块优先
        "scenario_first": (
            "import app.services.matching.llm_assist",
            "import app.services.agent.registry",
        ),
    }

    @pytest.mark.parametrize("order_name", sorted(_IMPORT_ORDERS))
    def test_import_orders_no_cycle(self, order_name):
        """任一导入顺序均成功（无循环依赖），失败时输出子进程 stderr。"""
        code = "\n".join(self._IMPORT_ORDERS[order_name])

        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=self._REPO_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 0, (
            f"导入顺序 {order_name!r} 失败（returncode={completed.returncode}），"
            f"存在循环导入或反向依赖。\n"
            f"code:\n{code}\n"
            f"stderr:\n{completed.stderr}"
        )

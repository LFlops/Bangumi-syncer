"""agent 层协议结构守卫测试（protocols.py 唯一化）。

覆盖：
- **协议单一来源**：记录器协议只允许在 ``protocols.py`` 定义；``tools.py`` 不再本地
  定义 ``ToolSpanRecorder``（此前与 ``protocols.BudgetRecorder`` 的
  ``start_tool``/``end_tool`` 逐字重复）。
- **协议复用而非复制**：``BudgetRecorder`` 继承 ``ToolSpanRecorder``，不重复声明
  ``start_tool``/``end_tool``（消除两份同签名定义）。
- **依赖方向**：``tools.py`` 从 ``protocols`` 取协议；``recorder.py`` 不再 import
  ``tools``（切断 recorder → tools 的反向依赖边）；执行器依赖 ``protocols`` 而非 ``loop``。
- **执行器显式实现协议**：``StreamingToolExecutor`` 显式继承
  ``protocols.StreamingExecutor``（nominal 而非纯鸭子类型），使协议变更由类型检查与
  本守卫同时兜住，而非仅靠结构约定。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.agent.protocols import (
    BudgetRecorder,
    StreamingExecutor,
    ToolSpanRecorder,
)
from app.services.agent.streaming_tool_executor import StreamingToolExecutor

_AGENT_DIR = Path("app/services/agent")


def _parse(name: str) -> ast.AST:
    """解析 agent 层源文件 AST（守卫用）。"""
    return ast.parse((_AGENT_DIR / name).read_text(encoding="utf-8"), filename=name)


def _imported_modules(name: str) -> set[str]:
    """收集文件内 ``from X import ...`` 的模块名（不含相对导入）。"""
    modules: set[str] = set()
    for node in ast.walk(_parse(name)):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
    return modules


def _defined_class_names(name: str) -> set[str]:
    """收集文件内定义的类名。"""
    return {
        node.name for node in ast.walk(_parse(name)) if isinstance(node, ast.ClassDef)
    }


# ---------------------------------------------------------------------------
# 1. 协议单一来源：记录器协议只允许在 protocols.py 定义
# ---------------------------------------------------------------------------


def test_tools_defines_no_recorder_protocol():
    """tools.py 不应本地定义任何记录器协议（避免与 protocols 重复定义）。"""
    recorder_classes = {
        cls for cls in _defined_class_names("tools.py") if "Recorder" in cls
    }
    assert recorder_classes == set(), (
        f"记录器协议应由 protocols.py 唯一持有，tools.py 仍定义: {recorder_classes}"
    )


def test_recorder_protocol_defined_in_protocols_module():
    """ToolSpanRecorder / BudgetRecorder 均在 protocols.py 定义。"""
    defined = _defined_class_names("protocols.py")
    assert {"ToolSpanRecorder", "BudgetRecorder"} <= defined


def test_budget_recorder_covers_budget_and_tool_span():
    """BudgetRecorder 同时具备 record_budget/start_tool/end_tool 三种能力。"""
    assert {"record_budget", "start_tool", "end_tool"} <= set(dir(BudgetRecorder))


def test_tool_span_recorder_only_requires_tool_span():
    """ToolSpanRecorder 只要求 start_tool/end_tool（不强制 budget 能力）。"""
    assert "record_budget" not in dir(ToolSpanRecorder)


# ---------------------------------------------------------------------------
# 2. 协议复用而非复制：BudgetRecorder 继承 ToolSpanRecorder
# ---------------------------------------------------------------------------


def test_budget_recorder_inherits_tool_span_recorder():
    """BudgetRecorder 继承 ToolSpanRecorder（工具 span 能力单一来源）。

    以 ``__mro__`` 判定而非 ``issubclass``：协议未加 ``@runtime_checkable``
    （全项目无 isinstance/issubclass 用例，无需为此付运行时检查代价）。
    """
    assert ToolSpanRecorder in BudgetRecorder.__mro__
    assert "start_tool" not in BudgetRecorder.__dict__


def test_budget_recorder_does_not_duplicate_tool_span_methods():
    """BudgetRecorder 不重复声明 start_tool/end_tool（签名只在基类一处）。"""
    assert "start_tool" not in BudgetRecorder.__dict__
    assert "end_tool" not in BudgetRecorder.__dict__
    assert "record_budget" in BudgetRecorder.__dict__


# ---------------------------------------------------------------------------
# 3. 依赖方向：protocols 是唯一协议出口
# ---------------------------------------------------------------------------


def test_tools_imports_protocols():
    """tools.py 从 protocols 取记录器协议（不本地重复定义）。"""
    assert "app.services.agent.protocols" in _imported_modules("tools.py")


def test_recorder_does_not_import_tools():
    """recorder.py 不应 import tools（切断 recorder → tools 依赖边）。"""
    assert "app.services.agent.tools" not in _imported_modules("recorder.py")


def test_streaming_executor_depends_on_protocols_not_loop():
    """执行器依赖 protocols（而非 loop），避免骨架反向依赖实现层。"""
    modules = _imported_modules("streaming_tool_executor.py")
    assert "app.services.agent.protocols" in modules
    assert "app.services.agent.loop" not in modules


# ---------------------------------------------------------------------------
# 4. 执行器显式继承 StreamingExecutor
# ---------------------------------------------------------------------------


def test_streaming_tool_executor_explicitly_implements_protocol():
    """StreamingToolExecutor 显式继承 StreamingExecutor（非纯鸭子类型）。"""
    assert StreamingExecutor in StreamingToolExecutor.__mro__


def test_streaming_tool_executor_declares_protocol_as_base_class():
    """以 AST 校验协议确实写在基类位置（防止仅靠结构满足蒙混）。"""
    for node in ast.walk(_parse("streaming_tool_executor.py")):
        if isinstance(node, ast.ClassDef) and node.name == "StreamingToolExecutor":
            bases = [ast.unparse(b) for b in node.bases]
            assert bases == ["StreamingExecutor"], (
                f"StreamingToolExecutor 基类应为 [StreamingExecutor]，实际 {bases}"
            )
            return
    raise AssertionError("未找到 StreamingToolExecutor 类定义")

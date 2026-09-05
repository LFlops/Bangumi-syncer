"""通用工具注册表与执行器。

提供：
- ``ToolDefinition``：工具元信息（含 access 枚举与 readonly 推导）
- ``ToolRegistry``：注册 / 执行 / JSON Schema 轻量校验 / 分段并行批量执行
- 模块级单例 ``get_tool_registry()`` / ``reset_tool_registry()``

零新增依赖：JSON Schema 校验使用手写轻量实现（必填字段、类型、pattern、maxLength），
不引入 ``jsonschema``。
"""

import asyncio
import logging
import re
from collections import UserDict
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from app.services.llm.models import ToolResultBlock, ToolUseBlock

logger = logging.getLogger(__name__)

ToolAccess = Literal["read", "write", "terminal"]

# JSON Schema type → Python 类型（用于轻量校验）
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class ToolError(Exception):
    """工具执行 / 校验失败。"""


@dataclass
class TerminalCapture:
    """终止性工具（access=terminal）的捕获结果：仅记录参数，不执行 handler。

    由循环收到即 break（终止工具优先）。
    """

    name: str
    args: dict


@dataclass
class ToolDefinition:
    """单一工具定义。

    - ``access``：read（只读）/ write（写，执行前审计）/ terminal（终止性，仅捕获参数）
    - ``readonly``：仅代码层面属性，决定循环的分段并行策略；不序列化进 tools schema
      默认由 ``access`` 推导（read→True；write/terminal→False），注册时可显式覆盖。
    - ``parameters``：OpenAI function calling 标准的 JSON Schema
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Any]
    access: ToolAccess
    readonly: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.readonly is None:
            self.readonly = self.access == "read"

    def to_schema(self) -> dict:
        """供 provider ``tools`` 参数的 schema：仅 name/description/parameters。

        readonly / access 不序列化（LLM 不可见，天然防诱导）。
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class BatchResults(UserDict):
    """批量执行结果：``ordered`` 独立槽位 + 兼容的 ``dict[tool_use_id, result]`` 视图。

    - ``ordered``：``[(tool_use_id, result), ...]``，与传入 ``tool_calls`` 一一对应
      （长度与顺序一致），消费方（loop.py）按槽位回填 tool_result，保证每条 tool_use
      都有且仅有一条对应结果
    - dict 视图：同一 ``tool_use_id`` 重复出现时保留**首个**结果（真实执行结果不会被
      后续 duplicate 错误块覆盖）。该 first-wins 契约在**任何赋值路径**下成立：构造、
      ``[]`` 赋值、``update`` 均触发 ``__setitem__``；对已存在 key 的再次赋值被拒绝并打
      warning 日志（避免覆盖契约被静默破坏的悬垂分支）。
    """

    def __init__(self, ordered: Optional[list[tuple[str, Any]]] = None) -> None:
        self.ordered: list[tuple[str, Any]] = list(ordered or [])
        super().__init__()
        for tool_use_id, result in self.ordered:
            self.setdefault(tool_use_id, result)

    def __setitem__(self, key: str, value: Any) -> None:
        # first-wins：已存在的 tool_use_id 拒绝覆盖，并打 warning 暴露该非预期路径
        if key in self:
            logger.warning(
                "BatchResults 试图覆盖已存在的 tool_use_id=%r，保留首个结果", key
            )
            return
        super().__setitem__(key, value)


class ToolRegistry:
    """工具注册表与执行器。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    # -- 注册 / 查询 ---------------------------------------------------------

    def register(self, defn: ToolDefinition, quiet: bool = False) -> None:
        """注册工具；重复注册**始终覆盖**（后注册的 handler 生效）。

        ``quiet=True``：调用方明确以覆盖为语义（如场景层每次 run 重新绑定
        handler 闭包），重复注册降为 debug 日志，不刷 warning。
        """
        if defn.name in self._tools:
            if quiet:
                logger.debug("工具 %r 重新注册（覆盖旧定义）", defn.name)
            else:
                logger.warning("工具 %r 重复注册，已覆盖旧定义", defn.name)
        self._tools[defn.name] = defn

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def is_readonly(self, name: str) -> bool:
        """工具是否可并行执行（连续 readonly 段）。"""
        defn = self._tools.get(name)
        return defn is not None and bool(defn.readonly)

    # -- 校验 ---------------------------------------------------------------

    def _validate(self, defn: ToolDefinition, args: dict) -> None:
        """轻量 JSON Schema 校验；失败抛 ToolError。

        支持：required 字段必填、properties.type 类型检查、pattern 正则、
        maxLength 长度上限。
        """
        schema = defn.parameters or {}
        props: dict = schema.get("properties", {})
        required: list = schema.get("required", [])

        for field in required:
            if field not in args:
                raise ToolError(f"缺少必填参数: {field}")

        for key, val in args.items():
            prop = props.get(key)
            if prop is None:
                continue
            ptype = prop.get("type")
            if ptype:
                self._check_type(key, val, ptype)
            pattern = prop.get("pattern")
            if pattern is not None and isinstance(val, str):
                if not re.fullmatch(pattern, val):
                    raise ToolError(f"参数 {key} 不匹配 pattern {pattern!r}")
            max_length = prop.get("maxLength")
            if max_length is not None and isinstance(val, str):
                if len(val) > max_length:
                    raise ToolError(f"参数 {key} 超过 maxLength {max_length}")

    @staticmethod
    def _check_type(key: str, val: Any, ptype: str) -> None:
        expected = _TYPE_MAP.get(ptype)
        if expected is None:
            return
        # bool 是 int 子类的特殊情况：integer/number 不应接受 bool
        if ptype in ("integer", "number") and isinstance(val, bool):
            raise ToolError(f"参数 {key} 应为 {ptype}，实际为 bool")
        if not isinstance(val, expected):
            raise ToolError(f"参数 {key} 应为 {ptype}，实际为 {type(val).__name__}")

    # -- 单条执行 -----------------------------------------------------------

    async def execute(self, name: str, args: dict, timeout: float = 30) -> Any:
        """执行单条工具。

        - 未注册 → 抛 ToolError
        - JSON Schema 校验失败 → 抛 ToolError（不调 handler）
        - write 级：执行前记录审计日志
        - terminal 级：不执行 handler，返回 ``TerminalCapture``
        - read 级：以 ``asyncio.wait_for`` 包裹 handler（默认 30s 超时，超时抛 ToolError）
        """
        defn = self._tools.get(name)
        if defn is None:
            raise ToolError(f"未注册的工具: {name}")

        self._validate(defn, args)

        if defn.access == "terminal":
            return TerminalCapture(name=defn.name, args=args)

        if defn.access == "write":
            logger.info("审计日志：执行写工具 %r args=%s", defn.name, args)

        try:
            return await self._call_handler(defn, args, timeout)
        except asyncio.TimeoutError as exc:
            raise ToolError(f"工具 {name} 执行超时（>{timeout}s）") from exc

    @staticmethod
    async def _call_handler(defn: ToolDefinition, args: dict, timeout: float) -> Any:
        handler = defn.handler
        if asyncio.iscoroutinefunction(handler):
            return await asyncio.wait_for(handler(args), timeout=timeout)
        # 同步 handler：丢到线程池并以 wait_for 限定超时
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, handler, args), timeout=timeout
        )

    # -- 批量分段并行执行 ---------------------------------------------------

    async def execute_batch(self, tool_calls: list[ToolUseBlock]) -> "BatchResults":
        """分段并行批量执行。

        - 按原始顺序扫描：连续 readonly 段 ``asyncio.gather`` 并行（保序返回）
        - 非 readonly（write / terminal）单独串行，相对顺序保持
        - 返回 ``BatchResults``：``ordered`` 与 ``tool_calls`` **一一对应的独立槽位**，
          同时兼容 ``{tool_use_id: result}`` 的 dict 视图（按 id 取首个结果）：
          - read/write 成功 → ``ToolResultBlock(tool_use_id, content, is_error=False)``
          - 任意异常（校验/超时/handler 异常/未注册）→ ``ToolResultBlock(is_error=True)``
          - terminal → ``TerminalCapture``（不执行 handler，供循环 break）
          - 重复 ``tool_use_id``：仅执行第一个，**首个槽位保留真实结果**，第二及以后的
            槽位为 ``ToolResultBlock(is_error=True, content="duplicate tool_use_id")``
            （独立槽位避免 dup 错误块覆盖首个真实结果）
        """
        n = len(tool_calls)
        slots: list[Any] = [None] * n
        seen_ids: set[str] = set()
        i = 0
        while i < n:
            tc = tool_calls[i]
            # 重复 tool_use_id：不执行 handler，本槽位回填错误块
            if tc.id in seen_ids:
                slots[i] = self._duplicate_block(tc.id)
                i += 1
                continue
            if self.is_readonly(tc.name):
                # 收集连续 readonly 段，段内同样跳过重复 id（不进入 gather）
                j = i
                seg: list[tuple[int, ToolUseBlock]] = []
                while j < n and self.is_readonly(tool_calls[j].name):
                    cur = tool_calls[j]
                    if cur.id in seen_ids:
                        slots[j] = self._duplicate_block(cur.id)
                    else:
                        seen_ids.add(cur.id)
                        seg.append((j, cur))
                    j += 1
                seg_results = await asyncio.gather(*[self._exec_one(t) for _, t in seg])
                for (idx, _), r in zip(seg, seg_results):
                    slots[idx] = r
                i = j
            else:
                seen_ids.add(tc.id)
                slots[i] = await self._exec_one(tc)
                i += 1
        return BatchResults([(tc.id, slots[idx]) for idx, tc in enumerate(tool_calls)])

    @staticmethod
    def _duplicate_block(tool_use_id: str) -> ToolResultBlock:
        """重复 tool_use_id 的占位错误块（不执行 handler）。"""
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content="duplicate tool_use_id",
            is_error=True,
        )

    async def _exec_one(self, tc: ToolUseBlock) -> Any:
        """执行单条并统一包装为 ToolResultBlock / TerminalCapture。"""
        try:
            result = await self.execute(tc.name, tc.input)
        except Exception as exc:  # ToolError / handler 异常 / 超时 统一转为错误块
            return ToolResultBlock(
                tool_use_id=tc.id,
                content=f"工具执行失败: {type(exc).__name__}",
                is_error=True,
            )
        if isinstance(result, TerminalCapture):
            return result
        return ToolResultBlock(tool_use_id=tc.id, content=str(result), is_error=False)


# ---------------------------------------------------------------------------
# 模块级单例（参照 mapping_service 的 Injectable 模式简化版）
# ---------------------------------------------------------------------------

_tool_registry: ToolRegistry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    """返回模块级工具注册表单例。"""
    return _tool_registry


def reset_tool_registry() -> None:
    """重置模块级单例（测试隔离）。"""
    global _tool_registry
    _tool_registry = ToolRegistry()

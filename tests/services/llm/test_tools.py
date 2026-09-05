"""工具注册表与执行器单元测试。

覆盖：
- ToolDefinition 的 readonly 推导与 to_schema 序列化
- ToolRegistry.register / get / execute 的注册、审计、terminal 捕获、超时、JSON Schema 校验
- execute_batch 的分段并行（连续 readonly 段 gather 并行、非只读串行、保序）与异常统一包装
"""

import asyncio
import logging

import pytest

from app.services.llm.models import ToolResultBlock, ToolUseBlock
from app.services.llm.tools import (
    TerminalCapture,
    ToolDefinition,
    ToolError,
    ToolRegistry,
    get_tool_registry,
    reset_tool_registry,
)


def _noop_handler(args):
    return "ok"


async def _async_noop(args):
    return "ok"


# ---------------------------------------------------------------------------
# ToolDefinition：readonly 推导
# ---------------------------------------------------------------------------


def test_tool_definition_readonly_derives_true_for_read_access():
    d = ToolDefinition(
        name="t", description="d", parameters={}, handler=_noop_handler, access="read"
    )
    assert d.readonly is True


def test_tool_definition_readonly_derives_false_for_write_access():
    d = ToolDefinition(
        name="t", description="d", parameters={}, handler=_noop_handler, access="write"
    )
    assert d.readonly is False


def test_tool_definition_readonly_derives_false_for_terminal_access():
    d = ToolDefinition(
        name="t",
        description="d",
        parameters={},
        handler=_noop_handler,
        access="terminal",
    )
    assert d.readonly is False


def test_tool_definition_readonly_explicit_override():
    # 显式覆盖：read 但指定 readonly=False（罕见"读但需串行"场景）
    d = ToolDefinition(
        name="t",
        description="d",
        parameters={},
        handler=_noop_handler,
        access="read",
        readonly=False,
    )
    assert d.readonly is False
    # 显式覆盖 write 为 True
    d2 = ToolDefinition(
        name="t",
        description="d",
        parameters={},
        handler=_noop_handler,
        access="write",
        readonly=True,
    )
    assert d2.readonly is True


# ---------------------------------------------------------------------------
# ToolDefinition：to_schema 不序列化 readonly / access
# ---------------------------------------------------------------------------


def test_tool_definition_to_schema_excludes_readonly_and_access():
    d = ToolDefinition(
        name="search",
        description="search bangumi",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 200}},
            "required": ["title"],
        },
        handler=_noop_handler,
        access="read",
    )
    schema = d.to_schema()
    assert set(schema.keys()) == {"name", "description", "parameters"}
    assert schema["name"] == "search"
    assert schema["description"] == "search bangumi"
    assert schema["parameters"]["properties"]["title"]["maxLength"] == 200
    assert "readonly" not in schema
    assert "access" not in schema


# ---------------------------------------------------------------------------
# ToolRegistry：register / get
# ---------------------------------------------------------------------------


def test_register_duplicate_warns_and_overwrites(caplog):
    reg = ToolRegistry()
    caplog.set_level(logging.WARNING)
    first = ToolDefinition(
        name="dup", description="a", parameters={}, handler=_noop_handler, access="read"
    )
    second = ToolDefinition(
        name="dup", description="b", parameters={}, handler=_noop_handler, access="read"
    )
    reg.register(first)
    reg.register(second)
    # 告警覆盖
    assert any("dup" in r.message and "覆盖" in r.message for r in caplog.records)
    # 取回的是后者（覆盖生效）
    assert reg.get("dup").description == "b"
    assert len(reg._tools) == 1


def test_get_unregistered_returns_none():
    reg = ToolRegistry()
    assert reg.get("nope") is None


# ---------------------------------------------------------------------------
# ToolRegistry：execute 未注册抛错
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_unregistered_raises_tool_error():
    reg = ToolRegistry()
    with pytest.raises(ToolError):
        await reg.execute("ghost", {})


# ---------------------------------------------------------------------------
# JSON Schema 校验：缺必填 / pattern / maxLength
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_missing_required_does_not_call_handler():
    called = []
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }

    def handler(args):
        called.append(1)
        return "ran"

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="need",
            description="d",
            parameters=schema,
            handler=handler,
            access="read",
        )
    )
    with pytest.raises(ToolError):
        await reg.execute("need", {})
    assert called == []


@pytest.mark.asyncio
async def test_execute_pattern_mismatch_does_not_call_handler():
    called = []
    schema = {
        "type": "object",
        "properties": {"subject_id": {"type": "string", "pattern": r"^\d+$"}},
        "required": ["subject_id"],
    }

    def handler(args):
        called.append(1)
        return "ran"

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="chk",
            description="d",
            parameters=schema,
            handler=handler,
            access="read",
        )
    )
    with pytest.raises(ToolError):
        await reg.execute("chk", {"subject_id": "abc"})
    assert called == []


@pytest.mark.asyncio
async def test_execute_maxlength_exceeded_does_not_call_handler():
    called = []
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string", "maxLength": 5}},
        "required": ["title"],
    }

    def handler(args):
        called.append(1)
        return "ran"

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="len",
            description="d",
            parameters=schema,
            handler=handler,
            access="read",
        )
    )
    with pytest.raises(ToolError):
        await reg.execute("len", {"title": "toolong"})
    assert called == []


@pytest.mark.asyncio
async def test_execute_valid_args_calls_handler_with_args():
    received = {}

    def handler(args):
        received.update(args)
        return "value"

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="ok",
            description="d",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            handler=handler,
            access="read",
        )
    )
    result = await reg.execute("ok", {"x": "hello"})
    assert result == "value"
    assert received == {"x": "hello"}


# ---------------------------------------------------------------------------
# write 级审计日志
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_access_audit_log_before_execution(caplog):
    caplog.set_level(logging.INFO)
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="write_tool",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=_noop_handler,
            access="write",
        )
    )
    await reg.execute("write_tool", {})
    assert any(
        r.levelno == logging.INFO and "write_tool" in r.message and "审计" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# terminal 级不执行 handler，仅捕获参数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_access_does_not_run_handler_returns_capture():
    called = []
    reg = ToolRegistry()

    def handler(args):
        called.append(1)
        return "never"

    reg.register(
        ToolDefinition(
            name="submit",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            access="terminal",
        )
    )
    result = await reg.execute("submit", {"subject_id": "123"})
    assert isinstance(result, TerminalCapture)
    assert result.name == "submit"
    assert result.args == {"subject_id": "123"}
    assert called == []


# ---------------------------------------------------------------------------
# 超时：read 级 handler 超时抛 ToolError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_handler_timeout_raises_tool_error():
    reg = ToolRegistry()

    async def slow(args):
        await asyncio.sleep(0.1)
        return "late"

    reg.register(
        ToolDefinition(
            name="slow",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=slow,
            access="read",
        )
    )
    with pytest.raises(ToolError):
        await reg.execute("slow", {}, timeout=0.01)


# ---------------------------------------------------------------------------
# execute_batch：分段并行（连续 readonly 段 gather 并行）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_readonly_segment_runs_in_parallel():
    reg = ToolRegistry()
    starts = {}
    ends = {}

    async def make_handler(key):
        async def handler(args):
            starts[key] = asyncio.get_event_loop().time()
            await asyncio.sleep(0.05)
            ends[key] = asyncio.get_event_loop().time()
            return key

        return handler

    for k in ("a", "b", "c"):
        reg.register(
            ToolDefinition(
                name=k,
                description="d",
                parameters={"type": "object", "properties": {}},
                handler=await make_handler(k),
                access="read",
            )
        )
    calls = [ToolUseBlock(id=k, name=k, input={}) for k in ("a", "b", "c")]
    t0 = asyncio.get_event_loop().time()
    results = await reg.execute_batch(calls)
    elapsed = asyncio.get_event_loop().time() - t0
    # 三条 read 并行，总耗时约等于单条（< 3 倍）
    assert elapsed < 0.13
    # 起始时间彼此接近（并发启动）
    min_start = min(starts.values())
    max_start = max(starts.values())
    assert max_start - min_start < 0.04
    # 返回结构：每条都是 ToolResultBlock，保序
    assert list(results.keys()) == ["a", "b", "c"]
    for k in ("a", "b", "c"):
        blk = results[k]
        assert isinstance(blk, ToolResultBlock)
        assert blk.tool_use_id == k
        assert blk.is_error is False
        assert blk.content == k


# ---------------------------------------------------------------------------
# execute_batch：顺序（read, read, write, read）——非只读串行且相对顺序保持
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_preserves_order_with_write_in_middle():
    reg = ToolRegistry()
    order = []

    async def make_handler(key):
        async def handler(args):
            await asyncio.sleep(0.01)
            order.append(key)
            return key

        return handler

    for k in ("ra", "rb", "wc", "rd"):
        access = "write" if k == "wc" else "read"
        reg.register(
            ToolDefinition(
                name=k,
                description="d",
                parameters={"type": "object", "properties": {}},
                handler=await make_handler(k),
                access=access,  # type: ignore[arg-type]
            )
        )
    calls = [ToolUseBlock(id=k, name=k, input={}) for k in ("ra", "rb", "wc", "rd")]
    results = await reg.execute_batch(calls)
    # 结果 dict 顺序与输入一致
    assert list(results.keys()) == ["ra", "rb", "wc", "rd"]
    # 执行顺序：两条 read 并行（先后不确定，但都在 wc 之前），wc 在 rd 之前
    assert order.index("wc") == 2
    assert order.index("rd") == 3
    assert set(order[:2]) == {"ra", "rb"}


# ---------------------------------------------------------------------------
# execute_batch：工具异常 → is_error=True 的 ToolResultBlock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_handler_exception_wrapped_as_error_block():
    reg = ToolRegistry()

    async def boom(args):
        raise ValueError("kaboom")

    reg.register(
        ToolDefinition(
            name="boom",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=boom,
            access="read",
        )
    )
    calls = [ToolUseBlock(id="x1", name="boom", input={})]
    results = await reg.execute_batch(calls)
    blk = results["x1"]
    assert isinstance(blk, ToolResultBlock)
    assert blk.is_error is True
    assert "ValueError" in blk.content


# ---------------------------------------------------------------------------
# execute_batch：未注册工具 → is_error=True 的 ToolResultBlock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_unknown_tool_wrapped_as_error_block():
    reg = ToolRegistry()
    calls = [ToolUseBlock(id="u1", name="ghost", input={})]
    results = await reg.execute_batch(calls)
    blk = results["u1"]
    assert isinstance(blk, ToolResultBlock)
    assert blk.is_error is True


# ---------------------------------------------------------------------------
# execute_batch：重复 tool_use_id
#   重复 id 仅执行第一个；每个 tool_call 有独立结果槽位（ordered）：
#   首个保留真实结果，第二及以后为 duplicate 错误块（不覆盖首个）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_duplicate_tool_use_id_executes_only_first():
    reg = ToolRegistry()
    called = []

    def handler(args):
        called.append(1)
        return "ran"

    reg.register(
        ToolDefinition(
            name="dup",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            access="read",
        )
    )
    calls = [
        ToolUseBlock(id="same", name="dup", input={}),
        ToolUseBlock(id="same", name="dup", input={}),
    ]
    results = await reg.execute_batch(calls)
    # handler 仅被调用一次（第一个执行，第二个不执行）
    assert called == [1]
    # G2：独立槽位与 tool_calls 一一对应（顺序一致）
    assert len(results.ordered) == len(calls)
    first_id, first = results.ordered[0]
    second_id, second = results.ordered[1]
    assert first_id == second_id == "same"
    # 首个槽位保留真实执行结果（不被 dup 错误块覆盖）
    assert isinstance(first, ToolResultBlock)
    assert first.is_error is False
    assert first.content == "ran"
    # 第二个槽位为 duplicate 错误块
    assert isinstance(second, ToolResultBlock)
    assert second.is_error is True
    assert second.content == "duplicate tool_use_id"
    # 兼容的 dict 视图按 id 取值 → 首个真实结果
    assert results["same"] is first


@pytest.mark.asyncio
async def test_execute_batch_duplicate_write_tool_keeps_first_result():
    """非 readonly（write）串行分支同样保留首个真实结果，第二个为 dup 错误块。"""
    reg = ToolRegistry()
    called = []

    def handler(args):
        called.append(args.get("k"))
        return "written"

    reg.register(
        ToolDefinition(
            name="w",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            access="write",
        )
    )
    calls = [
        ToolUseBlock(id="w1", name="w", input={"k": "first"}),
        ToolUseBlock(id="w1", name="w", input={"k": "second"}),
    ]
    results = await reg.execute_batch(calls)
    # 仅首个执行（写工具不重复副作用）
    assert called == ["first"]
    assert [i for i, _ in results.ordered] == ["w1", "w1"]
    assert results.ordered[0][1].is_error is False
    assert results.ordered[0][1].content == "written"
    assert results.ordered[1][1].is_error is True
    assert results.ordered[1][1].content == "duplicate tool_use_id"
    assert results["w1"].content == "written"


@pytest.mark.asyncio
async def test_execute_batch_duplicate_does_not_affect_unique_ids():
    reg = ToolRegistry()
    called = []

    def handler(args):
        called.append(args.get("k"))
        return "ran"

    reg.register(
        ToolDefinition(
            name="t",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            access="read",
        )
    )
    calls = [
        ToolUseBlock(id="a", name="t", input={"k": "a"}),  # 唯一，正常
        ToolUseBlock(id="dup", name="t", input={"k": "dup"}),  # 重复对的首个，执行
        ToolUseBlock(id="dup", name="t", input={"k": "dup2"}),  # 重复，不执行
        ToolUseBlock(id="b", name="t", input={"k": "b"}),  # 唯一，正常
    ]
    results = await reg.execute_batch(calls)
    # 唯一 id 各自执行一次；重复的那个不执行 → 共 3 次
    assert called == ["a", "dup", "b"]
    # 唯一 id 结果不受影响
    assert results["a"].is_error is False
    assert results["a"].content == "ran"
    assert results["b"].is_error is False
    assert results["b"].content == "ran"
    # G2：重复 id 的首个槽位保留真实结果，第二个槽位为错误块
    slots = results.ordered
    assert [i for i, _ in slots] == ["a", "dup", "dup", "b"]
    assert slots[1][1].is_error is False
    assert slots[1][1].content == "ran"
    assert isinstance(slots[2][1], ToolResultBlock)
    assert slots[2][1].is_error is True
    assert slots[2][1].content == "duplicate tool_use_id"
    # dict 视图保留首个真实结果
    assert results["dup"].is_error is False


# ---------------------------------------------------------------------------
# BatchResults：first-wins 契约 + 覆盖拒绝 warning（UserDict）
# ---------------------------------------------------------------------------


def test_batch_results_construct_keeps_first_result():
    from app.services.llm.tools import BatchResults

    r1 = ToolResultBlock(tool_use_id="dup", content="real", is_error=False)
    r2 = ToolResultBlock(tool_use_id="dup", content="dup", is_error=True)
    results = BatchResults([("dup", r1), ("dup", r2)])
    assert results["dup"] is r1


def test_batch_results_setitem_rejects_overwrite_and_warns(caplog):
    from app.services.llm.tools import BatchResults

    r1 = ToolResultBlock(tool_use_id="a", content="first", is_error=False)
    r2 = ToolResultBlock(tool_use_id="a", content="second", is_error=False)
    results = BatchResults([("a", r1)])

    with caplog.at_level(logging.WARNING, logger="app.services.llm.tools"):
        results["a"] = r2  # 试图覆盖已存在 key
        results.update({"a": r2})  # update 同样触发 __setitem__

    # first-wins：两次覆盖均被拒绝，仍保留首个结果
    assert results["a"] is r1
    # 覆盖路径被 warning 日志显式暴露（非悬垂静默分支）
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 2
    assert all("覆盖已存在的 tool_use_id" in w.getMessage() for w in warns)


def test_batch_results_setitem_allows_new_key():
    from app.services.llm.tools import BatchResults

    r1 = ToolResultBlock(tool_use_id="a", content="first", is_error=False)
    r2 = ToolResultBlock(tool_use_id="b", content="new", is_error=False)
    results = BatchResults([("a", r1)])
    results["b"] = r2  # 新 key 正常写入
    assert results["b"] is r2


# ---------------------------------------------------------------------------
# execute_batch：含 terminal → 返回 TerminalCapture（不执行 handler）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_terminal_returns_capture_without_handler():
    reg = ToolRegistry()
    called = []

    def handler(args):
        called.append(1)
        return "never"

    reg.register(
        ToolDefinition(
            name="submit",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            access="terminal",
        )
    )
    calls = [ToolUseBlock(id="s1", name="submit", input={"subject_id": "9"})]
    results = await reg.execute_batch(calls)
    cap = results["s1"]
    assert isinstance(cap, TerminalCapture)
    assert cap.name == "submit"
    assert cap.args == {"subject_id": "9"}
    assert called == []


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------


def test_module_singleton_get_and_reset():
    reset_tool_registry()
    reg = get_tool_registry()
    assert isinstance(reg, ToolRegistry)
    # 二次获取同一实例
    assert get_tool_registry() is reg
    # reset 后获得新实例
    reset_tool_registry()
    assert get_tool_registry() is not reg

"""provider 消息序列化的**忠实性**守卫。

对应评审意见（``openai_compat.py`` / ``openai_responses.py`` 的 assistant 纯文本
消息带 ``\\n\\n`` 回写）：协议无 block 结构时必须降级为单字符串，但**降级不得改写
模型实际发出的语义**。业界口径：

- 能保序就不 join（Anthropic content 数组 / Responses items 原生支持）；
- 必须 join 时不要凭空造分隔符 —— 模型发出的是连续 delta，直接拼接才是忠实形态；
- 退化按 role 分层 —— system 多块是「多条独立规则」，``\\n\\n`` 有意义；
  assistant / user 多块是「同一次发言被切开」，加分隔符即改写语义。

本文件把该决策表固化为可执行断言，防止后续新增分支时退化。
"""

from __future__ import annotations

from typing import Any

from app.services.llm.models import Message, TextBlock
from app.services.llm.providers.openai_compat import OpenAICompatProvider


def _provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_base="https://api.openai.com/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
    )


def _multi(role: str) -> Message:
    return Message(role=role, content=[TextBlock(text="A"), TextBlock(text="B")])


def test_system_keeps_paragraph_separator():
    """system 多块保留 ``\\n\\n``（多条独立规则的边界）。"""
    assert _provider()._to_wire_messages(_multi("system")) == [
        {"role": "system", "content": "A\n\nB"}
    ]


def test_assistant_uses_empty_join():
    """assistant 多块空串连接（忠实还原连续 delta，不注入分隔符）。"""
    assert _provider()._to_wire_messages(_multi("assistant")) == [
        {"role": "assistant", "content": "AB"}
    ]


def test_user_uses_empty_join():
    """user 多块空串连接。"""
    assert _provider()._to_wire_messages(_multi("user")) == [
        {"role": "user", "content": "AB"}
    ]


def test_anthropic_preserves_assistant_blocks_without_joining():
    """Anthropic content 为 block 数组：assistant 多块原样保序，**不join**。

    （system 是例外：多条 system 合并为单块以保证 prompt cache 前缀稳定，
    那是 ``_system_text`` / system 分支的职责，与 assistant 无关。）
    """
    from app.services.llm.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        api_base="https://api.anthropic.com", api_key="sk-test", model="claude-x"
    )
    wire = provider._to_wire_message(_multi("assistant"))
    # 两个独立 text block，未被拼成一个字符串
    assert wire["content"] == [
        {"type": "text", "text": "A"},
        {"type": "text", "text": "B"},
    ], "Anthropic assistant 多块应逐块保序，不得 join 退化"


def test_anthropic_system_blocks_merge_for_cache_stability():
    """system 多块合并为单块（prompt cache 前缀需逐字节稳定），是**有意**的 join。"""
    from app.services.llm.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        api_base="https://api.anthropic.com", api_key="sk-test", model="claude-x"
    )
    text = provider._system_text(_multi("system").content)
    assert text == "A\n\nB"


def test_agent_history_assistant_messages_always_carry_tool_calls():
    """历史消息中的 assistant 必带 tool_calls（否则 provider兜底 join 会改写语义）。

    今天由两处守卫保证：live loop 仅在 ``terminal_reason is None``（必然含
    tool_calls）时 append assistant；replay 同样以 ``if tool_calls:`` 把关。
    一旦破坏，纯文本 assistant 消息会以 ``\\n\\n`` 拼接被发回 provider。
    """
    import ast
    from pathlib import Path

    def _assistant_appends(tree: ast.AST) -> list[int]:
        return [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and n.args
            and "assistant" in ast.unparse(n.args[0])
        ]

    # trace.py：append assistant 显式位于 ``if tool_calls:`` 守卫内。
    trace_src = Path("app/services/agent/trace.py").read_text(encoding="utf-8")
    trace_tree = ast.parse(trace_src)
    guarded_lines: set[int] = set()
    for node in ast.walk(trace_tree):
        if isinstance(node, ast.If) and "tool_calls" in ast.unparse(node.test):
            guarded_lines.update(_assistant_appends(node))
    assert set(_assistant_appends(trace_tree)) <= guarded_lines, (
        "trace.py: append assistant 必须在 if tool_calls: 守卫内"
    )

    # loop.py：append assistant 位于 ``if terminal_reason is not None:`` 的**之后**
    #          （该分支全部提前 return，故其后必然处于 terminal_reason is None，
    #           而 normalize_stop_reason 仅在 has_tool_calls 时返回 None）。
    loop_src = Path("app/services/agent/loop.py").read_text(encoding="utf-8")
    loop_tree = ast.parse(loop_src)
    early_return_lines: set[int] = set()
    for node in ast.walk(loop_tree):
        if isinstance(node, ast.If) and "terminal_reason is not None" in ast.unparse(
            node.test
        ):
            early_return_lines.update(
                n.lineno for n in ast.walk(node) if isinstance(n, ast.Return)
            )
    append_lines = _assistant_appends(loop_tree)
    assert append_lines, "loop.py 应有 append assistant 消息"
    for lineno in append_lines:
        assert lineno > max(early_return_lines), (
            f"loop.py:{lineno} append assistant 位于终局提前 return 之后"
            "（即 terminal_reason is None 分支内，必然含 tool_calls）"
        )


# ---------------------------------------------------------------------------
# OpenAI Responses 侧：items 数组原生保序，但仍有一处 join 需要同口径收口
# ---------------------------------------------------------------------------


def _responses_provider() -> Any:
    from app.services.llm.providers.openai_responses import OpenAIResponsesProvider

    return OpenAIResponsesProvider(
        api_base="https://api.openai.com/v1", api_key="sk-test"
    )


def test_responses_interleaved_text_and_tool_keep_wire_order():
    """Responses 的 items 数组原生保序：文本-工具-文本不得被压平或重排。

    ``_to_input_items`` 在每个 ``function_call`` 处 flush 文本，因此交错顺序天然
    保真。本用例把该不变量固化，防止后续把 items 合并成单条消息。
    """
    from app.services.llm.models import ToolUseBlock

    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="查一下"),
            ToolUseBlock(id="c1", name="search", input={"q": "x"}),
            TextBlock(text="然后总结"),
        ],
    )
    assert _responses_provider()._to_input_items(msg) == [
        {"role": "assistant", "content": "查一下"},
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "search",
            "arguments": '{"q": "x"}',
        },
        {"role": "assistant", "content": "然后总结"},
    ]


def test_responses_contiguous_text_joined_without_separator():
    """Responses 侧连续文本块也**空串连接**（与 compat 层同口径）。

    注意与「每块一个 item」的区别：模型流式发出的是连续 delta（``models.py``
    ``StreamAggregator`` 亦以 ``""`` 聚合为单块），拆成多条 ``message`` item 会
    凭空造出一条模型没说过的消息边界 —— 那是另一种不忠实。故此处只改分隔符，
    不改项数。
    """
    assert _responses_provider()._to_input_items(_multi("assistant")) == [
        {"role": "assistant", "content": "AB"}
    ]


def test_responses_system_text_keeps_paragraph_separator():
    """system 侧 ``_text_of`` 只服务 system，多块保留 ``\\n\\n``（独立规则边界）。"""
    assert _responses_provider()._text_of(_multi("system").content) == "A\n\nB"

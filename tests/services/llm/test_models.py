"""app.services.llm.models 测试。"""

import pytest
from pydantic import ValidationError

from app.services.llm.models import (
    ChatResponse,
    Message,
    RedactedThinkingBlock,
    StreamAggregator,
    StreamChunk,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)
from app.services.llm.providers.base import BaseProvider


class TestContentBlock:
    """ContentBlock 构造与校验。"""

    def test_text_block(self):
        b = TextBlock(text="hi")
        assert b.type == "text"
        assert b.text == "hi"

    def test_thinking_block(self):
        b = ThinkingBlock(thinking="思考", signature="sig1")
        assert b.type == "thinking"
        assert b.thinking == "思考"
        assert b.signature == "sig1"

    def test_thinking_block_signature_optional(self):
        b = ThinkingBlock(thinking="思考")
        assert b.signature is None

    def test_redacted_thinking_block(self):
        b = RedactedThinkingBlock(data="xxx")
        assert b.type == "redacted_thinking"
        assert b.data == "xxx"

    def test_invalid_type_raises(self):
        """type 字段被 Literal 强约束。"""
        with pytest.raises(ValidationError):
            TextBlock(type="thinking", text="hi")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ThinkingBlock(type="text", thinking="x")  # type: ignore[arg-type]

    def test_unknown_block_type_not_in_union(self):
        """Union 仅接受已知 block 类型。

        tool_use / tool_result 已被纳入 ContentBlock，
        故这两个类型现在可被 Message 正常解析；真正未知的 block type 仍抛
        ValidationError（向后强约束）。
        """
        from app.services.llm.models import ToolResultBlock, ToolUseBlock

        # tool_use / tool_result 现属合法 union 成员
        msg = Message.model_validate(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "u1", "name": "search"}],
            }
        )
        assert isinstance(msg.content[0], ToolUseBlock)

        msg2 = Message.model_validate(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "u1", "content": "ok"}
                ],
            }
        )
        assert isinstance(msg2.content[0], ToolResultBlock)

        # 真正未知的类型仍被拒绝
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "role": "assistant",
                    "content": [{"type": "bogus", "foo": 1}],
                }
            )

    def test_content_block_dump(self):
        b = TextBlock(text="hi")
        assert b.model_dump(exclude_none=True) == {"type": "text", "text": "hi"}


class TestMessage:
    """Message 模型创建和序列化。"""

    def test_create_message(self):
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_message_all_roles(self):
        for role in ("system", "user", "assistant"):
            msg = Message(role=role, content="test")
            assert msg.role == role

    def test_message_invalid_role_raises(self):
        with pytest.raises(ValueError):
            Message(role="invalid", content="test")  # type: ignore[invalid-argument-type]

    def test_message_serialization(self):
        msg = Message(role="user", content="What is AI?")
        d = msg.model_dump()
        assert d == {"role": "user", "content": "What is AI?"}

    def test_message_deserialization(self):
        d = {"role": "assistant", "content": "AI is ..."}
        msg = Message.model_validate(d)
        assert msg.role == "assistant"
        assert msg.content == "AI is ..."

    def test_message_empty_content(self):
        msg = Message(role="system", content="")
        assert msg.content == ""

    def test_message_content_blocks(self):
        """新用法——content 为 list[ContentBlock]。"""
        msg = Message(role="assistant", content=[TextBlock(text="hi")])
        assert isinstance(msg.content, list)
        block = msg.content[0]
        assert isinstance(block, TextBlock)
        assert block.text == "hi"

    def test_message_str_backward_compat(self):
        """旧用法——content 为 str。"""
        msg = Message(role="user", content="纯文本")
        assert isinstance(msg.content, str)
        assert msg.content == "纯文本"

    def test_message_serialization_with_blocks(self):
        msg = Message(role="assistant", content=[TextBlock(text="hi")])
        d = msg.model_dump()
        assert d == {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        }


class TestUsage:
    """Usage 模型默认值和序列化。"""

    def test_usage_defaults(self):
        u = Usage()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0

    def test_usage_custom_values(self):
        u = Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        assert u.prompt_tokens == 100
        assert u.completion_tokens == 50
        assert u.total_tokens == 150

    def test_usage_serialization(self):
        u = Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        d = u.model_dump()
        assert d == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    def test_usage_partial_values(self):
        u = Usage(prompt_tokens=5)
        assert u.prompt_tokens == 5
        assert u.completion_tokens == 0
        assert u.total_tokens == 0


class TestChatResponse:
    """ChatResponse 模型创建、序列化和可选的 usage。"""

    def test_create_without_usage(self):
        resp = ChatResponse(content="Hello", model="gpt-4o-mini")
        assert resp.content == "Hello"
        assert resp.model == "gpt-4o-mini"
        assert resp.usage is None

    def test_create_with_usage(self):
        usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        resp = ChatResponse(content="Hi", model="test", usage=usage)
        assert resp.content == "Hi"
        assert resp.model == "test"
        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 10
        assert resp.usage.total_tokens == 15

    def test_chat_response_serialization_without_usage(self):
        resp = ChatResponse(content="Hi", model="gpt-3.5-turbo")
        d = resp.model_dump()
        assert d["content"] == "Hi"
        assert d["model"] == "gpt-3.5-turbo"
        assert d["usage"] is None

    def test_chat_response_serialization_with_usage(self):
        usage = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        resp = ChatResponse(content="X", model="m", usage=usage)
        d = resp.model_dump()
        assert d["content"] == "X"
        assert d["model"] == "m"
        assert d["usage"] == {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
        }

    def test_chat_response_default_model(self):
        resp = ChatResponse(content="Hi")
        assert resp.model == ""

    def test_chat_response_extra_fields_ignored(self):
        """Pydantic 使用 model_validate 时应默认忽略未知字段。"""
        resp = ChatResponse.model_validate(
            {"content": "Hi", "model": "m", "extra": "should-be-ignored"}
        )
        assert resp.content == "Hi"


class TestStreamAggregator:
    """StreamChunk 序列聚合为等价 ChatResponse。"""

    def test_aggregate_text_deltas_concatenates_content(self):
        """多个 text_delta → content 拼接、blocks 含单个 TextBlock。"""
        agg = StreamAggregator()
        for part in ("你", "好", "世界"):
            agg.feed(StreamChunk(type="text_delta", text=part))

        resp = agg.finalize()

        assert resp.content == "你好世界"
        assert len(resp.blocks) == 1
        block = resp.blocks[0]
        assert isinstance(block, TextBlock)
        assert block.text == "你好世界"

    def test_aggregate_thinking_and_signature_deltas(self):
        """thinking_delta 与 signature 均按增量拼接。"""
        agg = StreamAggregator()
        agg.feed(StreamChunk(type="thinking_delta", thinking="思", signature="sig-1"))
        agg.feed(StreamChunk(type="thinking_delta", thinking="考", signature="sig-2"))

        resp = agg.finalize()

        assert len(resp.blocks) == 1
        block = resp.blocks[0]
        assert isinstance(block, ThinkingBlock)
        assert block.thinking == "思考"
        assert block.signature == "sig-1sig-2"

    def test_aggregate_tool_use_deltas_builds_input_dict(self):
        """tool_use_start + 多段 tool_use_delta → 完整 input dict。"""
        agg = StreamAggregator()
        agg.feed(
            StreamChunk(type="tool_use_start", tool_use_id="t1", tool_name="search")
        )
        agg.feed(
            StreamChunk(type="tool_use_delta", tool_use_id="t1", partial_json='{"q":')
        )
        agg.feed(
            StreamChunk(type="tool_use_delta", tool_use_id="t1", partial_json='"hi"}')
        )

        resp = agg.finalize()

        assert len(resp.blocks) == 1
        block = resp.blocks[0]
        assert isinstance(block, ToolUseBlock)
        assert block.id == "t1"
        assert block.name == "search"
        assert block.input == {"q": "hi"}

    def test_malformed_tool_json_falls_back_to_raw(self):
        """partial_json 非法 → input 兜底 {"raw": <原字符串>}。"""
        agg = StreamAggregator()
        agg.feed(StreamChunk(type="tool_use_start", tool_use_id="t1", tool_name="x"))
        agg.feed(
            StreamChunk(type="tool_use_delta", tool_use_id="t1", partial_json="{bad")
        )

        resp = agg.finalize()

        block = resp.blocks[0]
        assert isinstance(block, ToolUseBlock)
        assert block.input == {"raw": "{bad"}

    def test_usage_and_stop_propagated(self):
        """usage / stop 事件传递到 ChatResponse。"""
        agg = StreamAggregator()
        usage = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        agg.feed(StreamChunk(type="usage", usage=usage))
        agg.feed(StreamChunk(type="stop", stop_reason="tool_use"))

        resp = agg.finalize()

        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 1
        assert resp.usage.total_tokens == 3
        assert resp.stop_reason == "tool_use"

    def test_blocks_order_follows_first_appearance(self):
        """blocks 顺序 = 各块首次出现的事件顺序。"""
        agg = StreamAggregator()
        agg.feed(StreamChunk(type="thinking_delta", thinking="t"))
        agg.feed(StreamChunk(type="text_delta", text="a"))
        agg.feed(StreamChunk(type="tool_use_start", tool_use_id="t1", tool_name="f"))
        agg.feed(
            StreamChunk(type="tool_use_delta", tool_use_id="t1", partial_json="{}")
        )

        resp = agg.finalize()

        assert [type(b).__name__ for b in resp.blocks] == [
            "ThinkingBlock",
            "TextBlock",
            "ToolUseBlock",
        ]

    def test_empty_stream_finalize(self):
        """无事件 finalize → 空 blocks、content 为空字符串。"""
        resp = StreamAggregator().finalize()

        assert resp.blocks == []
        assert resp.content == ""
        assert resp.usage is None


class TestBaseProviderStream:
    """BaseProvider.stream 默认实现契约。"""

    @pytest.mark.asyncio
    async def test_stream_default_raises_not_implemented(self):
        """子类未实现 stream → 迭代时抛 NotImplementedError。"""

        class ChatOnlyProvider(BaseProvider):
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="mocked")

        provider = ChatOnlyProvider()
        with pytest.raises(NotImplementedError):
            async for _ in provider.stream([Message(role="user", content="hi")]):
                pass

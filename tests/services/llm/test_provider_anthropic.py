"""app.services.llm.providers.anthropic 测试。"""

import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app.services.llm.models import (
    Message,
    StreamAggregator,
    TextBlock,
    ThinkingBlock,
    ThinkingLevel,
    ToolUseBlock,
)
from app.services.llm.providers.anthropic import AnthropicProvider


def _make_provider(
    *,
    api_base: str = "https://api.anthropic.com/v1",
    api_key: str = "sk-test",
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2000,
    temperature: float = 0.7,
    timeout: int = 60,
    thinking_level: ThinkingLevel = "off",
) -> AnthropicProvider:
    """构造测试 provider，仅允许覆盖需要调整的参数。"""
    return AnthropicProvider(
        api_base=api_base,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        thinking_level=thinking_level,
    )


class _AsyncContextManager:
    """辅助类：让 mock 对象支持 ``async with``。"""

    def __init__(self, return_value):  # noqa: ANN001
        self._return_value = return_value

    async def __aenter__(self):  # noqa: ANN204
        return self._return_value

    async def __aexit__(self, *args):  # noqa: ANN002, ANN204
        return False


def _sse_lines(*events: tuple[str, dict]) -> list[str]:
    """构造 Anthropic SSE 文本行：event + data + 空行。"""
    lines: list[str] = []
    for name, payload in events:
        lines.append(f"event: {name}")
        lines.append(f"data: {json.dumps(payload)}")
        lines.append("")
    return lines


def _make_stream_mock_client(
    lines: list[str],
    *,
    status_code: int = 200,
    raise_for_status_side_effect: Exception | None = None,
):
    """创建支持 ``client.stream(...)`` 的 mock httpx.AsyncClient。

    stream() 返回异步上下文管理器，其响应体经 ``aiter_lines()`` 逐行产出
    给定的 SSE 行。
    """
    mock_response = Mock()
    mock_response.status_code = status_code

    async def _aiter():
        for line in lines:
            yield line

    mock_response.aiter_lines = Mock(return_value=_aiter())

    if raise_for_status_side_effect is not None:
        mock_response.raise_for_status = Mock(side_effect=raise_for_status_side_effect)
    else:
        mock_response.raise_for_status = Mock()

    mock_client = AsyncMock()
    mock_client.stream = Mock(return_value=_AsyncContextManager(mock_response))
    mock_client.aclose = AsyncMock()
    mock_client.__aenter__.return_value = mock_client

    async def _mock_aexit(*args, **kwargs):
        await mock_client.aclose()

    mock_client.__aexit__ = _mock_aexit
    return mock_client


async def _collect_stream(
    provider: AnthropicProvider,
    lines: list[str],
    *,
    messages: list[Message] | None = None,
    **kwargs,
) -> tuple[list, Mock]:  # noqa: ANN401
    """驱动 provider.stream() 消费给定的 SSE 行，返回 (chunks, mock_client)。"""
    mock_client = _make_stream_mock_client(lines)
    with patch("httpx.AsyncClient", return_value=mock_client):
        chunks = [
            chunk
            async for chunk in provider.stream(
                messages or [Message(role="user", content="Q")], **kwargs
            )
        ]
    return chunks, mock_client


def _aggregate(chunks: list):
    """把 StreamChunk 序列聚合为 ChatResponse。"""
    aggregator = StreamAggregator()
    for chunk in chunks:
        aggregator.feed(chunk)
    return aggregator.finalize()


class TestAnthropicProviderInit:
    """构造函数和默认值。"""

    def test_default_values(self):
        provider = _make_provider()
        assert provider.api_base == "https://api.anthropic.com/v1"
        assert provider.api_key == "sk-test"
        assert provider.model == "claude-sonnet-4-6"
        assert provider.max_tokens == 2000
        assert provider.temperature == 0.7
        assert provider.timeout == 60
        assert provider.thinking_level == "off"

    def test_custom_values(self):
        provider = AnthropicProvider(
            api_base="https://custom.api/v1/",
            api_key="sk-custom",
            model="custom-model",
            max_tokens=500,
            temperature=0.3,
            timeout=30,
            thinking_level="medium",
        )
        # api_base 尾斜杠被去除
        assert provider.api_base == "https://custom.api/v1"
        assert provider.model == "custom-model"
        assert provider.max_tokens == 500
        assert provider.temperature == 0.3
        assert provider.timeout == 30
        assert provider.thinking_level == "medium"


# ===================================================================
# 请求构建
# ===================================================================


class TestBuildRequest:
    """_build_request 纯函数测试。"""

    def test_text_message_wire_format(self):
        """纯文本请求符合 Messages API 格式。"""
        provider = _make_provider()
        body = provider._build_request([Message(role="user", content="Hello")])
        assert body["model"] == "claude-sonnet-4-6"
        assert body["max_tokens"] == 2000
        assert body["temperature"] == 0.7
        assert body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
        ]
        assert "system" not in body
        assert "thinking" not in body

    def test_temperature_kwargs_override(self):
        """temperature kwargs 覆盖配置值。"""
        provider = _make_provider()
        body = provider._build_request(
            [Message(role="user", content="Hello")], temperature=0.3
        )
        assert body["temperature"] == 0.3

    def test_system_prompt_top_level(self):
        """system prompt 提升为顶层参数。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(role="system", content="你是追番助手"),
                Message(role="user", content="Hello"),
            ]
        )
        assert body["system"] == "你是追番助手"
        assert all(m["role"] != "system" for m in body["messages"])

    def test_multiple_system_messages_joined(self):
        """多条 system 消息用 \\n\\n 合并。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(role="system", content="规则A"),
                Message(role="system", content="规则B"),
                Message(role="user", content="Hello"),
            ]
        )
        assert body["system"] == "规则A\n\n规则B"

    def test_single_system_message_unchanged(self):
        """单条 system 消息原样传递，不合并不加分隔符。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(role="system", content="规则A"),
                Message(role="user", content="Hello"),
            ]
        )
        assert body["system"] == "规则A"

    def test_system_message_content_blocks(self):
        """system 消息 content 为 list 时提取 text block，块间用 \\n\\n 拼接（与多条 system 合并语义一致）。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="system",
                    content=[TextBlock(text="规则A"), TextBlock(text="规则B")],
                ),
                Message(role="user", content="Hello"),
            ]
        )
        assert body["system"] == "规则A\n\n规则B"

    def test_system_message_blocks_ignore_non_text(self):
        """system 消息 content 混入 thinking block 时只提取 text。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="system",
                    content=[ThinkingBlock(thinking="思考"), TextBlock(text="规则A")],
                ),
                Message(role="user", content="Hello"),
            ]
        )
        assert body["system"] == "规则A"

    def test_thinking_level_medium_maps_budget(self):
        """thinking_level=medium 映射 budget_tokens=4096，
        max_tokens 自动抬升到 budget + 余量，temperature 强制为 1。"""
        provider = _make_provider(thinking_level="medium")
        body = provider._build_request([Message(role="user", content="Q")])
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
        # Anthropic 约束：budget_tokens 必须小于 max_tokens（思考计入上限）
        assert body["max_tokens"] == 4096 + 1024
        assert body["temperature"] == 1

    @pytest.mark.parametrize(
        ("level", "budget"),
        [("low", 2048), ("medium", 4096), ("high", 8192)],
    )
    def test_thinking_max_tokens_raised_above_budget(self, level, budget):
        """开启思考时 max_tokens 自动抬升至 budget + 余量。

        默认 max_tokens=2000 小于全部三档 budget，若不抬升 Anthropic API
        会以 budget_tokens < max_tokens 约束返回 400。
        """
        provider = _make_provider(thinking_level=level)
        body = provider._build_request([Message(role="user", content="Q")])
        assert body["max_tokens"] == budget + 1024

    def test_thinking_max_tokens_keeps_larger_configured_value(self):
        """用户配置的 max_tokens 大于 budget + 余量时保持不变。"""
        provider = _make_provider(thinking_level="low", max_tokens=8000)
        body = provider._build_request([Message(role="user", content="Q")])
        assert body["max_tokens"] == 8000

    def test_thinking_max_tokens_per_call_override_also_raised(self):
        """per-call max_tokens 覆盖值同样受 budget 约束抬升。"""
        provider = _make_provider(thinking_level="medium")
        body = provider._build_request(
            [Message(role="user", content="Q")], max_tokens=1000
        )
        assert body["max_tokens"] == 4096 + 1024

    def test_thinking_level_high_kwargs_override(self):
        """kwargs thinking_level=high 覆盖全局。"""
        provider = _make_provider(thinking_level="off")
        body = provider._build_request(
            [Message(role="user", content="Q")], thinking_level="high"
        )
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
        assert body["max_tokens"] == 8192 + 1024
        assert body["temperature"] == 1

    def test_thinking_level_off_no_thinking(self):
        """thinking_level=off 不传 thinking，temperature 保持配置值。"""
        provider = _make_provider(thinking_level="off")
        body = provider._build_request([Message(role="user", content="Q")])
        assert "thinking" not in body
        assert body["temperature"] == 0.7

    def test_thinking_level_default_off(self):
        """未配置 thinking_level（缺省 off）时行为一致。"""
        provider = _make_provider()
        body = provider._build_request([Message(role="user", content="Q")])
        assert "thinking" not in body
        assert body["temperature"] == 0.7

    def test_per_call_override_global_default(self):
        """per-call 覆盖全局默认，未传 kwargs 仍走全局。"""
        provider = _make_provider(thinking_level="off")
        body_override = provider._build_request(
            [Message(role="user", content="Q")], thinking_level="high"
        )
        assert body_override["thinking"]["budget_tokens"] == 8192

        body_default = provider._build_request([Message(role="user", content="Q")])
        assert "thinking" not in body_default

    def test_haiku_model_thinking_degraded(self):
        """claude-haiku 模型 thinking 降级为 off。"""
        provider = _make_provider(thinking_level="medium")
        body = provider._build_request(
            [Message(role="user", content="Q")],
            model="claude-haiku-4-5-20251001",
        )
        assert "thinking" not in body
        assert body["temperature"] == 0.7

    def test_thinking_enabled_unknown_model_ok(self):
        """未知模型按支持处理。"""
        provider = _make_provider(thinking_level="medium")
        assert provider._thinking_enabled("medium", "unknown-model") == 4096
        assert provider._thinking_enabled("off", "claude-sonnet-4-6") == 0
        assert provider._thinking_enabled("invalid-level", "claude-sonnet-4-6") == 0


# ===================================================================
# 响应解析
# ===================================================================


# ===================================================================
# stream() 集成（mock httpx SSE）
# ===================================================================


def _message_start(input_tokens: int = 10, model: str = "") -> tuple[str, dict]:
    message: dict = {
        "usage": {"input_tokens": input_tokens, "output_tokens": 1},
    }
    if model:
        message["model"] = model
    return ("message_start", {"type": "message_start", "message": message})


def _block_start(index: int, block: dict) -> tuple[str, dict]:
    return (
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def _block_delta(index: int, delta: dict) -> tuple[str, dict]:
    return (
        "content_block_delta",
        {"type": "content_block_delta", "index": index, "delta": delta},
    )


def _message_delta(
    stop_reason: str = "end_turn", output_tokens: int = 5
) -> tuple[str, dict]:
    return (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
    )


class TestAnthropicProviderStream:
    """stream() SSE 事件映射与聚合。"""

    @pytest.mark.asyncio
    async def test_text_deltas_emitted_in_order_and_aggregate(self):
        """多个 text_delta 依次产出 text_delta，聚合后 content 正确。"""
        lines = _sse_lines(
            _message_start(),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "Hello"}),
            _block_delta(0, {"type": "text_delta", "text": " world"}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _message_delta(output_tokens=5),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        text_chunks = [c for c in chunks if c.type == "text_delta"]
        assert [c.text for c in text_chunks] == ["Hello", " world"]

        resp = _aggregate(chunks)
        assert resp.content == "Hello world"
        assert isinstance(resp.blocks[0], TextBlock)
        assert resp.blocks[0].text == "Hello world"

    @pytest.mark.asyncio
    async def test_thinking_and_signature_preserved(self):
        """thinking_delta + signature_delta 增量拼接，聚合后 signature 不丢。"""
        lines = _sse_lines(
            _message_start(),
            _block_start(0, {"type": "thinking", "thinking": ""}),
            _block_delta(0, {"type": "thinking_delta", "thinking": "思考"}),
            _block_delta(0, {"type": "signature_delta", "signature": "sig-a"}),
            _block_delta(0, {"type": "thinking_delta", "thinking": "中"}),
            _block_delta(0, {"type": "signature_delta", "signature": "sig-b"}),
            _message_delta(output_tokens=8),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        thinking_events = [c for c in chunks if c.type == "thinking_delta"]
        # signature_delta 也映射为 thinking_delta，仅填 signature 字段
        assert [c.thinking for c in thinking_events] == ["思考", "", "中", ""]
        assert [c.signature for c in thinking_events] == ["", "sig-a", "", "sig-b"]

        resp = _aggregate(chunks)
        block = resp.blocks[0]
        assert isinstance(block, ThinkingBlock)
        assert block.thinking == "思考中"
        assert block.signature == "sig-asig-b"

    @pytest.mark.asyncio
    async def test_tool_use_start_and_partial_json(self):
        """tool_use_start + input_json_delta 分片映射为同 id，聚合 input 完整。"""
        lines = _sse_lines(
            _message_start(),
            _block_start(
                0, {"type": "tool_use", "id": "toolu_1", "name": "get_weather"}
            ),
            _block_delta(0, {"type": "input_json_delta", "partial_json": '{"loc'}),
            _block_delta(
                0, {"type": "input_json_delta", "partial_json": 'ation": "Tokyo"}'}
            ),
            _message_delta(stop_reason="tool_use", output_tokens=12),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        starts = [c for c in chunks if c.type == "tool_use_start"]
        assert len(starts) == 1
        assert starts[0].tool_use_id == "toolu_1"
        assert starts[0].tool_name == "get_weather"

        deltas = [c for c in chunks if c.type == "tool_use_delta"]
        assert [c.partial_json for c in deltas] == ['{"loc', 'ation": "Tokyo"}']
        assert all(c.tool_use_id == "toolu_1" for c in deltas)

        resp = _aggregate(chunks)
        block = resp.blocks[0]
        assert isinstance(block, ToolUseBlock)
        assert block.id == "toolu_1"
        assert block.name == "get_weather"
        assert block.input == {"location": "Tokyo"}
        assert resp.stop_reason == "tool_use"

    @pytest.mark.asyncio
    async def test_model_from_message_start_attached(self):
        """model：message_start 的 message.model 附到首个产出事件，聚合后保留。"""
        lines = _sse_lines(
            _message_start(model="claude-real-1"),
            _block_delta(0, {"type": "text_delta", "text": "Hi"}),
            _message_delta(output_tokens=2),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        assert chunks[0].model == "claude-real-1"
        assert all(c.model == "" for c in chunks[1:])
        assert _aggregate(chunks).model == "claude-real-1"

    @pytest.mark.asyncio
    async def test_tool_use_stop_from_content_block_stop(self):
        """content_block_stop 且 index 为 tool_use 块 → tool_use_stop 停点事件。"""
        lines = _sse_lines(
            _message_start(),
            _block_start(
                0, {"type": "tool_use", "id": "toolu_1", "name": "get_weather"}
            ),
            _block_delta(0, {"type": "input_json_delta", "partial_json": "{}"}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _message_delta(stop_reason="tool_use", output_tokens=2),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        stops = [c for c in chunks if c.type == "tool_use_stop"]
        assert len(stops) == 1
        assert stops[0].tool_use_id == "toolu_1"

    @pytest.mark.asyncio
    async def test_content_block_stop_for_text_ignored(self):
        """text 块的 content_block_stop 不产出 tool_use_stop。"""
        lines = _sse_lines(
            _message_start(),
            _block_start(0, {"type": "text", "text": ""}),
            _block_delta(0, {"type": "text_delta", "text": "hi"}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _message_delta(output_tokens=2),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        assert not [c for c in chunks if c.type == "tool_use_stop"]

    @pytest.mark.asyncio
    async def test_usage_merges_input_and_output_tokens(self):
        """message_start 的 input_tokens + message_delta 的 output_tokens 合并为 usage 事件。"""
        lines = _sse_lines(
            _message_start(input_tokens=10),
            _block_delta(0, {"type": "text_delta", "text": "hi"}),
            _message_delta(output_tokens=20),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        usage_chunks = [c for c in chunks if c.type == "usage"]
        assert len(usage_chunks) == 1
        usage = usage_chunks[0].usage
        assert usage is not None
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 20
        assert usage.total_tokens == 30

        # usage 先于 stop 产出
        types = [c.type for c in chunks]
        assert types.index("usage") < types.index("stop")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use", "max_tokens"])
    async def test_stop_reason_passthrough(self, stop_reason):
        """message_delta 的 stop_reason 原样透传到 stop 事件。"""
        lines = _sse_lines(
            _message_start(),
            _message_delta(stop_reason=stop_reason, output_tokens=3),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        stops = [c for c in chunks if c.type == "stop"]
        assert len(stops) == 1
        assert stops[0].stop_reason == stop_reason

    @pytest.mark.asyncio
    async def test_request_body_stream_flag_and_thinking(self):
        """stream() 请求体含 stream=True，thinking 映射与 chat() 完全一致。"""
        lines = _sse_lines(("message_stop", {"type": "message_stop"}))
        provider = _make_provider(thinking_level="low")
        _, mock_client = await _collect_stream(provider, lines)

        mock_client.stream.assert_called_once()
        call_args = mock_client.stream.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "https://api.anthropic.com/v1/messages"

        body = call_args[1]["json"]
        assert body["stream"] is True
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert body["max_tokens"] == 2048 + 1024
        assert body["temperature"] == 1

        # 除 stream 外与 chat() 的请求体一致
        expected = provider._build_request([Message(role="user", content="Q")])
        assert {k: v for k, v in body.items() if k != "stream"} == expected

        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer sk-test"
        assert headers["x-api-key"] == "sk-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert call_args[1]["timeout"] == 60

    @pytest.mark.asyncio
    async def test_unknown_event_ignored(self):
        """插入未知 SSE 事件类型不影响正常产出。"""
        lines = _sse_lines(
            _message_start(),
            _block_delta(0, {"type": "text_delta", "text": "Hello"}),
            ("weird_event", {"type": "weird_event", "foo": 1}),
            _block_delta(0, {"type": "text_delta", "text": " world"}),
            _message_delta(output_tokens=5),
            ("message_stop", {"type": "message_stop"}),
        )
        chunks, _ = await _collect_stream(_make_provider(), lines)

        assert [c.text for c in chunks if c.type == "text_delta"] == ["Hello", " world"]
        assert _aggregate(chunks).content == "Hello world"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 429, 500])
    async def test_http_error_raises(self, status_code):
        """非 2xx 响应抛 httpx.HTTPStatusError。"""
        mock_client = _make_stream_mock_client(
            [],
            status_code=status_code,
            raise_for_status_side_effect=httpx.HTTPStatusError(
                "error",
                request=Mock(),
                response=Mock(status_code=status_code),
            ),
        )
        provider = _make_provider()
        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in provider.stream([Message(role="user", content="Q")]):
                    pass

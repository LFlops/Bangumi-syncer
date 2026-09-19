"""Anthropic provider 工具协议测试（app.services.llm.providers.anthropic）。

覆盖：
- 内部 assistant 消息含 ToolUseBlock → wire tool_use block 1:1（多 tool_use 同消息共存）
- 内部 user 消息含 ToolResultBlock → wire tool_result block
- tools 参数透传进 body（name/description/input_schema 由调用方构造，provider 仅透传）
- tool_choice 参数透传进 body
- cache_control：tools 参数级别的 cache_control 标记随 tools dict 透传（实现以透传为主）
- _parse_response：wire tool_use → ToolUseBlock + stop_reason="tool_use"；文本+工具混合解析

测试直接调用 _build_request / _parse_response，沿用 test_provider_anthropic 的 mock 风格。
"""

import pytest

from app.services.llm.models import (
    ChatResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.services.llm.providers.anthropic import AnthropicProvider


def _make_provider(**kwargs) -> AnthropicProvider:
    """构造测试 provider，仅允许覆盖需要调整的参数。"""
    return AnthropicProvider(
        api_base="https://api.anthropic.com/v1",
        api_key="sk-test",
        model="claude-sonnet-4-6",
        **kwargs,
    )


# ===================================================================
# Feature 1: 请求构建 — 工具协议
# ===================================================================


class TestBuildRequestToolUse:
    """_build_request：内部 assistant ToolUseBlock → wire tool_use block。"""

    def test_assistant_single_tool_use_block_to_wire_1to1(self):
        """单条 assistant ToolUseBlock 1:1 转为 wire tool_use block。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="tu_1", name="search_bangumi", input={"title": "eva"}
                        )
                    ],
                )
            ]
        )
        assert body["messages"] == [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "search_bangumi",
                        "input": {"title": "eva"},
                    }
                ],
            }
        ]

    def test_assistant_multiple_tool_use_blocks_same_message(self):
        """一个 assistant 消息内含多个 tool_use block，逐条 1:1 共存、顺序一致。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="tu_1", name="search_bangumi", input={"title": "eva"}
                        ),
                        ToolUseBlock(
                            id="tu_2", name="search_episode", input={"id": 123}
                        ),
                    ],
                )
            ]
        )
        assert body["messages"] == [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "search_bangumi",
                        "input": {"title": "eva"},
                    },
                    {
                        "type": "tool_use",
                        "id": "tu_2",
                        "name": "search_episode",
                        "input": {"id": 123},
                    },
                ],
            }
        ]

    def test_assistant_text_and_tool_use_mixed_content(self):
        """assistant 消息文本 + tool_use 混排，二者均进入 wire content（顺序保持）。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="assistant",
                    content=[
                        TextBlock(text="我查一下"),
                        ToolUseBlock(
                            id="tu_1", name="search_bangumi", input={"title": "eva"}
                        ),
                    ],
                )
            ]
        )
        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "我查一下"},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "search_bangumi",
                "input": {"title": "eva"},
            },
        ]


class TestBuildRequestToolResult:
    """_build_request：内部 user ToolResultBlock → wire tool_result block。"""

    def test_user_tool_result_block_to_wire(self):
        """user 消息含 ToolResultBlock → wire tool_result block（含 is_error）。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id="tu_1",
                            content='{"subject_id": "123"}',
                            is_error=False,
                        )
                    ],
                )
            ]
        )
        assert body["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": '{"subject_id": "123"}',
                        "is_error": False,
                    }
                ],
            }
        ]

    def test_user_tool_result_block_is_error_true(self):
        """tool_result 的 is_error=True 也原样透传。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id="tu_1", content="boom", is_error=True
                        )
                    ],
                )
            ]
        )
        assert body["messages"][0]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": "boom",
                "is_error": True,
            }
        ]


class TestBuildRequestToolsAndToolChoice:
    """_build_request：tools / tool_choice 透传。"""

    def test_tools_param_passthrough(self):
        """kwargs.tools（调用方构造的 wire dict 列表）透传进 body["tools"]。"""
        provider = _make_provider()
        tools = [
            {
                "name": "search_bangumi",
                "description": "按标题搜索番组",
                "input_schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                },
            }
        ]
        body = provider._build_request(
            [Message(role="user", content="Hello")], tools=tools
        )
        assert body["tools"] == tools

    def test_tool_choice_param_passthrough(self):
        """kwargs.tool_choice dict 透传进 body["tool_choice"]。"""
        provider = _make_provider()
        tool_choice = {"type": "tool", "name": "submit_suggestion"}
        body = provider._build_request(
            [Message(role="user", content="Hello")], tool_choice=tool_choice
        )
        assert body["tool_choice"] == tool_choice

    def test_tools_and_tool_choice_absent_when_not_passed(self):
        """未传 tools/tool_choice 时 body 不含这两个键（不发送空参数）。"""
        provider = _make_provider()
        body = provider._build_request([Message(role="user", content="Hello")])
        assert "tools" not in body
        assert "tool_choice" not in body

    def test_cache_control_in_tools_passthrough(self):
        """cache_control：tools 参数级别的 cache_control 标记随 tools dict 透传（实现以透传为主）。"""
        provider = _make_provider()
        tools = [
            {
                "name": "search_bangumi",
                "description": "按标题搜索番组",
                "input_schema": {"type": "object", "properties": {}},
                "cache_control": {"type": "ephemeral"},
            }
        ]
        body = provider._build_request(
            [Message(role="user", content="Hello")], tools=tools
        )
        assert body["tools"] == tools
        assert body["tools"][0]["cache_control"] == {"type": "ephemeral"}

    def test_flat_parameters_converted_to_input_schema(self):
        """flat 形态（parameters）→ 转换为 input_schema（Anthropic wire 必填）。"""
        provider = _make_provider()
        tools = [
            {
                "name": "search_bangumi",
                "description": "按标题搜索番组",
                "parameters": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                },
            }
        ]
        body = provider._build_request(
            [Message(role="user", content="Hello")], tools=tools
        )
        assert "tools" in body
        t = body["tools"][0]
        assert "input_schema" in t
        assert "parameters" not in t
        assert t["input_schema"] == {
            "type": "object",
            "properties": {"title": {"type": "string"}},
        }
        assert t["name"] == "search_bangumi"
        assert t["description"] == "按标题搜索番组"

    def test_flat_parameters_with_cache_control_preserved(self):
        """flat 形态 + cache_control：转换 input_schema 时保留 cache_control 等其它字段。"""
        provider = _make_provider()
        tools = [
            {
                "name": "search_bangumi",
                "description": "按标题搜索番组",
                "parameters": {"type": "object", "properties": {}},
                "cache_control": {"type": "ephemeral"},
            }
        ]
        body = provider._build_request(
            [Message(role="user", content="Hello")], tools=tools
        )
        t = body["tools"][0]
        assert "input_schema" in t
        assert "parameters" not in t
        assert t["cache_control"] == {"type": "ephemeral"}

    def test_string_tool_choice_objectized(self):
        """字符串 tool_choice → {"type":"tool","name":<str>}（Anthropic wire 对象化）。"""
        provider = _make_provider()
        body = provider._build_request(
            [Message(role="user", content="Hello")],
            tool_choice="submit_suggestion",
        )
        assert body["tool_choice"] == {"type": "tool", "name": "submit_suggestion"}

    def test_tool_choice_none_not_in_body(self):
        """tool_choice=None 时 body 不含该键（不发送 null）。"""
        provider = _make_provider()
        body = provider._build_request(
            [Message(role="user", content="Hello")],
            tool_choice=None,
        )
        assert "tool_choice" not in body


# ===================================================================
# Feature 2: 响应解析 — 工具协议
# ===================================================================


class TestParseResponseToolUse:
    """_parse_response：wire tool_use → ToolUseBlock + stop_reason="tool_use"。"""

    def test_parse_response_tool_use_to_block(self):
        """wire tool_use block → ToolUseBlock，stop_reason 保留为 tool_use。"""
        provider = _make_provider()
        resp = provider._parse_response(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "search_bangumi",
                        "input": {"title": "eva"},
                    }
                ],
                "model": "claude-sonnet-4-6",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        )
        assert resp.stop_reason == "tool_use"
        assert len(resp.blocks) == 1
        block = resp.blocks[0]
        assert isinstance(block, ToolUseBlock)
        assert block.id == "tu_1"
        assert block.name == "search_bangumi"
        assert block.input == {"title": "eva"}
        # tool_use 不产生纯文本内容
        assert resp.content == ""

    def test_parse_response_text_and_tool_mixed(self):
        """文本 + tool_use 混合解析：text 进 content，tool_use 进 blocks，stop_reason 保留。"""
        provider = _make_provider()
        resp = provider._parse_response(
            {
                "content": [
                    {"type": "text", "text": "我查一下番组"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "search_bangumi",
                        "input": {"title": "eva"},
                    },
                ],
                "stop_reason": "tool_use",
            }
        )
        assert resp.content == "我查一下番组"
        assert len(resp.blocks) == 2
        assert isinstance(resp.blocks[0], TextBlock)
        assert resp.blocks[0].text == "我查一下番组"
        assert isinstance(resp.blocks[1], ToolUseBlock)
        assert resp.blocks[1].name == "search_bangumi"
        assert resp.stop_reason == "tool_use"


# ===================================================================
# chat() 集成（mock httpx）— tools/tool_choice 真实发送
# ===================================================================


class TestAnthropicProviderChatTools:
    """chat() 集成：tools/tool_choice 透传至请求体。"""

    @pytest.mark.asyncio
    async def test_tools_and_tool_choice_sent_in_request(self):
        """chat() 把 tools/tool_choice 透传到 /v1/messages 请求体。"""
        from unittest.mock import AsyncMock, Mock, patch

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json = Mock(
            return_value={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "search_bangumi",
                        "input": {"title": "eva"},
                    }
                ],
                "model": "claude-sonnet-4-6",
                "stop_reason": "tool_use",
            }
        )
        mock_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()
        mock_client.__aenter__.return_value = mock_client

        async def _mock_aexit(*args, **kwargs):
            await mock_client.aclose()

        mock_client.__aexit__ = _mock_aexit

        tools = [
            {
                "name": "search_bangumi",
                "description": "按标题搜索番组",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        tool_choice = {"type": "tool", "name": "submit_suggestion"}
        with patch("httpx.AsyncClient", return_value=mock_client):
            provider = _make_provider()
            resp = await provider.chat(
                [Message(role="user", content="Hello")],
                tools=tools,
                tool_choice=tool_choice,
            )

        body = mock_client.post.call_args[1]["json"]
        assert body["tools"] == tools
        assert body["tool_choice"] == tool_choice
        assert isinstance(resp, ChatResponse)
        assert resp.stop_reason == "tool_use"
        assert isinstance(resp.blocks[0], ToolUseBlock)


# ===================================================================
# Feature：连续 tool_result 消息合并（Anthropic 协议：tool_use 必须被
# 紧随其后同一条消息中的 tool_result 一一响应）
# ===================================================================


class TestMergeToolResultMessages:
    """agent 循环逐条追加的 tool_result user 消息 → 归一化为单条。"""

    def test_multiple_tool_results_merged_into_single_user_message(self):
        """assistant 两个 tool_use + 两条独立 user(tool_result) + budget → 合并。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="assistant",
                    content=[
                        ToolUseBlock(id="tu_1", name="search_bangumi", input={}),
                        ToolUseBlock(id="tu_2", name="search_bangumi", input={}),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id="tu_1", content="r1", is_error=False
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(tool_use_id="tu_2", content="r2", is_error=True)
                    ],
                ),
                Message(role="user", content="[剩余轮次：2]"),
            ]
        )
        msgs = body["messages"]
        assert [m["role"] for m in msgs] == ["assistant", "user", "user"]
        merged = msgs[1]["content"]
        assert [b["tool_use_id"] for b in merged] == ["tu_1", "tu_2"]
        assert merged[0]["is_error"] is False
        assert merged[1]["is_error"] is True
        assert msgs[2]["content"][0]["text"] == "[剩余轮次：2]"

    def test_single_tool_result_followed_by_text_kept_separate(self):
        """单条 tool_result 后的纯文本 user（budget）不被并入。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="assistant",
                    content=[ToolUseBlock(id="tu_1", name="t", input={})],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(tool_use_id="tu_1", content="r", is_error=False)
                    ],
                ),
                Message(role="user", content="[剩余轮次：1]"),
            ]
        )
        msgs = body["messages"]
        assert [m["role"] for m in msgs] == ["assistant", "user", "user"]
        assert msgs[1]["content"][0]["type"] == "tool_result"
        assert msgs[2]["content"][0]["type"] == "text"

    def test_already_merged_form_unchanged(self):
        """单条 user 里已含多个 tool_result → 保持原样。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(
                    role="assistant",
                    content=[
                        ToolUseBlock(id="tu_1", name="t", input={}),
                        ToolUseBlock(id="tu_2", name="t", input={}),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(tool_use_id="tu_1", content="r1"),
                        ToolResultBlock(tool_use_id="tu_2", content="r2"),
                    ],
                ),
            ]
        )
        msgs = body["messages"]
        assert [m["role"] for m in msgs] == ["assistant", "user"]
        assert [b["tool_use_id"] for b in msgs[1]["content"]] == ["tu_1", "tu_2"]

    def test_consecutive_plain_text_users_unchanged(self):
        """连续纯文本 user（无 tool_result）不合并。"""
        provider = _make_provider()
        body = provider._build_request(
            [
                Message(role="user", content="a"),
                Message(role="user", content="b"),
            ]
        )
        assert [m["role"] for m in body["messages"]] == ["user", "user"]

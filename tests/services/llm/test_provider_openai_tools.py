"""OpenAI 兼容 provider 工具协议测试。

覆盖 _build_request / _parse_response 的 tool 拆并与 tools/tool_choice 透传，
以及 wire→内部历史消息合并。
"""

import json

from app.services.llm.models import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.services.llm.providers.openai_compat import OpenAICompatProvider


class TestOpenAICompatToolsBuildRequest:
    """_build_request：内部模型 → OpenAI wire 工具协议拆并。"""

    def _provider(self, **kwargs):
        return OpenAICompatProvider(
            api_base="https://api.openai.com/v1",
            api_key="sk-test",
            model="gpt-4o-mini",
            **kwargs,
        )

    def test_tool_use_blocks_to_assistant_tool_calls(self):
        """assistant 消息含多个 ToolUseBlock → 聚合为单条 assistant.tool_calls，
        arguments 为 JSON 字符串。"""
        provider = self._provider()
        messages = [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="call_1", name="search_bangumi", input={"title": "测试"}
                    ),
                    ToolUseBlock(
                        id="call_2", name="get_detail", input={"subject_id": "123"}
                    ),
                ],
            )
        ]
        wire = provider._build_request(messages)["messages"]
        assert len(wire) == 1
        assert wire[0]["role"] == "assistant"
        assert wire[0]["content"] is None
        tcs = wire[0]["tool_calls"]
        assert len(tcs) == 2
        assert tcs[0]["id"] == "call_1"
        assert tcs[0]["type"] == "function"
        assert tcs[0]["function"]["name"] == "search_bangumi"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"title": "测试"}
        assert tcs[1]["function"]["name"] == "get_detail"
        assert json.loads(tcs[1]["function"]["arguments"]) == {"subject_id": "123"}

    def test_assistant_text_and_tool_use_coexist(self):
        """assistant 文本与 tool_use 共存：content 字段与 tool_calls 同消息。"""
        provider = self._provider()
        messages = [
            Message(
                role="assistant",
                content=[
                    TextBlock(text="我先查一下"),
                    ToolUseBlock(
                        id="call_1", name="search_bangumi", input={"title": "x"}
                    ),
                ],
            )
        ]
        wire = provider._build_request(messages)["messages"]
        assert wire[0]["content"] == "我先查一下"
        assert wire[0]["tool_calls"][0]["id"] == "call_1"

    def test_tool_result_blocks_to_role_tool_messages(self):
        """user 消息含多个 ToolResultBlock → 拆为多条 role=tool 消息，
        tool_call_id 对应；is_error 加 [ERROR] 前缀。"""
        provider = self._provider()
        messages = [
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="call_1", content="结果A"),
                    ToolResultBlock(
                        tool_use_id="call_2", content="结果B", is_error=True
                    ),
                ],
            )
        ]
        wire = provider._build_request(messages)["messages"]
        assert len(wire) == 2
        assert wire[0] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "结果A",
        }
        assert wire[1] == {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": "[ERROR] 结果B",
        }

    def test_mixed_text_and_tool_result(self):
        """user 消息混排 text + tool_result：文本先输出独立 user 消息，
        再拆 tool 消息。"""
        provider = self._provider()
        messages = [
            Message(
                role="user",
                content=[
                    TextBlock(text="用户说：请查"),
                    ToolResultBlock(tool_use_id="call_1", content="结果"),
                ],
            )
        ]
        wire = provider._build_request(messages)["messages"]
        assert wire[0] == {"role": "user", "content": "用户说：请查"}
        assert wire[1] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "结果",
        }

    def test_tools_passthrough(self):
        """tools 参数由调用方构造 dict 列表，provider 透传。"""
        provider = self._provider()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_bangumi",
                    "description": "搜索",
                    "parameters": {"type": "object"},
                },
            }
        ]
        body = provider._build_request([Message(role="user", content="Q")], tools=tools)
        assert body["tools"] == tools

    def test_tool_choice_passthrough(self):
        """tool_choice 以 function 模式透传。"""
        provider = self._provider()
        tool_choice = {"type": "function", "function": {"name": "search_bangumi"}}
        body = provider._build_request(
            [Message(role="user", content="Q")], tool_choice=tool_choice
        )
        assert body["tool_choice"] == tool_choice

    def test_cache_control_not_in_body(self):
        """cache_control 是 Anthropic 专属参数，OpenAI 请求体不得包含。"""
        provider = self._provider()
        body = provider._build_request(
            [Message(role="user", content="Q")],
            cache_control={"type": "ephemeral"},
        )
        assert "cache_control" not in body


class TestOpenAICompatToolsParseResponse:
    """_parse_response：OpenAI wire → 内部模型（tool_calls 解析）。"""

    def _provider(self):
        return OpenAICompatProvider(
            api_base="https://api.openai.com/v1", api_key="sk-test"
        )

    def test_tool_calls_to_tool_use_blocks(self):
        """wire tool_calls → ToolUseBlock 列表；stop_reason 映射为 tool_use。"""
        provider = self._provider()
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "search_bangumi",
                                    "arguments": '{"title": "x"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "model": "gpt-4o-mini",
        }
        resp = provider._parse_response(data)
        assert resp.stop_reason == "tool_use"
        assert len(resp.blocks) == 1
        assert isinstance(resp.blocks[0], ToolUseBlock)
        assert resp.blocks[0].id == "call_1"
        assert resp.blocks[0].name == "search_bangumi"
        assert resp.blocks[0].input == {"title": "x"}

    def test_invalid_json_arguments_fallback(self):
        """arguments 非法 JSON → 兜底 {"raw": "<原字符串>"}。"""
        provider = self._provider()
        data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "f", "arguments": "not-json{"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "model": "gpt-4o-mini",
        }
        resp = provider._parse_response(data)
        assert resp.blocks[0].input == {"raw": "not-json{"}

    def test_text_and_tool_calls_in_response(self):
        """响应含文本与 tool_calls：content 拼文本，blocks 含 Text + ToolUse。"""
        provider = self._provider()
        data = {
            "choices": [
                {
                    "message": {
                        "content": "让我查一下",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "f", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "model": "m",
        }
        resp = provider._parse_response(data)
        assert resp.content == "让我查一下"
        assert isinstance(resp.blocks[0], TextBlock)
        assert isinstance(resp.blocks[1], ToolUseBlock)
        assert resp.blocks[1].input == {}

    def test_finish_reason_stop_unchanged(self):
        """非 tool_calls 的 finish_reason 保持原值（与 OpenAI 映射行为一致）。"""
        provider = self._provider()
        resp = provider._parse_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "model": "m",
            }
        )
        assert resp.stop_reason == "stop"


class TestOpenAICompatWireToMessages:
    """_wire_to_messages：wire → 内部历史消息合并。"""

    def _provider(self):
        return OpenAICompatProvider(
            api_base="https://api.openai.com/v1", api_key="sk-test"
        )

    def test_consecutive_role_tool_merged_to_tool_results(self):
        """连续的 role=tool 消息 → 合并回一条 user 消息的多个 ToolResultBlock。"""
        provider = self._provider()
        wire = [
            {"role": "user", "content": "请查"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "结果A"},
            {"role": "tool", "tool_call_id": "call_2", "content": "[ERROR] 结果B"},
            {"role": "user", "content": "继续"},
        ]
        messages = provider._wire_to_messages(wire)

        tool_msgs = [
            m
            for m in messages
            if isinstance(m.content, list)
            and any(isinstance(b, ToolResultBlock) for b in m.content)
        ]
        assert len(tool_msgs) == 1
        blocks: list[ContentBlock] = tool_msgs[0].content
        assert len(blocks) == 2
        assert blocks[0].tool_use_id == "call_1"
        assert blocks[0].content == "结果A"
        assert blocks[0].is_error is False
        assert blocks[1].tool_use_id == "call_2"
        assert blocks[1].content == "结果B"
        assert blocks[1].is_error is True

        # 普通文本 user 消息原样保留
        assert any(m.role == "user" and m.content == "请查" for m in messages)
        assert any(m.role == "user" and m.content == "继续" for m in messages)

    def test_role_tool_error_prefix_stripped_on_merge(self):
        """合并时 [ERROR] 前缀还原为 is_error 标记，内容去掉前缀。"""
        provider = self._provider()
        wire = [{"role": "tool", "tool_call_id": "c1", "content": "[ERROR] boom"}]
        messages = provider._wire_to_messages(wire)
        assert len(messages) == 1
        blocks = messages[0].content
        assert blocks[0].is_error is True
        assert blocks[0].content == "boom"

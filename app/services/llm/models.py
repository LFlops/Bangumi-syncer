"""LLM 数据模型（纯数据结构层）。

定义聊天交互的核心 Pydantic 模型：Message、ContentBlock、Usage 和 ChatResponse。

Message.content 支持纯文本（str，旧用法）或 content blocks。
ContentBlock 是内部归一化模型，形状对齐 Anthropic
Messages API 的 content blocks，各 provider 负责与自己的 wire 格式互转。
"""

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

ThinkingLevel = Literal["off", "low", "medium", "high"]


class TextBlock(BaseModel):
    """文本内容块。"""

    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    """思考内容块（Anthropic extended thinking）。"""

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: Optional[str] = None  # Anthropic 的 thinking signature


class RedactedThinkingBlock(BaseModel):
    """被遮蔽的思考内容块（Anthropic 签名验证安全机制）。"""

    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


class ToolUseBlock(BaseModel):
    """工具调用请求块（Anthropic tool_use / OpenAI tool_calls 归一化）。

    - id: 工具调用唯一标识，下游 ToolResult 据此关联
    - name: 工具名称（与 ToolRegistry 注册名一致）
    - input: 工具入参（JSON 对象，默认空 dict）
    """

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict = {}


class ToolResultBlock(BaseModel):
    """工具执行结果块（Anthropic tool_result / OpenAI role=tool 归一化）。

    - tool_use_id: 对应的 ToolUseBlock.id
    - content: 结果文本（可为 JSON 字符串）
    - is_error: 工具执行是否失败（供循环自我纠正）
    """

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


# 向后兼容：Text/Thinking/Redacted 现有行为不变；扩展追加
# ToolUse/ToolResult 两类工具协议块。
ContentBlock = Union[
    TextBlock,
    ThinkingBlock,
    RedactedThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
]


class Message(BaseModel):
    """单条聊天消息，包含角色和内容。

    content 兼容旧用法（纯文本 str）；富内容场景使用 list[ContentBlock]。
    """

    role: Literal["system", "user", "assistant"]
    content: Union[str, list[ContentBlock]]


class Usage(BaseModel):
    """聊天补全的 token 用量统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """聊天补全请求的响应。

    content 为纯文本（无 tool call 时），旧代码照常用；blocks 携带完整内容块
    （含 thinking），供 agent 循环消费；stop_reason 为统一结束原因
    （end_turn / tool_use / max_tokens）。
    """

    content: str
    blocks: list[ContentBlock] = Field(default_factory=list)
    stop_reason: str = ""
    model: str = ""
    usage: Optional[Usage] = None
    latency: int = 0

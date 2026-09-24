"""LLM 数据模型（纯数据结构层）。

定义聊天交互的核心 Pydantic 模型：Message、ContentBlock、Usage 和 ChatResponse。

Message.content 支持纯文本（str，旧用法）或 content blocks。
ContentBlock 是内部归一化模型，形状对齐 Anthropic
Messages API 的 content blocks，各 provider 负责与自己的 wire 格式互转。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ThinkingLevel = Literal["off", "low", "medium", "high"]


class TextBlock(BaseModel):
    """文本内容块。"""

    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    """思考内容块（Anthropic extended thinking）。"""

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str | None = None  # Anthropic 的 thinking signature


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
ContentBlock = (
    TextBlock | ThinkingBlock | RedactedThinkingBlock | ToolUseBlock | ToolResultBlock
)


class Message(BaseModel):
    """单条聊天消息，包含角色和内容。

    content 兼容旧用法（纯文本 str）；富内容场景使用 list[ContentBlock]。
    """

    role: Literal["system", "user", "assistant"]
    content: str | list[ContentBlock]


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
    usage: Usage | None = None
    latency: int = 0


@dataclass
class StreamChunk:
    """provider 无关的流式归一化事件（SSE 单条增量）。

    各 provider 负责把自己的 wire 流式事件映射到本模型：

    - text_delta / thinking_delta: 增量文本，分别填 text / thinking（signature 亦增量）
    - tool_use_start: 新建工具调用槽，填 tool_use_id / tool_name
    - tool_use_delta: 工具入参 JSON 增量片段，填 tool_use_id / partial_json
    - usage: 填 usage
    - stop: 填 stop_reason
    """

    type: Literal[
        "text_delta",
        "thinking_delta",
        "tool_use_start",
        "tool_use_delta",
        "usage",
        "stop",
    ]
    text: str = ""
    thinking: str = ""
    signature: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    partial_json: str = ""
    usage: Usage | None = None
    stop_reason: str | None = None


@dataclass
class StreamAggregator:
    """把 StreamChunk 序列聚合为等价 ChatResponse。

    聚合规则：

    - text_delta → 单个 TextBlock；thinking_delta → 单个
      ThinkingBlock（thinking / signature 均增量拼接）
    - tool_use_start → 新建 ToolUseBlock 槽；tool_use_delta 按 tool_use_id
      累积 partial_json，finalize 时 json.loads，失败兜底 {"raw": ...}
    - usage / stop 事件透传到 ChatResponse 的 usage / stop_reason
    - blocks 顺序 = 各块「首次出现」的事件顺序
    """

    _order: list[tuple[str, str]] = field(default_factory=list)
    _text_parts: list[str] = field(default_factory=list)
    _thinking_parts: list[str] = field(default_factory=list)
    _signature_parts: list[str] = field(default_factory=list)
    _tool_names: dict[str, str] = field(default_factory=dict)
    _tool_json_parts: dict[str, list[str]] = field(default_factory=dict)
    _usage: Usage | None = None
    _stop_reason: str | None = None

    def feed(self, chunk: StreamChunk) -> None:
        """累积一个流式事件。"""
        if chunk.type == "text_delta":
            self._append_text(chunk.text)
        elif chunk.type == "thinking_delta":
            self._append_thinking(chunk)
        elif chunk.type == "tool_use_start":
            self._start_tool(chunk.tool_use_id, chunk.tool_name)
        elif chunk.type == "tool_use_delta":
            self._append_tool_json(chunk.tool_use_id, chunk.partial_json)
        elif chunk.type == "usage":
            self._usage = chunk.usage
        elif chunk.type == "stop":
            self._stop_reason = chunk.stop_reason
        else:
            # 防御兜底：Literal 已限定类型，未知事件仅记录不中断
            logger.warning("StreamAggregator 收到未知事件类型: %r", chunk.type)

    def _append_text(self, text: str) -> None:
        if not self._text_parts:
            self._order.append(("text", ""))
        self._text_parts.append(text)

    def _append_thinking(self, chunk: StreamChunk) -> None:
        if not self._thinking_parts and not self._signature_parts:
            self._order.append(("thinking", ""))
        self._thinking_parts.append(chunk.thinking)
        self._signature_parts.append(chunk.signature)

    def _start_tool(self, tool_use_id: str, tool_name: str) -> None:
        if tool_use_id in self._tool_names:
            logger.warning("StreamAggregator 收到重复 tool_use_start: %s", tool_use_id)
            return
        self._tool_names[tool_use_id] = tool_name
        self._tool_json_parts[tool_use_id] = []
        self._order.append(("tool_use", tool_use_id))

    def _append_tool_json(self, tool_use_id: str, partial_json: str) -> None:
        if tool_use_id not in self._tool_json_parts:
            # 防御兜底：缺少 tool_use_start 的增量片段，按新槽处理并记录
            logger.warning(
                "StreamAggregator 收到无 start 的 tool_use_delta: %s", tool_use_id
            )
            self._tool_names[tool_use_id] = ""
            self._tool_json_parts[tool_use_id] = []
            self._order.append(("tool_use", tool_use_id))
        self._tool_json_parts[tool_use_id].append(partial_json)

    def _build_tool_block(self, tool_use_id: str) -> ToolUseBlock:
        raw = "".join(self._tool_json_parts.get(tool_use_id, []))
        try:
            parsed = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            # 与 providers/openai_compat.py 的解析失败兜底对齐
            parsed = {"raw": raw}
        return ToolUseBlock(
            id=tool_use_id,
            name=self._tool_names.get(tool_use_id, ""),
            input=parsed,
        )

    def finalize(self) -> ChatResponse:
        """产出终态 ChatResponse（blocks 按首次出现顺序）。"""
        blocks: list[ContentBlock] = []
        for kind, key in self._order:
            if kind == "text":
                blocks.append(TextBlock(text="".join(self._text_parts)))
            elif kind == "thinking":
                blocks.append(
                    ThinkingBlock(
                        thinking="".join(self._thinking_parts),
                        signature="".join(self._signature_parts) or None,
                    )
                )
            elif kind == "tool_use":
                blocks.append(self._build_tool_block(key))
            else:
                # 防御兜底：未知块标记仅记录不中断
                logger.warning("StreamAggregator 遇到未知块标记: %r", kind)

        text = "".join(b.text for b in blocks if isinstance(b, TextBlock))
        return ChatResponse(
            content=text,
            blocks=blocks,
            stop_reason=self._stop_reason or "",
            usage=self._usage,
        )

"""Anthropic Messages API provider。

基于 httpx 实现的 BaseProvider，与任何遵循 Anthropic /v1/messages
API 规范的端点通信（官方 API 或兼容代理/网关）。

内部中立模型 → Anthropic wire 格式的差异收敛在 _build_request /
_parse_response 两个方法内：
- system prompt 抽为顶层参数（多条用 \\n\\n 连接）
- content 统一为 content blocks 数组
- thinking_level 映射为 thinking.budget_tokens（模型不支持时降级）
"""

from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.services.llm.models import (
    ChatResponse,
    ContentBlock,
    Message,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ThinkingLevel,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from app.services.llm.providers.base import BaseProvider
from app.utils.http_client import create_async_client


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API 的 LLM provider。

    与任何遵循 Anthropic /v1/messages API 规范的端点通信。

    Attributes:
        api_base: API 的基础 URL（如 https://api.anthropic.com/v1）。
        api_key: 用于认证的 API key（随 x-api-key 与 Authorization: Bearer 双头发送，
            兼顾官方 API 与 OpenAI 风格网关）。
        model: 默认使用的模型名称。
        max_tokens: 补全的默认最大 token 数（Anthropic API 必填）。
        temperature: 默认采样温度（thinking 开启时被强制为 1）。
        timeout: 请求超时时间（秒）。
        proxy: 可选的 HTTP 代理 URL。
        thinking_level: 思考强度 off/low/medium/high（每任务 kwargs 可覆盖）。
    """

    # thinking_level → Anthropic budget_tokens 映射
    # 键为 str：_thinking_enabled 需对无效值兜底为 0（ThinkingLevel 约束在构造参数）
    _THINKING_BUDGETS: dict[str, int] = {
        "off": 0,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
    }

    # thinking 开启时 max_tokens 的最低抬升余量：Anthropic 约束 budget_tokens
    # 必须小于 max_tokens（思考 token 计入 max_tokens 上限），默认 max_tokens=2000
    # 小于三档 budget（2048/4096/8192），需自动抬升并留出实际输出空间。
    _THINKING_HEADROOM: int = 1024

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 2000,
        temperature: float = 0.7,
        timeout: int = 60,
        proxy: str | None = None,
        thinking_level: ThinkingLevel = "off",
    ) -> None:
        """初始化 Anthropic provider。"""
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.proxy = proxy
        self.thinking_level = thinking_level

    async def chat(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
        """向 API 发送聊天补全请求。

        Args:
            messages: 对话消息列表。
            **kwargs: 覆盖默认的 model、max_tokens、temperature 或 thinking_level。

        Returns:
            包含助手回复内容和可选用量的 ChatResponse。

        Raises:
            httpx.HTTPStatusError: HTTP 错误响应。
            httpx.TimeoutException: 请求超时。
        """
        url = f"{self.api_base}/messages"
        model = kwargs.get("model", self.model)
        proxy_label = f", proxy={self.proxy}" if self.proxy else ""
        logger.debug(
            f"LLM request: url={url}, model={model}, "
            f"timeout={self.timeout}s{proxy_label}"
        )

        body = self._build_request(messages, **kwargs)
        # x-api-key 为 Anthropic 官方文档标准认证头，Authorization: Bearer 兼容
        # OpenAI 风格网关——双发对两类端点都兼容且无害。
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }

        async with create_async_client(
            proxy=self.proxy,
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                url,
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

        return self._parse_response(data)

    def _build_request(self, messages: list[Message], **kwargs: Any) -> dict:
        """内部模型 → Anthropic wire 格式（请求体）。"""
        system_parts = [
            self._system_text(m.content) for m in messages if m.role == "system"
        ]
        body: dict[str, Any] = {
            "model": kwargs.get("model", self.model),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "messages": self._merge_tool_result_messages(
                [self._to_wire_message(m) for m in messages if m.role != "system"]
            ),
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)

        # tools / tool_choice：工具协议 wire 规范化（provider 拥有 wire 格式，
        # agent/场景层保持 provider 无关）。
        # - tools 元素含 input_schema → 原样透传；含 parameters（内部 flat 形态）
        #   → 转换为 {"name", "description", "input_schema": parameters}；其余字段
        #   （如 cache_control）一并无损保留。
        # - tool_choice 字符串 → {"type":"tool","name":<str>}；dict → 透传；
        #   None → 不发送该键。
        tools = kwargs.get("tools")
        if tools is not None:
            body["tools"] = [self._normalize_anthropic_tool(t) for t in tools]
        tool_choice = kwargs.get("tool_choice")
        if tool_choice is not None:
            normalized_tc = self._normalize_anthropic_tool_choice(tool_choice)
            if (
                self._force_tool_choice_degraded
                and isinstance(normalized_tc, dict)
                and normalized_tc.get("type") in ("tool", "any")
            ):
                # 端点级降级（client 依据服务端拒绝置位）：该端点不支持强制
                # 工具选择（如 thinking 模式约束），改为 auto。
                logger.warning(
                    "已按端点约束将强制 tool_choice 降级为 auto"
                    "（该端点不支持强制工具选择）"
                )
                normalized_tc = {"type": "auto"}
            body["tool_choice"] = normalized_tc

        # thinking_level：每任务 kwargs 覆盖 > 全局默认；模型不支持时降级；
        # 端点拒绝过扩展参数时（_extras_disabled）不再发送
        level = kwargs.get("thinking_level", self.thinking_level)
        budget = (
            0
            if self._extras_disabled
            else self._thinking_enabled(level, kwargs.get("model", self.model))
        )
        if budget > 0:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic 约束：budget_tokens 必须小于 max_tokens，且 max_tokens
            # 同时计入思考与输出 token。用户配置的 max_tokens 可能小于 budget
            # （默认 2000 < 三档 budget），自动抬升到 budget + 余量；
            # 用户配置更大时保持用户的值不变。
            body["max_tokens"] = max(
                body["max_tokens"], budget + self._THINKING_HEADROOM
            )
            body["temperature"] = (
                1  # Anthropic 要求 thinking 开启时 temperature 必须为 1
            )
            # thinking 模式不支持强制工具选择（Anthropic/DeepSeek 约束：
            # tool_choice 仅 auto/none 可用）→ 降级 auto 并告警；收尾依赖模型
            # 自行提交（场景侧 output_parser 兜底解析文本建议）。
            forced = body.get("tool_choice")
            if isinstance(forced, dict) and forced.get("type") in ("tool", "any"):
                logger.warning(
                    "thinking 模式不支持强制 tool_choice，已降级为 auto"
                    "（收尾依赖模型自行调用终止工具）"
                )
                body["tool_choice"] = {"type": "auto"}
        return body

    @staticmethod
    def _normalize_anthropic_tool(tool: dict) -> dict:
        """将工具 schema 规范化为 Anthropic wire 形态。

        - 含 input_schema → 原样透传（已是 Anthropic 形态）；
        - 含 parameters（内部 flat 形态）→ 转为 input_schema；
        - 其余字段（cache_control 等）一并无损保留。
        """
        if "input_schema" in tool:
            return tool
        result: dict[str, Any] = {
            "name": tool["name"],
            "description": tool["description"],
        }
        if "parameters" in tool:
            result["input_schema"] = tool["parameters"]
        for k, v in tool.items():
            if k not in ("name", "description", "parameters"):
                result[k] = v
        return result

    @staticmethod
    def _normalize_anthropic_tool_choice(tool_choice: Any) -> Any:
        """字符串 tool_choice → {"type":"tool","name":<str>}；dict → 透传。"""
        if isinstance(tool_choice, str):
            return {"type": "tool", "name": tool_choice}
        return tool_choice

    def _thinking_enabled(self, level: str, model: str) -> int:
        """返回 budget_tokens；模型不支持或 level=off 时返回 0。"""
        # 包含匹配而非前缀：兼容网关透传的带前缀模型名（如 "anthropic/claude-haiku-..."）
        if "claude-haiku" in model:
            logger.warning(f"model {model} 不支持 extended thinking，已降级为 off")
            return 0
        return self._THINKING_BUDGETS.get(level, 0)

    def _system_text(self, content: str | list[ContentBlock]) -> str:
        """提取 system 消息文本：str 直接用，list 取 text block 拼接（其余类型跳过）。

        多块拼接与多条 system 消息的合并（\n\n）保持同一分隔语义，避免
        记忆注入产出多块 system 时与多条 system 消息行为不一致。
        """
        if isinstance(content, str):
            return content
        return "\n\n".join(b.text for b in content if isinstance(b, TextBlock))

    @staticmethod
    def _merge_tool_result_messages(wire_messages: list[dict]) -> list[dict]:
        """合并连续的 tool_result user 消息为单条。

        Anthropic 协议要求 assistant 的全部 ``tool_use`` 由**紧随其后同一条
        消息**中的 ``tool_result`` 一一响应。agent 循环按 provider 无关契约
        逐条追加（assistant tool_use × N → user(tr1) → user(tr2) → ...），
        此处归一化收敛，避免第 2..N 个 tool_use 悬空（真实端点 400：
        "``tool_use`` ids were found without ``tool_result`` blocks
        immediately after"）。纯文本 user 消息（如预算提示）不合并，
        保持原有交替语义。
        """

        def _is_tool_result(msg: dict) -> bool:
            return msg.get("role") == "user" and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in (msg.get("content") or [])
            )

        merged: list[dict] = []
        for msg in wire_messages:
            if _is_tool_result(msg) and merged and _is_tool_result(merged[-1]):
                merged[-1]["content"] = list(merged[-1].get("content") or []) + list(
                    msg.get("content") or []
                )
                continue
            merged.append(msg)
        return merged

    def _to_wire_message(self, m: Message) -> dict:
        """内部消息 → Anthropic wire 消息（content 统一为 blocks 数组）。"""
        if isinstance(m.content, str):
            blocks = [{"type": "text", "text": m.content}]
        else:
            blocks = [self._to_wire_block(block) for block in m.content]
        return {"role": m.role, "content": blocks}

    def _to_wire_block(self, block: ContentBlock) -> dict:
        """内部 content block → Anthropic wire content block。

        工具协议：
        - ToolUseBlock → {"type": "tool_use", "id", "name", "input"}（assistant 消息，1:1）
        - ToolResultBlock → {"type": "tool_result", "tool_use_id", "content", "is_error"}（user 消息）
        其余类型沿用 model_dump（exclude_none）保持向后行为一致。block 若携带
        cache_control 等透传字段，model_dump 已含则一并透传。
        """
        if isinstance(block, ToolUseBlock):
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        if isinstance(block, ToolResultBlock):
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
                "is_error": block.is_error,
            }
        return block.model_dump(exclude_none=True)

    def _parse_response(self, data: dict) -> ChatResponse:
        """Anthropic wire 格式 → 内部模型。"""
        blocks: list[ContentBlock] = []
        text_parts: list[str] = []
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                blocks.append(TextBlock(text=text))
                text_parts.append(text)
            elif btype == "thinking":
                blocks.append(
                    ThinkingBlock(
                        thinking=block.get("thinking", ""),
                        signature=block.get("signature"),
                    )
                )
            elif btype == "redacted_thinking":
                blocks.append(RedactedThinkingBlock(data=block.get("data", "")))
            elif btype == "tool_use":
                # 工具调用请求块：转为内部 ToolUseBlock，
                # stop_reason 为 "tool_use" 时由调用方驱动 agent 循环执行工具。
                blocks.append(
                    ToolUseBlock(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input", {}) or {},
                    )
                )
            else:
                # 真正未知的 block 类型：跳过 + warning，不崩溃
                logger.warning(f"未知 content block 类型 {btype!r}，已跳过")

        # Anthropic usage 字段映射：input_tokens → prompt_tokens,
        # output_tokens → completion_tokens
        usage: Usage | None = None
        if "usage" in data:
            u = data["usage"]
            prompt = u.get("input_tokens", 0)
            completion = u.get("output_tokens", 0)
            usage = Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            )

        return ChatResponse(
            content="".join(text_parts),
            blocks=blocks,
            stop_reason=data.get("stop_reason", ""),
            model=data.get("model", ""),
            usage=usage,
        )

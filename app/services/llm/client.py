"""LLM 客户端（重试逻辑与用量日志记录）。

提供 LLMClient —— 对 OpenAI 兼容 provider 的封装，增加了自动重试（含退避等待）
和用量记录功能。同时通过 get_llm_client() 导出模块级单例。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.core.config import config_manager
from app.core.logging import logger

from .models import (
    ChatResponse,
    Message,
    StreamAggregator,
    StreamChunk,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from .providers.anthropic import AnthropicProvider
from .providers.base import BaseProvider
from .providers.openai_compat import OpenAICompatProvider
from .providers.openai_responses import OpenAIResponsesProvider

_PROVIDER_MAP: dict[str, type] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic_compat": AnthropicProvider,
    "openai_responses": OpenAIResponsesProvider,
}


def _format_error_detail(e: Exception) -> str:
    """从异常对象提取详细的错误信息，包含异常类型、消息、底层原因和请求 URL。"""
    parts = [f"{type(e).__name__}: {e}"]
    cause = getattr(e, "__cause__", None)
    if cause is not None:
        parts.append(f"[cause: {type(cause).__name__}: {cause}]")
    try:
        # httpx 的 .request property 在未附加请求时会抛 RuntimeError
        # （如连接建立前的 ConnectError/超时），此处兜底避免格式化本身失败
        req = getattr(e, "request", None)
    except RuntimeError:
        req = None
    if req is not None:
        parts.append(f"[url: {req.url}]")
    return " ".join(parts)


# 端点拒绝扩展参数的响应特征（大小写不敏感子串匹配）。
# 模式串已收窄到具体短语，避免网关无关文案（如 "model does not support streaming"、
# "unrecognized model"）被误判为参数拒绝。
# OpenAI canonical: "Unrecognized request argument supplied: <param>" → "unrecognized request argument"；
# 网关变体: "unrecognized parameter '<param>' is not supported" → "unrecognized parameter"。
# thinking/reasoning 类: "does not support thinking" / "does not support reasoning"。
# 其余（unknown parameter / unexpected keyword / invalid request argument / extra fields not permitted）保持裸短语。
# M8：状态码放行 400/422（pydantic 网关）。
_PARAM_REJECTION_PATTERNS = (
    "unrecognized request argument",
    "unrecognized parameter",
    "unknown parameter",
    "unexpected keyword",
    "invalid request argument",
    "extra fields not permitted",
    "does not support thinking",
    "does not support reasoning",
    # 强制 tool_choice 被 thinking 模式拒绝的实测文案（DeepSeek anthropic 兼容层）：
    # "Thinking mode does not support this tool_choice"
    "does not support this tool_choice",
)

# Anthropic invalid_request_error 覆盖过广（消息格式错误等无关 400 也用该 type），
# 须与扩展参数关键字组合才判定为参数拒绝。
_PARAM_KEYWORDS = ("thinking", "reasoning", "budget_tokens")
_PARAM_REJECTION_STATUSES = (400, 422)


def _is_param_rejection(e: Exception) -> bool:
    """识别"端点不支持某请求参数"类错误（区别于鉴权/格式错误）。"""
    if not isinstance(e, httpx.HTTPStatusError):
        return False
    resp = getattr(e, "response", None)
    if resp is None or resp.status_code not in _PARAM_REJECTION_STATUSES:
        return False
    text = (resp.text or "").lower()
    if any(p in text for p in _PARAM_REJECTION_PATTERNS):
        return True
    # 复合判定：Anthropic invalid_request_error + 扩展参数关键字
    return "invalid_request_error" in text and any(k in text for k in _PARAM_KEYWORDS)


# 端点拒绝 stream 参数的响应特征（大小写不敏感子串匹配）。
# 与 _PARAM_REJECTION_PATTERNS 同风格，但语义更特定：命中后应整体切换非流式兜底，
# 而非仅降级扩展参数。故判定顺序须先于 _is_param_rejection——例如
# "unknown parameter: stream" 同时命中通用模式，但走 fallback 才是正确行为。
_STREAM_REJECTION_PATTERNS = (
    "stream is not supported",
    "streaming is not supported",
    "stream not supported",
    "does not support stream",
    "unsupported parameter: stream",
    "unknown parameter: stream",
    "stream: not supported",
    # M1：网关文案 "unrecognized parameter 'stream' is not supported"
    # 注意保留闭合引号，避免误伤 stream_options 等同前缀参数
    "unrecognized parameter 'stream'",
    'unrecognized parameter "stream"',
)


def _is_stream_rejection(e: Exception) -> bool:
    """识别"端点不支持 stream 参数"类错误（区别于普通扩展参数拒绝）。

    仅对 400/422（网关参数校验类状态码）生效；命中后调用方应将 provider 标记为
    ``_stream_unsupported`` 并切换非流式兜底。
    """
    if not isinstance(e, httpx.HTTPStatusError):
        return False
    resp = getattr(e, "response", None)
    if resp is None or resp.status_code not in _PARAM_REJECTION_STATUSES:
        return False
    text = (resp.text or "").lower()
    return any(p in text for p in _STREAM_REJECTION_PATTERNS)


# M7/M9：确定性错误 —— 重试无意义（refusal / 鉴权 / 参数类 / 不存在）
_TERMINAL_STATUSES = (400, 401, 403, 404, 422)


def _is_terminal_error(e: Exception) -> bool:
    """refusal（ValueError）与确定性 4xx 不重试；其余（429/5xx/超时）可重试。"""
    if isinstance(e, ValueError):
        return True  # 解析失败/refusal，重试结果不变
    if isinstance(e, httpx.HTTPStatusError):
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        if status in _TERMINAL_STATUSES:
            # 参数类 400/422 由调用方先走降级路径，此处仅兜底其它确定性 4xx
            return not _is_param_rejection(e)
    return False


class LLMCallError(Exception):
    """LLM 调用失败（重试耗尽或确定性错误）。

    ``retryable`` 指示调用方是否可以安全重试：
    - ``True``：429/5xx/超时类故障，下个调度周期重试可能恢复。
    - ``False``：401/403/400/参数类/refusal 等确定性错误，重试无意义。
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _retry_delay(e: Exception, fallback: int) -> int:
    """429 优先读取 Retry-After（秒）；其余用固定退避。

    顺手项 2：Retry-After 钳制到 60s 上限，避免恶意/异常端点返回超大值导致
    请求长时间挂起（退避本就只用于吸收短暂限流，过长无收益）。
    """
    _RETRY_AFTER_CAP = 60
    if isinstance(e, httpx.HTTPStatusError):
        resp = getattr(e, "response", None)
        if resp is not None and getattr(resp, "status_code", None) == 429:
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                return min(int(ra), _RETRY_AFTER_CAP)
    return fallback


def _build_provider(
    provider: str, cfg: dict[str, object], proxy: str | None
) -> BaseProvider:
    cls = _PROVIDER_MAP.get(provider)
    if cls is None:
        supported = ", ".join(_PROVIDER_MAP)
        raise ValueError(
            f"Unsupported LLM provider '{provider}'. Supported: {supported}"
        )
    kwargs: dict[str, object] = {
        "api_base": cfg["api_base"],
        "api_key": cfg["api_key"],
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
        "timeout": cfg["timeout"],
        "proxy": proxy,
        # 各 provider 构造函数均接受 thinking_level
        # （openai_compat / openai_responses 映射 reasoning_effort）
        "thinking_level": cfg.get("thinking_level", "off"),
    }
    return cls(**kwargs)


@dataclass
class _StreamRunState:
    """stream_chat 单次运行的跨生成器状态。

    - ``used_fallback``：本次是否走了非流式兜底。兜底路径的落库由
      ``_call_with_retry`` 负责，stream_chat 据此跳过自身 ``_log_success``，
      避免同一调用被重复计费。
    - ``model`` / ``latency_ms``：成功调用后回填，供 ``_log_success`` 与
      ``chat()`` 组装 ChatResponse 使用（流式事件本身不携带 model/latency）。
    """

    used_fallback: bool = False
    model: str = ""
    latency_ms: int = 0


class LLMClient:
    """LLM 客户端单例（含重试逻辑与用量日志记录）。

    Provider 选择由 [llm] 配置节中的 ``provider`` 键驱动
    （默认 ``"openai_compat"``）。
    """

    MAX_RETRIES = 2
    RETRY_BACKOFF: list[int] = [1, 3]  # 秒

    def __init__(self) -> None:
        cfg = config_manager.get_llm_config()
        proxy = config_manager.get("dev", "script_proxy", fallback="").strip() or None
        self._provider_name = cfg["provider"]
        self._provider = _build_provider(self._provider_name, cfg, proxy)

    async def chat(  # noqa: PLR0913
        self,
        messages: list[Message],
        *,
        job_id: int | None = None,
        job_name: str | None = None,
        **kwargs,
    ) -> ChatResponse:
        """发送聊天请求（底层统一走流式），记录用量到数据库。

        实现上等价于"消费 stream_chat() 的事件流并聚合"：所有调用统一走
        ``provider.stream()``，非流式仅作端点不支持 stream 时的自动兜底。
        对现有调用方保持返回 ChatResponse 的契约不变。

        Args:
            messages: 对话消息列表。
            job_id: 可选的 job 标识符，用于用量追踪。
            job_name: 可选的 job 名称，用于用量追踪。
            **kwargs: provider 特定的覆盖参数（temperature、max_tokens 等）。

        Returns:
            成功时返回 ChatResponse。

        Raises:
            LLMCallError: 所有重试耗尽或遇到确定性错误（401/403/refusal 等）。
                ``retryable`` 标志指示调用方是否可安全重试。
        """
        state = _StreamRunState()
        # S4：chat() 复用 stream_chat() 内部已聚合的结果，避免同一响应聚合两次。
        # 不污染 _StreamRunState 契约（其可为外部 SummaryStreamResult 鸭子类型），
        # 故聚合结果经独立私有容器回传。
        holder: dict[str, ChatResponse | None] = {"response": None}
        async for _ in self.stream_chat(
            messages,
            job_id=job_id,
            job_name=job_name,
            _state=state,
            _result=holder,
            **kwargs,
        ):
            # 事件流的聚合与落库均由 stream_chat() 内部完成，此处仅驱动至耗尽。
            pass
        response = holder["response"]
        if response is not None:
            return response
        # 防御兜底：正常耗尽/兜底路径均应回填聚合结果；走到此处说明内部契约被破坏
        # （重跑会二次发起 API 调用），故显式报错暴露该悬垂分支。
        logger.error("chat() 未取得聚合响应：stream_chat 未按契约回填聚合结果")
        raise LLMCallError(
            "stream_chat 未回填聚合响应（内部契约异常）", retryable=False
        )

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        job_id: int | None = None,
        job_name: str | None = None,
        _state: _StreamRunState | None = None,
        _result: dict[str, ChatResponse | None] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式聊天入口：逐条产出归一化事件，正常耗尽后落库一次。

        Args:
            messages: 对话消息列表。
            job_id: 可选的 job 标识符，用于用量追踪。
            job_name: 可选的 job 名称，用于用量追踪。
            _state: 内部使用——供 ``chat()`` 回读 model/latency 与 fallback 标记。
            _result: 内部使用——单元素容器，正常耗尽/兜底成功时回填聚合后的
                ``ChatResponse``，供 ``chat()`` 复用（S4：避免重复聚合）。
            **kwargs: provider 特定的覆盖参数。

        Yields:
            provider 无关的归一化流式事件 StreamChunk。

        Raises:
            LLMCallError: 重试耗尽/确定性错误/已产出内容后中断（透传给消费方）。
        """
        state = _state if _state is not None else _StreamRunState()
        state.model = kwargs.get("model") or getattr(self._provider, "model", "")
        aggregator = StreamAggregator()
        completed = False
        inner = self._stream_with_retry(
            messages,
            job_id=job_id,
            job_name=job_name,
            state=state,
            result=_result,
            **kwargs,
        )
        try:
            async for chunk in inner:
                aggregator.feed(chunk)
                yield chunk
            completed = True
        finally:
            if not completed:
                # 消费方提前 aclose / 异常中断：关闭内层流并记 warning，不落成功。
                try:
                    await inner.aclose()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不应掩盖原异常
                    logger.warning(f"关闭 LLM 流生成器时出错: {exc}")
                logger.warning(
                    "LLM 流式调用未正常耗尽（消费方提前关闭或异常中断），跳过成功落库"
                )
            elif not state.used_fallback:
                # fallback 路径的落库已由 _call_with_retry 完成，此处不重复。
                response = aggregator.finalize()
                response.model = state.model
                response.latency = state.latency_ms
                self._publish_result(_result, response)  # S4：供 chat() 复用聚合结果
                self._log_success(response, job_id=job_id, job_name=job_name)
                logger.debug(
                    f"LLM stream call: model={response.model} "
                    f"tokens={response.usage.total_tokens if response.usage else 0} "
                    f"latency={response.latency}ms"
                )

    async def _stream_with_retry(
        self,
        messages: list[Message],
        *,
        job_id: int | None = None,
        job_name: str | None = None,
        state: _StreamRunState,
        result: dict[str, ChatResponse | None] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用的重试/降级/兜底包装（唯一底层实现 = provider.stream）。

        决策树（``started`` 表示是否已产出首个 chunk）：

        - 首 chunk 前失败：可重试/降级——
          stream 拒绝 → 标记 provider 并切非流式兜底；
          参数拒绝 → 置降级标记后立即重试一次；
          429/5xx/连接超时 → 退避重试（MAX_RETRIES）；
          确定性 4xx/refusal → 终态。
        - 首 chunk 后失败：一律不重试（已产出内容不回滚），包装为
          ``LLMCallError(retryable=True)`` 抛出。

        ``result`` 为可选的单元素容器，兜底成功时回填聚合结果供 ``chat()`` 复用。
        """
        provider = self._provider
        if getattr(provider, "_stream_unsupported", False):
            # 端点此前已标记不支持 stream：后续调用直接走非流式兜底
            logger.debug("LLM 端点已标记不支持流式，直接走非流式兜底")
            state.used_fallback = True
            async for chunk in self._fallback_stream(
                messages,
                job_id=job_id,
                job_name=job_name,
                state=state,
                result=result,
                **kwargs,
            ):
                yield chunk
            return

        last_error: Exception | None = None
        t_attempt = time.time()
        attempt = 0
        extras_degraded = False  # 参数类 400 降级只做一次，不计入退避次数
        started = False

        while attempt <= self.MAX_RETRIES:
            source = provider.stream(messages, **kwargs)
            try:
                async for chunk in source:
                    started = True
                    yield chunk
                state.latency_ms = int((time.time() - t_attempt) * 1000)
                return
            except Exception as e:  # noqa: BLE001 - 统一按重试/终态策略处理
                last_error = e
                if isinstance(e, LLMCallError):
                    # S1：确定性 LLMCallError（如 fallback 透传）不套用重试策略，
                    # 保留其 retryable 原样抛出，避免确定性失败被放大
                    logger.debug(
                        "LLM stream 收到 LLMCallError，直接透传（不重试）: "
                        f"{_format_error_detail(e)}"
                    )
                    raise
                if started:
                    # 已产出内容：不回滚、不重试，包装为可重试错误抛出
                    latency_ms = int((time.time() - t_attempt) * 1000)
                    error_detail = _format_error_detail(e)
                    logger.error(
                        f"LLM stream 中断（已产出内容，不再重试）: {error_detail}"
                    )
                    self._log_error(
                        error_detail,
                        job_id=job_id,
                        job_name=job_name,
                        latency_ms=latency_ms,
                    )
                    raise LLMCallError(error_detail, retryable=True) from e

                # 首 chunk 前失败：按语义重试/降级
                if not extras_degraded and _is_stream_rejection(e):
                    provider._stream_unsupported = True
                    state.used_fallback = True
                    logger.warning(
                        "LLM 端点不支持流式，降级为非流式兜底: "
                        f"{_format_error_detail(e)}"
                    )
                    async for chunk in self._fallback_stream(
                        messages,
                        job_id=job_id,
                        job_name=job_name,
                        state=state,
                        result=result,
                        **kwargs,
                    ):
                        yield chunk
                    return
                if not extras_degraded and _is_param_rejection(e):
                    extras_degraded = True
                    self._degrade_extras(e)
                    # 降级重试前重置计时，避免把首次失败请求耗时计入 latency
                    t_attempt = time.time()
                    continue
                # 已降级后再次命中参数类拒绝 → 该端点始终不支持该参数 → 终态
                if extras_degraded and _is_param_rejection(e):
                    break
                if _is_terminal_error(e):
                    break
                if attempt < self.MAX_RETRIES:
                    delay = _retry_delay(e, self.RETRY_BACKOFF[attempt])
                    logger.warning(
                        f"LLM stream retry {attempt + 1}/{self.MAX_RETRIES} "
                        f"after {delay}s: {_format_error_detail(e)}"
                    )
                    await asyncio.sleep(delay)
                attempt += 1
                t_attempt = time.time()
            finally:
                # 显式关闭上游生成器，避免 GeneratorExit/中断时 httpx 流泄漏。
                # S2：防御 source 非 async generator（无 aclose）时 AttributeError。
                aclose = getattr(source, "aclose", None)
                if aclose is not None:
                    await aclose()
                else:
                    logger.debug(
                        "LLM stream source 无 aclose 方法，跳过显式关闭: %s",
                        type(source).__name__,
                    )

        # 所有重试耗尽 —— 记录错误并抛 LLMCallError
        latency_ms = int((time.time() - t_attempt) * 1000)
        error_detail = _format_error_detail(last_error) if last_error else "unknown"
        logger.error(
            f"LLM stream failed after {self.MAX_RETRIES} retries: {error_detail}"
        )
        self._log_error(
            error_detail,
            job_id=job_id,
            job_name=job_name,
            latency_ms=latency_ms,
        )
        retryable = not _is_terminal_error(last_error)
        if (
            extras_degraded
            and last_error is not None
            and _is_param_rejection(last_error)
        ):
            retryable = False
        raise LLMCallError(error_detail, retryable=retryable) from last_error

    async def _fallback_stream(
        self,
        messages: list[Message],
        *,
        job_id: int | None = None,
        job_name: str | None = None,
        state: _StreamRunState | None = None,
        result: dict[str, ChatResponse | None] | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """非流式兜底：调用 ``_call_with_retry``（含其重试/降级/落库）后，
        把 ChatResponse.blocks 按顺序包装为等价事件序列。

        落库由 ``_call_with_retry`` 负责；调用方据 ``state.used_fallback``
        跳过自身落库，避免重复计费。``result`` 非空时回填聚合响应供 ``chat()``
        复用（S4）。
        """
        response = await self._call_with_retry(
            messages, job_id=job_id, job_name=job_name, **kwargs
        )
        if state is not None:
            # 回填实际 model/latency，供 chat() 组装响应
            state.model = response.model or state.model
            state.latency_ms = response.latency
        self._publish_result(result, response)
        for chunk in self._response_to_chunks(response):
            yield chunk

    @staticmethod
    def _publish_result(
        result: dict[str, ChatResponse | None] | None, response: ChatResponse
    ) -> None:
        """把聚合后的 ChatResponse 写入单元素容器（S4：供 chat() 复用）。"""
        if result is None:
            logger.debug("stream_chat 未提供 result 容器，跳过聚合结果回填")
            return
        result["response"] = response

    @staticmethod
    def _response_to_chunks(response: ChatResponse) -> list[StreamChunk]:
        """ChatResponse.blocks → 等价 StreamChunk 序列（fallback 兜底用）。"""
        chunks: list[StreamChunk] = []
        blocks = list(response.blocks)
        if not blocks and response.content:
            # 防御兜底：provider 仅填 content 未填 blocks 时，仍还原文本
            logger.debug("ChatResponse 无 blocks，按 content 还原 text_delta")
            blocks = [TextBlock(text=response.content)]
        for block in blocks:
            if isinstance(block, TextBlock):
                chunks.append(StreamChunk(type="text_delta", text=block.text))
            elif isinstance(block, ThinkingBlock):
                chunks.append(
                    StreamChunk(
                        type="thinking_delta",
                        thinking=block.thinking,
                        signature=block.signature or "",
                    )
                )
            elif isinstance(block, ToolUseBlock):
                chunks.append(
                    StreamChunk(
                        type="tool_use_start",
                        tool_use_id=block.id,
                        tool_name=block.name,
                    )
                )
                chunks.append(
                    StreamChunk(
                        type="tool_use_delta",
                        tool_use_id=block.id,
                        partial_json=json.dumps(block.input, ensure_ascii=False),
                    )
                )
            else:
                # 防御兜底：未知 block 类型（如 RedactedThinkingBlock）跳过并记录
                logger.warning(
                    "fallback 遇到未知 block 类型，已跳过: %r", type(block).__name__
                )
        if response.usage is not None:
            chunks.append(StreamChunk(type="usage", usage=response.usage))
        chunks.append(StreamChunk(type="stop", stop_reason=response.stop_reason))
        return chunks

    def _degrade_extras(self, e: Exception) -> None:
        """参数类 400/422 降级：置位 provider 的扩展参数/强制 tool_choice 标记。"""
        resp_text = (getattr(getattr(e, "response", None), "text", "") or "").lower()
        if "tool_choice" in resp_text and hasattr(
            self._provider, "_force_tool_choice_degraded"
        ):
            # 专用降级：部分端点（thinking 模式）拒绝强制工具选择，
            # 仅降级 tool_choice，保留 thinking 等其它参数
            self._provider._force_tool_choice_degraded = True
            logger.warning(
                "LLM endpoint rejected forced tool_choice, "
                f"degraded to auto: {_format_error_detail(e)}"
            )
        if hasattr(self._provider, "_extras_disabled"):
            self._provider._extras_disabled = True
        logger.warning(
            "LLM endpoint rejected extra params, "
            f"degraded retry without them: {_format_error_detail(e)}"
        )

    async def _call_with_retry(
        self,
        messages: list[Message],
        *,
        job_id: int | None = None,
        job_name: str | None = None,
        **kwargs,
    ) -> ChatResponse:
        """非流式调用（含重试/降级/落库），作为流式不可用时的兜底路径。"""
        last_error: Exception | None = None
        # L8：latency 只计量成功那次请求（不含退避睡眠墙钟）
        t_attempt = time.time()
        attempt = 0
        extras_degraded = False  # 参数类 400 降级只做一次，不计入退避次数

        while attempt <= self.MAX_RETRIES:
            try:
                response = await self._provider.chat(messages, **kwargs)
                latency_ms = int((time.time() - t_attempt) * 1000)
                response.latency = latency_ms
                self._log_success(response, job_id=job_id, job_name=job_name)
                logger.debug(
                    f"LLM call: model={response.model} "
                    f"tokens={response.usage.total_tokens if response.usage else 0} "
                    f"latency={response.latency}ms"
                )
                return response
            except Exception as e:
                last_error = e
                if isinstance(e, LLMCallError):
                    # S1：冒泡的确定性 LLMCallError 不重试，原样透传（保留 retryable）
                    logger.debug(
                        "LLM 非流式兜底收到 LLMCallError，直接透传（不重试）: "
                        f"{_format_error_detail(e)}"
                    )
                    raise
                # 双重保险第二道：端点拒绝扩展参数（thinking/reasoning 等）→
                # 置位降级标记后立即重试（该 provider 实例生命周期内不再发送）
                if not extras_degraded and _is_param_rejection(e):
                    extras_degraded = True
                    self._degrade_extras(e)
                    # 顺手项 1：降级重试前重置计时，避免把首次失败请求的耗时计入 latency
                    t_attempt = time.time()
                    continue
                # 已降级后再次命中参数类拒绝 → 该端点始终不支持该参数 → 终态
                if extras_degraded and _is_param_rejection(e):
                    break
                # M7/M9：确定性错误（refusal/鉴权/参数类）不重试
                if _is_terminal_error(e):
                    break
                if attempt < self.MAX_RETRIES:
                    delay = _retry_delay(e, self.RETRY_BACKOFF[attempt])
                    logger.warning(
                        f"LLM retry {attempt + 1}/{self.MAX_RETRIES} "
                        f"after {delay}s: {_format_error_detail(e)}"
                    )
                    await asyncio.sleep(delay)
                attempt += 1
                t_attempt = time.time()  # L8：重试后重新计时

        # 所有重试耗尽 —— 记录错误并抛 LLMCallError（不再返回空响应伪装成功）
        latency_ms = int((time.time() - t_attempt) * 1000)
        error_detail = _format_error_detail(last_error) if last_error else "unknown"
        logger.error(
            f"LLM call failed after {self.MAX_RETRIES} retries: {error_detail}"
        )
        self._log_error(
            error_detail,
            job_id=job_id,
            job_name=job_name,
            latency_ms=latency_ms,
        )
        retryable = not _is_terminal_error(last_error)
        # 已降级后再次命中参数类拒绝 → 该端点始终不支持该参数，重试无意义 → 终态
        if (
            extras_degraded
            and last_error is not None
            and _is_param_rejection(last_error)
        ):
            retryable = False
        raise LLMCallError(error_detail, retryable=retryable) from last_error

    def _log_success(
        self,
        response: ChatResponse,
        job_id: int | None = None,
        job_name: str | None = None,
    ) -> None:
        """记录成功 LLM 调用（从 ChatResponse 提取用量信息）。"""
        try:
            from app.core.database import database_manager

            usage = response.usage
            database_manager.llm_usage.log_usage(
                job_id=job_id,
                job_name=job_name or "",
                model=response.model,
                provider=self._provider_name,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
                latency_ms=response.latency,
                status="success",
            )
        except Exception as e:
            logger.error(f"Failed to log LLM usage: {e}")

    def _log_error(
        self,
        error_message: str,
        job_id: int | None = None,
        job_name: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        """记录失败 LLM 调用（model 取自配置）。"""
        try:
            from app.core.database import database_manager

            database_manager.llm_usage.log_usage(
                job_id=job_id,
                job_name=job_name or "",
                model=config_manager.get_llm_config()["model"],
                provider=self._provider_name,
                latency_ms=latency_ms,
                status="error",
                error_message=error_message,
            )
        except Exception as e:
            logger.error(f"Failed to log LLM usage: {e}")


# 模块级单例 ----------------------------------------------------------------


_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """返回模块级 LLMClient 单例，首次调用时创建。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def reset_llm_client() -> None:
    """重置 LLM 单例，使下次调用 get_llm_client 时用最新配置重建。"""
    global _llm_client
    _llm_client = None

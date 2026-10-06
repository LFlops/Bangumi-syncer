"""轻量 Agent 循环。

通用骨架核心：固定 ``max_iterations`` 上限的 for 循环，每轮经注入的 LLM 调用
（**流式为主** ``stream_fn``，``chat_fn`` 为旧契约兼容）取响应，按既定流程处理终止、
聚合、分段并行、透明预算与 stop_reason。

设计要点：
- **不直接依赖 LLMClient**：LLM 调用经注入的 ``stream_fn(messages, tools=, tool_choice=)``
  （返回 ``AsyncIterator[StreamChunk]``），便于测试 mock 与场景层（llm_assist）注入真实
  客户端。``chat_fn``（返回 ``ChatResponse``）为旧契约兼容路径，行为与改造前等价。
- **流式解析 + 提交闸门 + 受控执行**（``stream_fn`` 路径）：逐事件喂
  :class:`~app.services.llm.models.StreamAggregator` 聚合响应，同时喂
  :class:`~app.services.agent.streaming_tool_executor.StreamingToolExecutor`。
  停点时序因协议而异：anthropic/responses **逐工具停点** → 幂等工具在参数停点
  （``tool_use_stop``）到达时**提前启动并与生成重叠**；openai_compat 在
  ``finish_reason`` **流末集中补发**停点 → 仍提前执行但**无重叠收益**；eval replay
  **无停点** → 纯轮级降级。非幂等工具在流结束后按 ``execute_batch`` 语义（连续幂等段
  并行、非幂等串行、保序）执行。执行器由 ``executor_factory`` 按轮构造。
- **旧路径兼容**：``chat_fn`` + ``tool_calls_fn`` 注入时行为与改造前**完全一致**
  （整批交给 ``execute_batch``，按独立结果槽位回填）。
- **终止工具优先**：本轮含 ``tool_choice_terminal`` 工具 → 捕获参数即 break，同轮其他工具不执行。
  流式路径下同轮已提前启动的幂等工具可能已执行完成，其结果保留但走终止分支时被忽略
  （无害；terminal 抑制保证后续停点不再启动新任务）。
- **透明预算**：每轮递减后仅当仍有后续轮次（remaining>0）才追加预算消息；
  remaining>1 为朴素 ``[剩余轮次：N]``，remaining==1 为强化文案
  ``FINAL_ROUND_BUDGET_MESSAGE``（明确要求给出结论、禁止再检索）。末轮（remaining==1 起手）
  强制 ``tool_choice=terminal``。末轮不再产生剩余 0 的幻影预算消息。
- **兜底收尾**：for 循环自然结束（即将返回 exhausted）时追加**一次**收尾 LLM 调用
  （``FINAL_RECOVERY_MESSAGE``，tools 仅 terminal schema、tool_choice=terminal），
  best-effort 争取明确结论；不执行任何非终止工具，异常时降级为 exhausted，仅调用一次。
- **预算钩子**：非末轮预算消息与收尾提示生成后调用 ``recorder.record_budget(...)``，
  由 recorder 内部决定并入哪条 span（同轮最后 tool_execute 或回退 llm_chat）。
  ``recorder=None`` 时整体跳过（可空实现）。

不引入 token/wall-time 预算系统，只做轮次上限。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.services.llm.models import (
    ChatResponse,
    ContentBlock,
    Message,
    StreamAggregator,
    StreamChunk,
    ToolResultBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    # 仅为类型别名/签名提供具体批量结果类型；不在运行时依赖 tools，保持通用骨架解耦。
    from app.services.llm.tools import BatchResults

logger = logging.getLogger(__name__)

# 末轮（remaining==1）强化提示：思考模型在强制 tool_choice=terminal 被供应商降级为
# auto 时容易不提交，明确要求给出结论（含放弃）并禁止再检索。
FINAL_ROUND_BUDGET_MESSAGE = (
    "[剩余轮次：1（最后一轮）] 必须调用 submit_suggestion 给出结论："
    "若已确定推荐条目则提交 subject_id 与理由；若确实无法确定，也要调用并说明放弃理由。"
    "不得再调用其他检索工具。"
)

# 兜底收尾：for 循环自然结束（即将 exhausted）时追加一次收尾调用，best-effort
# 争取一个明确结论；该消息须经 recorder 预算通道记录，保证恢复重放一致性。
FINAL_RECOVERY_MESSAGE = (
    "[最终收尾] 轮次预算已耗尽。请立即调用 submit_suggestion 给出结论："
    "若已确定则提交 subject_id 与理由；若确实无法确定也请调用并说明放弃理由。"
)


@dataclass
class RunResult:
    """一轮 Agent 会话的终态结果。

    - ``stop_reason``：end_turn / submit_suggestion / exhausted（统一结束原因）
    - ``text``：end_turn 时 LLM 的纯文本输出
    - ``suggestion``：submit_suggestion 捕获的参数 dict（subject_id/reason）
    - ``last_response``：末轮 ChatResponse（exhausted 兜底供 output_parser 解析）
    """

    stop_reason: str
    text: str = ""
    suggestion: dict | None = None
    last_response: ChatResponse | None = None


class StreamingExecutor(Protocol):
    """流式工具执行器协议。

    本地声明（不 import 具体 ``StreamingToolExecutor``），保持通用骨架对执行器实现的
    运行时解耦；返回值需实现 ``feed`` / ``finalize``。
    """

    def feed(self, chunk: StreamChunk) -> None: ...

    async def finalize(self) -> list[ToolResultBlock]: ...


class BudgetRecorder(Protocol):
    """预算与工具 span 记录器协议。

    本地声明（不 import 具体 ``TraceRecorder``），避免通用骨架与 recorder 循环依赖；
    签名与 ``recorder.TraceRecorder`` 一致。
    """

    def record_budget(self, budget_message: str) -> None: ...

    def start_tool(self, tool_use: ToolUseBlock, *, sequence: int) -> str | None: ...

    def end_tool(
        self,
        span_id: str,
        *,
        result: ToolResultBlock | None = None,
        error: str = "",
    ) -> None: ...


# 注入的函数类型（仅做文档化提示，运行时不强制）
#: .. deprecated:: 旧契约兼容。``ChatFn`` 仅测试/迁移期使用，后续清理时移除；
#: 主路径请用 :data:`StreamFn`。
ChatFn = Callable[..., Awaitable[ChatResponse]]
StreamFn = Callable[..., AsyncIterator[StreamChunk]]
#: .. deprecated:: 旧契约兼容。``ToolCallsFn`` 仅测试/迁移期使用，后续清理时移除；
#: 主路径请用 ``executor_factory`` + ``StreamingToolExecutor``。
ToolCallsFn = Callable[[list[ToolUseBlock]], Awaitable["BatchResults | dict[str, Any]"]]
#: 按轮构造流式工具执行器（返回值需实现 ``StreamingExecutor``）
ExecutorFactory = Callable[[], StreamingExecutor]


async def _consume_stream(
    stream_fn: StreamFn,
    messages: list[Message],
    *,
    tools: list[dict] | None,
    tool_choice: str | None,
    executor_factory: ExecutorFactory | None,
) -> tuple[ChatResponse, StreamingExecutor | None]:
    """消费一轮流：聚合响应，并把工具事件喂给执行器（若注入）。

    返回 ``(resp, executor)``；``executor_factory=None`` 时不构造执行器（纯聚合，
    用于收尾调用等不执行工具的场景）。

    异常安全：执行器**先于消费构造**，流中途异常（httpx 超时/断连等）时先
    ``await asyncio.shield(executor.finalize())`` 收敛已提前启动的任务（避免孤儿后台
    任务与 "exception never retrieved"、执行器引用丢失无法收敛），再向上抛出原异常。
    收敛经 ``shield`` 保护：外层协程被取消（关机/取消竞态）时收敛动作仍在后台完成。
    收敛自身失败（含取消类 ``BaseException``）只记 warning，**绝不掩盖原始异常**。
    """
    aggregator = StreamAggregator()
    executor = executor_factory() if executor_factory is not None else None
    try:
        async for chunk in stream_fn(messages, tools=tools, tool_choice=tool_choice):
            aggregator.feed(chunk)
            if executor is not None:
                executor.feed(chunk)
    except BaseException:
        if executor is not None:
            try:
                # finalize 内部 gather(return_exceptions=True) 收敛提前任务。
                # shield：外层被取消时收敛动作仍继续在后台完成（避免任务重新成孤儿）。
                await asyncio.shield(executor.finalize())
            except BaseException as e:
                # 兜住 Exception 与取消类/致命异常；绝不掩盖原始流异常。
                # 打印异常类型名：CancelledError 的 str(e) 为空，否则日志尾部只剩冒号空白，
                # 无法区分「被取消」与「finalize 真失败」。
                logger.warning(
                    "流异常后收敛提前执行任务未完成（可能被取消）: %s",
                    type(e).__name__,
                )
        raise
    return aggregator.finalize(), executor


def _extract_tool_calls(resp: ChatResponse) -> list[ToolUseBlock]:
    """从 ChatResponse.blocks 中提取全部 ToolUseBlock（同轮工具调用顺序固定）。"""
    return [b for b in resp.blocks if isinstance(b, ToolUseBlock)]


def _align_results(
    tool_calls: list[ToolUseBlock], results: BatchResults | dict[str, Any]
) -> list[ToolResultBlock | None]:
    """把执行器返回值对齐为与 ``tool_calls`` 一一对应的结果槽位列表。

    - 执行器返回 ``BatchResults``（带 ``ordered`` 槽位）→ 按槽位取，重复 tool_use_id
      的每条 tool_use 各取自身结果（首个真实结果不被 duplicate 错误块覆盖）
    - 普通 ``dict[tool_use_id, result]``（旧契约 / 注入的简易执行器）→ 回退按 id 取值
    - 逐元素归一化：仅接受 ``ToolResultBlock``，其余（缺失 / 异常泄漏值）归一为
      ``None``；返回元素类型为具体联合，避免 ``list[None]`` / ``list[Unknown]``
      在不变性位置被更严格的类型检查器拒绝
    """
    aligned: list[ToolResultBlock | None] = []
    ordered = getattr(results, "ordered", None)
    if isinstance(ordered, list) and len(ordered) == len(tool_calls):
        if all(oid == tc.id for (oid, _), tc in zip(ordered, tool_calls, strict=True)):
            for _, result in ordered:
                aligned.append(result if isinstance(result, ToolResultBlock) else None)
            return aligned
        logger.debug("ordered 槽位与 tool_calls 不一致，回退 dict 取值")
    getter = getattr(results, "get", None)
    if getter is None:
        aligned.extend([None] * len(tool_calls))
        return aligned
    for tc in tool_calls:
        item = getter(tc.id)
        aligned.append(item if isinstance(item, ToolResultBlock) else None)
    return aligned


def _pad_aligned(
    tool_calls: list[ToolUseBlock], aligned: Sequence[ToolResultBlock | None]
) -> list[ToolResultBlock | None]:
    """把执行器结果槽位补齐到与 ``tool_calls`` 等长，闭合会话协议。

    防御协议破坏（``finalize`` 返回槽位少于 ``tool_calls`` 或顺序错乱）：按
    ``tool_use_id`` 贪心匹配现有结果，未匹配到的 tool_use 以 ``slot mismatch``
    错误块回填。若直接 ``zip(strict=False)`` 截断，多出的 tool_use 会缺少配对
    tool_result，下一轮真实端点可能 400。
    """
    remaining = [r for r in aligned if isinstance(r, ToolResultBlock)]
    patched: list[ToolResultBlock | None] = []
    for tc in tool_calls:
        match_idx = next(
            (i for i, r in enumerate(remaining) if r.tool_use_id == tc.id), None
        )
        if match_idx is not None:
            patched.append(remaining.pop(match_idx))
            continue
        # 防御兜底：该 tool_use 无对应执行结果（协议破坏）→ 以错误块闭合，不静默
        logger.warning(
            "执行器结果缺少 tool_use_id=%s 的槽位，以 slot mismatch 错误块回填",
            tc.id,
        )
        patched.append(
            ToolResultBlock(tool_use_id=tc.id, content="slot mismatch", is_error=True)
        )
    return patched


def _inject_veto_hint(
    messages: list[Message], terminal_tc: ToolUseBlock, hint: str
) -> None:
    """veto 暂缓：注入与 terminal tool_use 配对的 tool_result（闭合会话协议）。

    OpenAI/Anthropic 协议要求 assistant 的 tool_use 后必须跟配对 tool_result，
    不能只注入 user 文本。
    """
    messages.append(
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=terminal_tc.id, content=hint, is_error=False
                )
            ],
        )
    )


def _record_veto_tool(
    recorder: BudgetRecorder | None,
    terminal_tc: ToolUseBlock,
    hint: str,
    *,
    sequence: int,
) -> None:
    """把 veto 伪执行落 tool_execute span，保证 replay 重建的消息与 live 一致。

    ``sequence`` 与 ``execute_batch`` 的约定一致：工具在 ``tool_calls`` 中的下标。
    ``recorder`` 为 None 时跳过（可空实现）。
    """
    if recorder is None:
        return
    span_id = recorder.start_tool(terminal_tc, sequence=sequence)
    if span_id is None:
        # 记录器选择不落 span（如可空实现）：仍需可观测，避免静默
        logger.debug(
            "veto terminal 工具 %s 未获得 span_id，跳过 replay 落 span",
            terminal_tc.name,
        )
        return
    recorder.end_tool(
        span_id,
        result=ToolResultBlock(
            tool_use_id=terminal_tc.id, content=hint, is_error=False
        ),
    )


async def run(
    *,
    tools_schemas: list[dict],
    max_iterations: int,
    tool_choice_terminal: str,
    seed_messages: Sequence[Message],
    stream_fn: StreamFn | None = None,
    executor_factory: ExecutorFactory | None = None,
    chat_fn: ChatFn | None = None,
    tool_calls_fn: ToolCallsFn | None = None,
    recorder: BudgetRecorder | None = None,
    veto_terminal: Callable[[dict], str | None] | None = None,
) -> RunResult:
    """运行轻量 Agent 循环，返回 ``RunResult``。

    参数（除 spec 约定的 seed_messages 外，全部经注入解耦，零 LLMClient 依赖）：

    - ``stream_fn``：**主路径**，``(messages, tools=, tool_choice=) -> AsyncIterator[StreamChunk]``。
      流式解析 + 提交闸门 + 受控执行（幂等工具停点提前执行）。
    - ``executor_factory``：主路径按轮构造 ``StreamingToolExecutor`` 的工厂（零参可调用）。
      为 ``None`` 时主路径退化为纯聚合（不执行工具，供不使用工具的调用方）。
    - ``chat_fn``：**旧契约兼容路径**，``(messages, tools=, tool_choice=) -> ChatResponse``。
      与 ``tool_calls_fn`` 搭配时行为与改造前完全一致。

      .. deprecated:: 仅测试/迁移期兼容，后续清理时移除；新代码请用 ``stream_fn``。
    - ``tools_schemas``：传给 provider 的 tools 参数（schema 列表）
    - ``tool_calls_fn``：旧路径批量执行器，返回 ``BatchResults``（``ordered`` 与 tool_calls
      一一对应的结果槽位；同时兼容 ``{tool_use_id: ToolResultBlock | TerminalCapture}`` 的
      dict 视图）

      .. deprecated:: 仅测试/迁移期兼容，后续清理时移除；新代码请用
         ``executor_factory`` + ``StreamingToolExecutor``。
    - ``max_iterations``：轮次上限（由 budget 策略计算后传入，循环无感知映射来源）
    - ``tool_choice_terminal``：终止性工具名（submit_suggestion）
    - ``seed_messages``：调用方构建的种子消息（system + user）。入口立即 ``list()``
      做快照：不改写调用方对象、调用方仍可复用；用 ``Sequence`` 而非 ``Iterable``
      ——seed 会被 runtime 的 ``write_seed_row`` 与 loop **多次消费**，生成器必须被
      类型排除
    - ``recorder``：可选预算记录器（满足本地 :class:`BudgetRecorder` 协议；不导入具体
      实现以避免与 recorder.py 循环依赖），None 时跳过钩子
    - ``veto_terminal``：可选终止提交软护栏。非 None 时，命中终止工具的轮次会先询问
      该回调（入参=terminal input）；返回提示文案则**不终止**、注入配对 tool_result 后
      继续一轮（仅拦一次，且末轮不拦）。返回 None / 回调为 None 时行为与现状一致。

    必须提供 ``stream_fn`` 或 ``chat_fn`` 之一；否则抛 ``ValueError``。
    """
    if stream_fn is None and chat_fn is None:
        raise ValueError("run 需要 stream_fn 或 chat_fn 之一")
    if stream_fn is not None and chat_fn is not None:
        # 双注入属调用方误用：忽略旧 chat_fn，以流式路径为准并告警
        logger.warning(
            "loop.run 同时收到 stream_fn 与 chat_fn，忽略 chat_fn（流式优先）"
        )
        chat_fn = None

    # 物化快照：后续每轮原地 append（assistant/tool_result/预算消息），需可追加容器；
    # list() 浅拷贝保证不改写调用方的 seed 对象（Message 视为不可变，只追加新对象）。
    messages: list[Message] = list(seed_messages)
    remaining = max_iterations
    resp: ChatResponse | None = None
    vetoed = False

    for _iteration in range(max_iterations):
        # 末轮（remaining==1 起手）强制 terminal 收尾；其余轮不指定 tool_choice
        tool_choice = tool_choice_terminal if remaining == 1 else None

        executor: StreamingExecutor | None = None
        if stream_fn is not None:
            resp, executor = await _consume_stream(
                stream_fn,
                messages,
                tools=tools_schemas,
                tool_choice=tool_choice,
                executor_factory=executor_factory,
            )
        else:
            # 入口不变式：双 None 已 raise、双注入已忽略 chat_fn，此处 chat_fn 必非空
            assert chat_fn is not None
            resp = await chat_fn(messages, tools=tools_schemas, tool_choice=tool_choice)

        # ① end_turn → 终止
        if resp.stop_reason == "end_turn":
            return RunResult(
                stop_reason="end_turn", text=resp.content, last_response=resp
            )

        # ② 无 tool_calls → 按 stop_reason 细分终止原因
        tool_calls = _extract_tool_calls(resp)
        if not tool_calls:
            if resp.stop_reason == "max_tokens":
                # 生成长度超限（非故障，但属特殊终态）
                return RunResult(
                    stop_reason="max_tokens", text=resp.content, last_response=resp
                )
            if resp.stop_reason == "" and not resp.blocks and not resp.content:
                # 空壳响应（LLM 调用失败后被错误传递到 loop，或 provider 异常）
                # → 显式标记 llm_error，不再伪装 end_turn
                return RunResult(stop_reason="llm_error", last_response=resp)
            # 其余（stop_sequence / stop_reason 未知但有 content 等）维持 end_turn 兼容
            return RunResult(
                stop_reason="end_turn", text=resp.content, last_response=resp
            )

        # ③ 先将本轮全部 tool_use blocks 聚合为【一条】assistant 消息追加。
        # 同时保留响应中的非工具块（Text/Thinking）：思考模型的 thinking 块必须随
        # tool_use 一并回传（Anthropic/DeepSeek 约束，否则真实端点 400：
        # "content[].thinking ... must be passed back"）；OpenAI 兼容层在 provider
        # 侧按各自协议处理（thinking 块跳过）。
        # 无需 ``or [ToolUseBlock(...)]`` 兜底：上方 ``if not tool_calls: return`` 已保证
        # 本轮存在 ToolUseBlock，而 ``tool_calls`` 正是从 ``resp.blocks`` 提取，故
        # ``resp.blocks`` 必非空，兜底分支不可达。
        assistant_blocks: list[ContentBlock] = list(resp.blocks)
        messages.append(Message(role="assistant", content=assistant_blocks))

        # ④ 终止工具优先：含 tool_choice_terminal → 捕获即 break（其他工具不执行）
        terminal_tc: ToolUseBlock | None = None
        terminal_idx = -1
        for idx, tc in enumerate(tool_calls):
            if tc.name == tool_choice_terminal:
                terminal_tc = tc
                terminal_idx = idx
                break
        if terminal_tc is not None:
            # 流式路径：收尾以 await 提前任务（terminal 抑制已保证不再启动新任务、
            # 且本轮非终止工具不执行）；旧路径不调用执行器。结果在终止分支被忽略。
            if executor is not None:
                await executor.finalize()
            # 软护栏（veto）：非末轮 + 本 run 尚未拦过时，先询问一次场景回调。
            # 末轮强收尾优先（remaining==1 不拦）；只拦一次避免无限暂缓。
            hint = ""
            if veto_terminal is not None and remaining > 1 and not vetoed:
                hint = veto_terminal(terminal_tc.input or {}) or ""
            if not hint:
                return RunResult(
                    stop_reason="submit_suggestion",
                    suggestion=terminal_tc.input,
                    last_response=resp,
                )
            # 暂缓：不终止、不执行其他工具，注入配对 tool_result 后消耗一轮预算继续。
            vetoed = True
            _inject_veto_hint(messages, terminal_tc, hint)
            _record_veto_tool(recorder, terminal_tc, hint, sequence=terminal_idx)
            remaining -= 1
            continue

        # ⑤ 执行本轮工具：
        #    - 流式路径：finalize 保序返回与 tool_calls 对齐的 ToolResultBlock 列表
        #    - 旧路径：整批交给 tool_calls_fn（execute_batch 内部分段并行），按槽位对齐
        # aligned 元素含 None（旧契约按 id 取值可能缺槽位），故用联合元素类型；用
        # Sequence 承接各分支的不同具体列表（list 不变性下无法统一到同一 list 注解）。
        aligned: Sequence[ToolResultBlock | None]
        if executor is not None:
            aligned = await executor.finalize()
            if len(aligned) != len(tool_calls):
                # 防御：执行器结果槽位数与 tool_calls 不一致（协议破坏）→ 补齐，
                # 保证每条 tool_use 都有配对 tool_result（否则 zip 截断留下悬垂 tool_use）
                logger.warning(
                    "流式执行器结果槽位数（%d）与 tool_calls（%d）不一致",
                    len(aligned),
                    len(tool_calls),
                )
                aligned = _pad_aligned(tool_calls, aligned)
        elif tool_calls_fn is not None:
            results = await tool_calls_fn(tool_calls)
            aligned = _align_results(tool_calls, results)
        else:
            # 防御兜底：既无执行器也无批量执行器（纯聚合调用方误传工具轮）→ 闭合协议
            logger.warning(
                "loop 无执行器可用（stream_fn 未配 executor_factory 且无 tool_calls_fn），"
                "本轮 %d 个工具以错误块回填",
                len(tool_calls),
            )
            aligned = [
                ToolResultBlock(
                    tool_use_id=tc.id,
                    content="skipped: no tool executor configured",
                    is_error=True,
                )
                for tc in tool_calls
            ]

        # ⑥ 逐条：追加 tool_result
        for tc, result in zip(tool_calls, aligned, strict=False):
            if result is None or not isinstance(result, ToolResultBlock):
                # 防御：缺失结果或非 ToolResultBlock（如极少数 TerminalCapture 泄漏）跳过
                logger.warning(
                    "tool result 缺失或非 ToolResultBlock（tool=%s, type=%s），跳过",
                    tc.name,
                    type(result).__name__,
                )
                continue
            messages.append(Message(role="user", content=[result]))

        # ⑦ 透明预算：递减后仅当仍有后续轮次（remaining>0）才注入剩余轮次。
        #    末轮递减到 0 时循环随即结束，若仍构造预算消息会写入 trace 参与 replay，
        #    但该消息从未发给 LLM —— 形成「幻影预算消息」，故不注入也不记录。
        remaining -= 1
        if remaining > 0:
            budget_message = (
                FINAL_ROUND_BUDGET_MESSAGE
                if remaining == 1
                else f"[剩余轮次：{remaining}]"
            )
            messages.append(Message(role="user", content=budget_message))
            if recorder is not None:
                recorder.record_budget(budget_message)

    # 预算刚性耗尽：追加一次收尾 LLM 调用（best-effort，仅一次，不执行非终止工具），
    # 尽量在返回 exhausted 前拿到明确结论。
    return await _final_recovery(
        stream_fn=stream_fn,
        chat_fn=chat_fn,
        tools_schemas=tools_schemas,
        tool_choice_terminal=tool_choice_terminal,
        messages=messages,
        recorder=recorder,
        last_response=resp,
    )


async def _final_recovery(
    *,
    tools_schemas: list[dict],
    tool_choice_terminal: str,
    messages: list[Message],
    recorder: BudgetRecorder | None,
    last_response: ChatResponse | None,
    stream_fn: StreamFn | None = None,
    chat_fn: ChatFn | None = None,
) -> RunResult:
    """耗尽后的兜底收尾调用（最多一次）。

    - 追加 ``FINAL_RECOVERY_MESSAGE`` 并经 recorder 预算通道记录（重放一致性）
    - tools 过滤为**仅** terminal schema，``tool_choice=terminal``
    - 含 terminal tool_call → submit_suggestion；否则 exhausted（last_response=收尾响应）
    - LLM 调用抛异常 → best-effort 捕获返回 exhausted（保留循环内最后一次响应），不重试
    - 收尾不执行任何工具（``executor_factory=None``：纯聚合流，不构造执行器）
    """
    messages.append(Message(role="user", content=FINAL_RECOVERY_MESSAGE))
    if recorder is not None:
        recorder.record_budget(FINAL_RECOVERY_MESSAGE)

    terminal_schemas = [
        s
        for s in tools_schemas
        if isinstance(s, dict) and s.get("name") == tool_choice_terminal
    ]
    try:
        if stream_fn is not None:
            recovery_resp, _ = await _consume_stream(
                stream_fn,
                messages,
                tools=terminal_schemas,
                tool_choice=tool_choice_terminal,
                executor_factory=None,
            )
        else:
            # 入口不变式：双 None 已 raise、双注入已忽略 chat_fn，此处 chat_fn 必非空
            assert chat_fn is not None
            recovery_resp = await chat_fn(
                messages, tools=terminal_schemas, tool_choice=tool_choice_terminal
            )
    except Exception as e:
        # best-effort：收尾失败不应让 run 更糟，保留循环内最后一次可用响应
        logger.warning("收尾调用失败（best-effort 降级为 exhausted）: %s", e)
        return RunResult(stop_reason="exhausted", last_response=last_response)

    terminal_tc = next(
        (
            tc
            for tc in _extract_tool_calls(recovery_resp)
            if tc.name == tool_choice_terminal
        ),
        None,
    )
    if terminal_tc is not None:
        return RunResult(
            stop_reason="submit_suggestion",
            suggestion=terminal_tc.input,
            last_response=recovery_resp,
        )
    return RunResult(stop_reason="exhausted", last_response=recovery_resp)

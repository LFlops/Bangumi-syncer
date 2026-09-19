"""Eval 最小公共库：录制/回放注入器、请求指纹、cassette IO、结果收集。

设计（对齐 opencode 的 VCR 模式）：

- ``fixtures/<case_id>.json``（cassette）保存一次真实执行的 LLM 响应序列 +
  工具结果 + 期望终局，含每轮**请求指纹**；
- ``replay`` 完全离线（不调用 LLM、不访问 Bangumi），任何请求变化 → 指纹不匹配 → 失败；
- ``record`` / ``live`` 需要真实 LLM 配置；``record`` 在 CI 环境被硬拒绝
  （机制保证 CI 不产生真实调用，见 ``run_eval.py``）。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from app.core.database import DatabaseManager, set_database_manager
from app.services.llm.models import (
    ChatResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from app.services.llm.tools import ToolRegistry
from app.services.matching import llm_assist

CASSETTE_SCHEMA_VERSION = 1


class FingerprintMismatch(RuntimeError):
    """回放时请求与录制不一致（提示用 ``--mode record`` 重录）。"""


# ---------------------------------------------------------------------------
# 请求指纹与响应编解码
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def fingerprint(messages, tools, tool_choice, model: str) -> str:
    """请求指纹：模型 + 工具 schema + tool_choice + 规范化消息序列。"""
    payload = {
        "model": model or "",
        "tools": [_dump(t) for t in (tools or [])],
        "tool_choice": tool_choice,
        "messages": [_dump(m) for m in messages],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def response_to_wire(resp: ChatResponse) -> dict:
    """ChatResponse → cassette 存储形态（stop_reason/content/tool_calls/usage）。"""
    return {
        "stop_reason": resp.stop_reason,
        "content": resp.content,
        "tool_calls": [_dump(b) for b in resp.blocks if isinstance(b, ToolUseBlock)],
        "usage": _dump(resp.usage) if resp.usage else None,
    }


def response_from_wire(wire: dict) -> ChatResponse:
    """cassette 存储形态 → ChatResponse（回放用）。"""
    content = wire.get("content") or ""
    blocks: list = []
    if content:
        blocks.append(TextBlock(text=content))
    for tc in wire.get("tool_calls") or []:
        blocks.append(
            ToolUseBlock(
                id=str(tc.get("id") or ""),
                name=str(tc.get("name") or ""),
                input=tc.get("input") or {},
            )
        )
    usage = Usage(**wire["usage"]) if wire.get("usage") else None
    return ChatResponse(
        content=content,
        blocks=blocks,
        stop_reason=wire.get("stop_reason") or "",
        usage=usage,
    )


# ---------------------------------------------------------------------------
# Golden / cassette IO
# ---------------------------------------------------------------------------


def load_golden(path) -> list:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def load_cassette(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_cassette(path, cassette: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cassette, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 回放注入器（完全离线）
# ---------------------------------------------------------------------------


class _FixtureResults:
    """伪 BatchResults：提供 ``ordered`` 槽位与 ``get`` 两种视图。"""

    def __init__(self, ordered: list):
        self.ordered = ordered

    def get(self, key, default=None):
        for oid, item in self.ordered:
            if oid == key:
                return item
        return default


class FixtureDriver:
    """回放驱动：按录制轮次依次返回 LLM 响应与工具结果。"""

    def __init__(self, rounds: list, model: str = ""):
        self._rounds = rounds
        self._idx = -1
        self._model = model

    @property
    def rounds_consumed(self) -> int:
        return self._idx + 1

    async def chat(self, messages, *, tools=None, tool_choice=None):
        self._idx += 1
        if self._idx >= len(self._rounds):
            raise FingerprintMismatch(
                f"回放请求超出录制轮数（round={self._idx}，cassette={len(self._rounds)}）"
            )
        rnd = self._rounds[self._idx]
        expected = rnd.get("request_fingerprint")
        actual = fingerprint(messages, tools, tool_choice, self._model)
        if expected and actual != expected:
            raise FingerprintMismatch(
                f"第 {self._idx} 轮请求与录制不一致（prompt/消息序列/工具协议已变化）——"
                f"请用 --mode record 重新录制"
            )
        return response_from_wire(rnd.get("response") or {})

    async def execute_batch(self, tool_calls, *, recorder=None):
        if not (0 <= self._idx < len(self._rounds)):
            raise FingerprintMismatch(
                "工具回放发生在 LLM 调用之前（cassette 顺序异常）"
            )
        tmap = {
            str(t.get("tool_use_id")): t
            for t in self._rounds[self._idx].get("tool_results") or []
        }
        ordered = []
        for tc in tool_calls:
            item = tmap.get(tc.id)
            if item is None:
                raise FingerprintMismatch(
                    f"cassette 缺少 tool_use_id={tc.id} 的工具结果"
                )
            ordered.append(
                (
                    tc.id,
                    ToolResultBlock(
                        tool_use_id=tc.id,
                        content=item.get("content", ""),
                        is_error=bool(item.get("is_error")),
                    ),
                )
            )
        return _FixtureResults(ordered)


# ---------------------------------------------------------------------------
# 录制注入器
# ---------------------------------------------------------------------------


class RecordingChatFn:
    """录制包装：透传真实调用，记录请求指纹与响应。"""

    def __init__(self, inner, model: str, sink: list):
        self._inner = inner
        self._model = model
        self._sink = sink

    async def __call__(self, messages, *, tools=None, tool_choice=None):
        fp = fingerprint(messages, tools, tool_choice, self._model)
        resp = await self._inner(messages, tools=tools, tool_choice=tool_choice)
        self._sink.append(
            {
                "request_fingerprint": fp,
                "response": response_to_wire(resp),
                "tool_results": [],
            }
        )
        return resp


def recording_execute_batch_factory(orig, sink):
    """构造工具录制包装（保留类方法绑定语义）。"""

    async def wrapped(self, tool_calls, *, recorder=None):
        results = await orig(self, tool_calls, recorder=recorder)
        if sink:
            current = sink[-1]
            for tc in tool_calls:
                blk = results.get(tc.id) if hasattr(results, "get") else None
                if isinstance(blk, ToolResultBlock):
                    current["tool_results"].append(
                        {
                            "tool_use_id": tc.id,
                            "tool_name": tc.name,
                            "input": tc.input,
                            "content": blk.content,
                            "is_error": blk.is_error,
                        }
                    )
        return results

    return wrapped


class CountingNotifier:
    """通知计数（断言通知次数；内容仅存可序列化摘要）。"""

    def __init__(self):
        self.calls = []

    def notify(self, *args, **kwargs):
        self.calls.append(
            {
                "args": [str(a) for a in args],
                "kwargs": {
                    k: (
                        v
                        if isinstance(v, (str, int, float, bool, type(None)))
                        else str(v)
                    )
                    for k, v in kwargs.items()
                },
            }
        )
        return None


# ---------------------------------------------------------------------------
# 环境与编排
# ---------------------------------------------------------------------------


def make_env(tmpdir) -> DatabaseManager:
    """临时库环境（绝不触碰生产 data/sync_records.db）。"""
    dbm = DatabaseManager(str(Path(tmpdir) / "eval.db"))
    set_database_manager(dbm)
    return dbm


def build_sync_record(case: dict, sync_record_id: int = 1) -> dict:
    inp = case.get("input") or {}
    return {
        "id": sync_record_id,
        "title": inp.get("title", ""),
        "ori_title": inp.get("ori_title", ""),
        "season": inp.get("season", 1),
        "episode": inp.get("episode", 0),
        "release_date": inp.get("release_date", ""),
        "media_type": inp.get("media_type", "episode"),
        "user_name": "eval",
        "source": "eval",
    }


def build_real_chat_fn(llm_cfg: dict):
    """构造真实 chat_fn（record/live）。

    注意：不向 provider 传 ``thinking_level``——兼容端点（如 DeepSeek 的
    Anthropic 兼容层）未必支持 thinking 字段；多轮预算由场景的 thinking_level
    参数独立控制。
    """
    from app.core.config import config_manager
    from app.services.llm.client import LLMClient

    with patch.object(config_manager, "get_llm_config", return_value=llm_cfg):
        client = LLMClient()

    async def chat_fn(messages, *, tools=None, tool_choice=None):
        return await client.chat(
            messages, tools=tools, tool_choice=tool_choice, job_name="eval"
        )

    return chat_fn


def _maybe_patch_legacy_search(bgm) -> None:
    """外部临时故障适配：Bangumi v0 搜索接口 502 时改用 legacy 搜索接口。

    仅影响 eval 运行环境的检索通道（数据仍来自 Bangumi 真实接口）；
    v0 接口恢复后自动不再启用（探测一次）。
    """
    import httpx

    _UA = "SanaeMio/Bangumi-syncer (https://github.com/SanaeMio/Bangumi-syncer)"
    reason = ""
    try:
        r = httpx.post(
            "https://api.bgm.tv/v0/search/subjects?limit=1",
            headers={"User-Agent": _UA},
            json={"keyword": "test", "filter": {"type": [2]}},
            timeout=10,
        )
        if r.status_code == 200:
            return
        reason = f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        reason = str(e)

    print(
        f"[eval] Bangumi v0 搜索接口不可用（{reason}），"
        f"本次改用 legacy 搜索接口（仅 eval 环境适配）"
    )

    def _legacy_search(title, subject_types=None, limit=5, **_kwargs):
        import urllib.parse

        t = (subject_types or [2])[0]
        kw = urllib.parse.quote(str(title))
        rr = httpx.get(
            f"https://api.bgm.tv/search/subject/{kw}"
            f"?type={t}&responseGroup=small&max_results={limit}",
            headers={"User-Agent": _UA},
            timeout=15,
        )
        rr.raise_for_status()
        items = (rr.json() or {}).get("list") or []
        return [
            {
                "id": it.get("id"),
                "name": it.get("name"),
                "name_cn": it.get("name_cn"),
                "date": it.get("air_date"),
                "type": it.get("type"),
                "images": it.get("images"),
                "summary": (it.get("summary") or "")[:200],
            }
            for it in items
        ]

    bgm.search = _legacy_search


def collect_outcome(dbm, run_id: str, sync_record_id: int, notifier) -> dict:
    run = dbm.agent_runs.get_run(run_id) or {}
    cand = dbm._pending.get_pending_candidate_by_sync_record_id(sync_record_id)
    subject_id = ""
    reason = ""
    if cand:
        subject_id = str(cand.get("llm_subject_id") or "")
        reason = str(cand.get("llm_reason") or "")
        if not subject_id:
            try:
                arr = json.loads(cand.get("candidates_json") or "[]")
                for c in reversed(arr):
                    if isinstance(c, dict) and c.get("source") == "llm_assist":
                        subject_id = str(c.get("subject_id") or "")
                        reason = str(c.get("reason") or reason)
                        break
            except Exception:
                pass
    return {
        "run_id": run_id,
        "run_status": run.get("status"),
        "stop_reason": run.get("stop_reason"),
        "total_tokens": int(run.get("total_tokens") or 0),
        "candidate_subject_id": subject_id or None,
        "candidate_reason": reason or None,
        "notifications": len(notifier.calls),
    }


def hit_expect(expect: dict, outcome: dict) -> bool:
    """按 golden 期望判定命中（acceptable_ids / no_suggestion）。"""
    cid = outcome.get("candidate_subject_id")
    if expect.get("expect_stop") == "no_suggestion":
        return not cid
    if not cid:
        return False
    accept = expect.get("acceptable_ids") or (
        [expect.get("subject_id")] if expect.get("subject_id") else []
    )
    return str(cid) in {str(x) for x in accept if x}


async def run_case(
    case: dict,
    *,
    mode: str,
    fixtures_dir,
    llm_cfg: dict | None = None,
    thinking_level: str = "medium",
    judge_fn: Callable | None = None,
) -> dict:
    """执行单条 case（record / replay / live），返回结构化结果。"""
    case_id = case["id"]
    tmp_root = Path(tempfile.mkdtemp(prefix=f"eval_{case_id}_"))
    dbm = make_env(tmp_root)
    sync_record_id = 1
    sync_record = build_sync_record(case, sync_record_id)
    run_id = f"eval-{case_id}"
    dbm.agent_runs.create_pending(run_id, "match", sync_record_id)

    notifier = CountingNotifier()
    sink: list = []
    cassette_path = Path(fixtures_dir) / f"{case_id}.json"
    cassette: dict | None = None

    if mode == "replay":
        cassette = load_cassette(cassette_path)
        if cassette.get("schema_version") != CASSETTE_SCHEMA_VERSION:
            raise FingerprintMismatch(
                f"cassette schema 版本不支持: {cassette.get('schema_version')}"
            )
        driver = FixtureDriver(
            cassette.get("rounds") or [],
            model=(cassette.get("meta") or {}).get("model", ""),
        )

        async def _fixture_execute_batch(self, tool_calls, *, recorder=None):
            return await driver.execute_batch(tool_calls, recorder=recorder)

        chat_fn = driver.chat
        tools_cm = patch.object(ToolRegistry, "execute_batch", _fixture_execute_batch)
        bgm = None  # 回放完全离线；_prefetch_bgm_name(None) → ""
    else:
        if llm_cfg is None:
            raise RuntimeError("record/live 模式需要 LLM 配置（环境变量）")
        real_chat = build_real_chat_fn(llm_cfg)
        chat_fn = RecordingChatFn(real_chat, model=llm_cfg.get("model", ""), sink=sink)
        orig_execute_batch = ToolRegistry.execute_batch
        tools_cm = patch.object(
            ToolRegistry,
            "execute_batch",
            recording_execute_batch_factory(orig_execute_batch, sink),
        )
        from app.utils.bangumi_api import BangumiApi

        bgm = BangumiApi(username=None, access_token=None)
        _maybe_patch_legacy_search(bgm)

    with tools_cm:
        status = await llm_assist.run(
            run_id,
            sync_record=sync_record,
            bgm=bgm,
            thinking_level=thinking_level,
            chat_fn=chat_fn,
            notification_service=notifier,
        )

    outcome = collect_outcome(dbm, run_id, sync_record_id, notifier)
    result = {
        "case_id": case_id,
        "mode": mode,
        "status": status,
        "outcome": outcome,
        "expect": case.get("expect") or {},
        "hit": hit_expect(case.get("expect") or {}, outcome),
        "tags": case.get("tags") or [],
        "ok": True,
        "error": None,
    }

    if mode == "record":
        cassette = {
            "schema_version": CASSETTE_SCHEMA_VERSION,
            "case_id": case_id,
            "meta": {
                "model": llm_cfg.get("model", ""),
                "provider": llm_cfg.get("provider", ""),
                "thinking_level": thinking_level,
                "golden_note": case.get("note", ""),
            },
            "rounds": sink,
            "expected_outcome": {
                "run_status": outcome["run_status"],
                "stop_reason": outcome["stop_reason"],
                "candidate_subject_id": outcome["candidate_subject_id"],
                "notifications": outcome["notifications"],
            },
        }
        write_cassette(cassette_path, cassette)
        result["cassette_written"] = str(cassette_path)
        result["rounds_recorded"] = len(sink)
    elif mode == "replay":
        exp = cassette.get("expected_outcome") or {}
        assertions = [
            ("run_status", outcome["run_status"], exp.get("run_status")),
            (
                "candidate_subject_id",
                outcome["candidate_subject_id"],
                exp.get("candidate_subject_id"),
            ),
            ("notifications", outcome["notifications"], exp.get("notifications")),
        ]
        for name, actual, expected in assertions:
            if actual != expected:
                raise AssertionError(
                    f"[replay 不一致] {case_id}.{name}: 回放={actual!r} 录制={expected!r}"
                )
        result["replay_verified"] = True
    elif mode == "live" and judge_fn is not None:
        result["judge"] = judge_fn(case, outcome)

    return result


# ---------------------------------------------------------------------------
# LLM 配置（环境变量）
# ---------------------------------------------------------------------------


def llm_cfg_from_env() -> dict:
    import os

    api_key = os.environ.get("EVAL_LLM_API_KEY", "")
    if not api_key:
        raise SystemExit("缺少 EVAL_LLM_API_KEY（record/live 模式需要真实 LLM 配置）")
    return {
        "provider": os.environ.get("EVAL_LLM_PROVIDER", "anthropic_compat"),
        "api_base": os.environ.get(
            "EVAL_LLM_BASE", "https://api.deepseek.com/anthropic/v1"
        ),
        "api_key": api_key,
        "model": os.environ.get("EVAL_LLM_MODEL", "deepseek-chat"),
        "max_tokens": int(os.environ.get("EVAL_LLM_MAX_TOKENS", "2000")),
        "temperature": float(os.environ.get("EVAL_LLM_TEMPERATURE", "0.0")),
        "timeout": int(os.environ.get("EVAL_LLM_TIMEOUT", "120")),
        "retention_days": 365,
    }

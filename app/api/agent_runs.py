"""
Agent 追踪查询 API

提供观测用途的追踪查询端点（仅元数据，不暴露内部重放全文）：
- ``GET /api/agent/runs/{run_id}``      → 单次会话元数据
- ``GET /api/agent/runs/{run_id}/steps`` → span 列表（按 (iteration, sequence) 排序）

鉴权语义：
- 未认证 → 401（复用 ``get_current_user_flexible`` 既有行为）
- 非本人且非管理员 → 403（经 ``sync_record_id`` 关联 ``sync_records.user_name`` 校验）
- run 不存在 → 404

展示契约：
- 响应永不包含 ``replay_delta`` 原文（仅用于内部重放）。
- ``payload_json`` 列已删除，不再出现在响应中。
- steps 端点新增 ``display_json``：读取时解密 replay_delta → 现场截断生成展示摘要。
- 时间字段由 epoch 秒整数转为 ISO 8601 字符串（含时区）；0/None → None。
"""

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..api.deps import get_current_user_flexible
from ..core.database import database_manager
from ..core.logging import logger
from ..core.security import security_manager
from ..utils.truncate import truncate_json

router = APIRouter(prefix="/api/agent", tags=["agent"])

# run 端点对外暴露的字段（不含 payload_json / replay_delta 等内部重放字段）
_RUN_PUBLIC_FIELDS = (
    "status",
    "stop_reason",
    "task_type",
    "sync_record_id",
    "attempts",
    "total_attempts",
    "total_tokens",
    "started_at",
    "ended_at",
    "created_at",
    "last_error",
)

# steps 端点对外暴露的字段（不含 payload_json / replay_delta；display_json 现场生成）
_STEP_PUBLIC_FIELDS = (
    "span_id",
    "name",
    "status",
    "model",
    "tokens",
    "latency_ms",
    "tool_name",
    "input_summary",
    "error",
    "iteration",
    "sequence",
    "started_at",
    "ended_at",
)

# 时间字段集合（run / steps 共享），用于 ISO 转换
_TIME_FIELDS = ("started_at", "ended_at", "created_at")


def _is_admin_user(current_user: dict) -> bool:
    """当前用户是否为管理员。

    管理员判定（与系统单管理员模型一致）：
    - 认证关闭（auth_disabled）视为管理员
    - 会话带 is_admin 标志
    - 用户名为配置中 auth.username 管理员账号
    """
    if current_user.get("auth_disabled"):
        return True
    if current_user.get("is_admin"):
        return True
    try:
        admin_name = security_manager.get_auth_config().get("username")
    except Exception:  # pragma: no cover - 配置异常时保守处理
        admin_name = None
    return bool(admin_name) and current_user.get("username") == admin_name


def _resolve_owner_user_name(run: dict) -> Optional[str]:
    """经 sync_record_id 解析会话归属用户（无则 None）。"""
    sync_record_id = run.get("sync_record_id")
    if not sync_record_id:
        return None
    try:
        record = database_manager.sync_records.get_sync_record_by_id(sync_record_id)
    except Exception as e:  # pragma: no cover - 查询异常时按“无归属”处理
        logger.error(f"查询 sync_record 归属失败: {e}")
        return None
    if not record:
        return None
    return record.get("user_name")


def _authorize(run: dict, current_user: dict) -> None:
    """校验当前用户是否可访问该 run；否则 403。"""
    if _is_admin_user(current_user):
        return
    owner = _resolve_owner_user_name(run)
    if owner is not None and current_user.get("username") == owner:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="无权访问该追踪记录",
    )


def _iso_from_epoch(ts) -> Optional[str]:
    """epoch 秒整数 → ISO 8601 字符串（含时区）；0/None/非数字 → None。"""
    if not ts:
        return None
    if not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _apply_time_fields(record: dict) -> dict:
    """将记录中的时间列原地转换为 ISO 字符串。"""
    for field in _TIME_FIELDS:
        if field in record:
            record[field] = _iso_from_epoch(record.get(field))
    return record


def _build_display_json(name: str, replay_delta: str) -> str:
    """从 replay_delta 现场生成展示摘要（截断至合法 JSON）。

    - llm_chat → {"stop_reason", "content"}
    - tool_execute → {"tool_use_id", "content", "is_error"}
    - seed → {"seed_messages_count": N}
    - 解析失败/空 delta/未知 name → ""
    """
    if not replay_delta:
        return ""
    try:
        data = (
            json.loads(replay_delta) if isinstance(replay_delta, str) else replay_delta
        )
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""

    if name == "llm_chat":
        response = data.get("response") or {}
        preview = {
            "stop_reason": response.get("stop_reason", ""),
            "content": response.get("content", ""),
        }
    elif name == "tool_execute":
        tool_result = data.get("tool_result") or {}
        preview = {
            "tool_use_id": tool_result.get("tool_use_id", ""),
            "content": tool_result.get("content", ""),
            "is_error": tool_result.get("is_error", False),
        }
    elif name == "seed":
        seed_messages = data.get("seed_messages") or []
        preview = {"seed_messages_count": len(seed_messages)}
    else:
        return ""

    return truncate_json(preview)


def _project_run(run: dict) -> dict:
    """裁剪为对外元数据（剔除内部重放字段），时间列 ISO 化。"""
    projected = {field: run.get(field) for field in _RUN_PUBLIC_FIELDS}
    _apply_time_fields(projected)
    return projected


def _project_steps(steps: list) -> list:
    """裁剪 span 列表：剔除内部字段、生成 display_json、ISO 时间、按 (iteration, sequence) 排序。"""
    projected = []
    for s in steps:
        record = {f: s.get(f) for f in _STEP_PUBLIC_FIELDS}
        record["display_json"] = _build_display_json(
            s.get("name", ""), s.get("replay_delta", "")
        )
        _apply_time_fields(record)
        projected.append(record)
    projected.sort(key=lambda r: (r.get("iteration") or 0, r.get("sequence") or 0))
    return projected


@router.get("/runs/{run_id}")
async def get_agent_run(
    run_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user_flexible),
) -> dict:
    """获取单次 Agent 会话的元数据（观测用途）。"""
    run = database_manager.agent_runs.get_run(run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="追踪记录不存在"
        )
    _authorize(run, current_user)
    return _project_run(run)


@router.get("/runs/{run_id}/steps")
async def get_agent_run_steps(
    run_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user_flexible),
) -> list:
    """获取单次 Agent 会话的 span 列表（按 (iteration, sequence) 排序）。"""
    run = database_manager.agent_runs.get_run(run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="追踪记录不存在"
        )
    _authorize(run, current_user)
    steps = database_manager.agent_runs.get_steps(run_id)
    return _project_steps(steps)

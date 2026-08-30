"""
Agent 追踪查询 API（Task T10 / 场景 M21, M21b）

提供观测用途的追踪查询端点（仅元数据，不暴露内部重放全文）：
- ``GET /api/agent/runs/{run_id}``      → 单次会话元数据
- ``GET /api/agent/runs/{run_id}/steps`` → span 列表（按 (iteration, sequence) 排序）

鉴权语义：
- 未认证 → 401（复用 ``get_current_user_flexible`` 既有行为）
- 非本人且非管理员 → 403（经 ``sync_record_id`` 关联 ``sync_records.user_name`` 校验）
- run 不存在 → 404

观测 API 不返回 ``payload_json`` 全文之外的内部字段（run 端点不返回
``payload_json`` / ``replay_delta``；steps 端点返回 ``payload_json`` 观测摘要，
但绝不返回 ``replay_delta``——其为内部重放专用）。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..api.deps import get_current_user_flexible
from ..core.database import database_manager
from ..core.logging import logger
from ..core.security import security_manager

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

# steps 端点对外暴露的字段（含 payload_json 观测摘要，但不含 replay_delta）
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
    "payload_json",
    "started_at",
    "ended_at",
)


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


def _project_run(run: dict) -> dict:
    """裁剪为对外元数据（剔除内部重放字段）。"""
    return {field: run.get(field) for field in _RUN_PUBLIC_FIELDS}


def _project_steps(steps: list) -> list:
    """裁剪 span 列表并强制按 (iteration, sequence) 排序。"""
    projected = [{f: s.get(f) for f in _STEP_PUBLIC_FIELDS} for s in steps]
    projected.sort(key=lambda s: (s.get("iteration") or 0, s.get("sequence") or 0))
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

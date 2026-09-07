"""
MCP 内部 API：日志读取端点（/api/mcp/logs）
"""

import asyncio
import os
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.config import config_manager
from ..core.logging import logger, resolved_dev_log_file_path
from .deps import get_mcp_client
from .logs import _read_log_file

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# 日志行时间戳格式: [2026/09/03 12:00:00.123]
_LOG_TIMESTAMP_RE = re.compile(r"^\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]")

VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


def _parse_log_timestamp(line: str) -> datetime | None:
    """解析日志行中的时间戳；无法解析返回 None。"""
    m = _LOG_TIMESTAMP_RE.match(line)
    if not m:
        return None
    ts_str = m.group(1)
    for fmt in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
    return None


def _parse_iso_datetime(value: str) -> datetime:
    """解析 ISO 格式时间字符串（since/until），无效格式抛出 ValueError。

    兼容 Python 3.9：fromisoformat 不支持 'Z' 后缀与空格分隔。

    返回 naive datetime（去掉时区信息），与日志行时间戳（_server 本地时间，naive）
    保持同一语义，避免比较时抛出 TypeError。
    """
    # 标准化：空格 → T，Z → +00:00（UTC 标记）
    normalized = value.replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"无法解析的时间格式: {value}")
    # 统一为 naive：日志时间戳无时区信息，按服务器本地时间对齐
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _filter_by_time_range(
    content: str,
    since: datetime | None,
    until: datetime | None,
) -> str:
    """按时间范围过滤日志内容行。"""
    if since is None and until is None:
        return content

    lines = content.splitlines(keepends=True)
    filtered = []
    for line in lines:
        ts = _parse_log_timestamp(line)
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        filtered.append(line)
    return "".join(filtered)


@router.get("/logs")
async def get_mcp_logs(
    level: str | None = None,
    limit: int = Query(50, ge=1, le=10000),
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
    current_user: dict = Depends(get_mcp_client()),
) -> dict[str, Any]:
    """MCP 内部 API：获取日志内容，支持级别过滤与时间范围过滤。"""
    # 验证 level
    if level is not None:
        level_upper = level.upper()
        if level_upper == "WARN":
            level_upper = "WARNING"
        if level_upper not in VALID_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"无效的日志级别: {level}，有效值为: {', '.join(sorted(VALID_LEVELS))}",
            )
        level = level_upper

    # 验证并解析时间范围
    since_dt: datetime | None = None
    until_dt: datetime | None = None

    if since is not None:
        try:
            since_dt = _parse_iso_datetime(since)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"无效的 since 时间格式: {since}"
            )

    if until is not None:
        try:
            until_dt = _parse_iso_datetime(until)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"无效的 until 时间格式: {until}"
            )

    # 校验时间顺序：since 必须早于或等于 until
    if since_dt is not None and until_dt is not None and since_dt > until_dt:
        raise HTTPException(
            status_code=400,
            detail="since 时间必须早于或等于 until 时间",
        )

    try:
        log_path = resolved_dev_log_file_path(config_manager)
        if log_path is None:
            return {
                "status": "success",
                "data": {
                    "content": "",
                    "stats": {"size": 0, "lines": 0, "modified": None, "errors": 0},
                },
            }

        log_file_path = os.fspath(log_path)

        result = await asyncio.to_thread(
            _read_log_file, log_file_path, level, search, str(limit)
        )

        # 应用时间范围过滤
        if "content" in result:
            result["content"] = _filter_by_time_range(
                result["content"], since_dt, until_dt
            )
            result["stats"]["lines"] = result["content"].count("\n")

        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取日志失败: {e}")
        raise HTTPException(
            status_code=500,
            detail="获取日志失败，请检查日志文件配置",
        )

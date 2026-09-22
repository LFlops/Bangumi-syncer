"""
MCP 工具函数（FastMCP 4 async tools）

直接调用 BS 业务层（同进程），不再走 HTTP。
提供 2 个只读工具：get_logs / get_current_config。
"""

import asyncio
import os
import re
from datetime import datetime
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from app.api.logs import _read_log_file
from app.core.config import config_manager
from app.core.config_schema import is_sensitive_field
from app.core.logging import resolved_dev_log_file_path

# 日志级别白名单（get_logs 参数校验）
VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


def _require_read_scope() -> None:
    """校验 access token 存在且含 read scope，否则抛出 ToolError。"""
    token = get_access_token()
    if token is None:
        raise ToolError("未找到访问令牌，无法读取。请提供含 read 权限的 access token。")
    scopes = getattr(token, "scopes", [])
    if "read" not in scopes:
        raise ToolError(f"权限不足：需要 read scope，当前 scope: {scopes}")


# 敏感字段回显掩码
_MASK = "***"

# MCP 侧专用敏感字段掩码集合：除 config_schema 的 sensitive_fields 外，
# 额外屏蔽 auth 段的高危字段（口令哈希 / 派生 Fernet 密钥的主密钥）。
# 不改动 config_schema.sensitive_fields，避免影响落盘加密语义。
_EXTRA_SENSITIVE_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {("auth", "password"), ("auth", "secret_key")}
)

# 日志行时间戳格式: [2026/09/03 12:00:00.123]
_LOG_TIMESTAMP_RE = re.compile(r"^\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]")


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


def _mask_sensitive(config_data: dict[str, Any]) -> dict[str, Any]:
    """将敏感字段值替换为掩码字符串（就地修改并返回）。"""
    for section, fields in config_data.items():
        if not isinstance(fields, dict):
            continue
        for key in list(fields.keys()):
            if (section, key) in _EXTRA_SENSITIVE_FIELDS:
                fields[key] = _MASK
            elif is_sensitive_field(section, key):
                fields[key] = _MASK
    return config_data


async def _dispatch_read_log(
    log_file_path: str,
    level: str | None,
    search: str | None,
    limit: str,
) -> dict[str, Any]:
    """在线程中读取日志文件（可被子类/测试覆盖）。"""
    return await asyncio.to_thread(_read_log_file, log_file_path, level, search, limit)


async def get_logs(
    level: str | None = None,
    search: str | None = None,
    limit: int = 50,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """查询 BS 日志，支持级别/搜索/时间范围过滤。

    Args:
        level: 日志级别过滤（DEBUG/INFO/WARNING/ERROR），可选。
        search: 关键词搜索（大小写不敏感），可选。
        limit: 返回行数上限（1~10000），默认 50。
        since: ISO 格式起始时间，可选。
        until: ISO 格式结束时间，可选。

    Returns:
        {"status": "success", "data": {"content": ..., "stats": {...}}}
    """
    # scope 校验：get_logs 要求 read 权限
    _require_read_scope()

    # 验证 level
    if level is not None:
        level_upper = level.upper()
        if level_upper == "WARN":
            level_upper = "WARNING"
        if level_upper not in VALID_LEVELS:
            raise ToolError(
                f"无效的日志级别: {level}，有效值为: {', '.join(sorted(VALID_LEVELS))}"
            )
        level = level_upper

    # 限制 limit 范围
    limit = max(1, min(limit, 10000))

    # 解析时间范围
    since_dt = None
    until_dt = None

    if since is not None:
        try:
            since_dt = _parse_iso_datetime(since)
        except ValueError:
            raise ToolError(f"无效的 since 时间格式: {since}")

    if until is not None:
        try:
            until_dt = _parse_iso_datetime(until)
        except ValueError:
            raise ToolError(f"无效的 until 时间格式: {until}")

    if since_dt is not None and until_dt is not None and since_dt > until_dt:
        raise ToolError("since 时间必须早于或等于 until 时间")

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

        if not os.path.exists(log_file_path):
            return {
                "status": "success",
                "data": {
                    "content": "",
                    "stats": {"size": 0, "lines": 0, "modified": None, "errors": 0},
                },
            }

        result = await _dispatch_read_log(log_file_path, level, search, str(limit))

        # 应用时间范围过滤
        if "content" in result:
            result["content"] = _filter_by_time_range(
                result["content"], since_dt, until_dt
            )
            result["stats"]["lines"] = result["content"].count("\n")

        return {"status": "success", "data": result}
    except ToolError:
        raise
    except Exception as e:
        # 不泄露内部路径/异常细节
        raise ToolError("获取日志失败，请检查日志文件配置") from e


async def get_current_config() -> dict[str, Any]:
    """读取当前配置，敏感字段已脱敏。

    Returns:
        {"status": "success", "data": {section: {key: value, ...}, ...}}
    """
    # scope 校验：get_current_config 要求 read 权限
    _require_read_scope()

    data = config_manager.get_all_config()
    _mask_sensitive(data)
    return {"status": "success", "data": data}

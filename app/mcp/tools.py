"""
MCP 工具函数（FastMCP 4 async tools）

直接调用 BS 业务层（同进程），不再走 HTTP。
提供 3 个工具：get_logs / get_current_config / update_config。
"""

import asyncio
import os
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from app.api.logs import _read_log_file
from app.api.mcp_config import _is_valid_section, _mask_sensitive
from app.api.mcp_logs import _filter_by_time_range, _parse_iso_datetime
from app.core.config import config_manager
from app.core.logging import resolved_dev_log_file_path

# 日志级别白名单（与 mcp_logs.py 保持一致）
VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


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
    data = config_manager.get_all_config()
    _mask_sensitive(data)
    return {"status": "success", "data": data}


async def update_config(
    section: str,
    key: str,
    value: Any,
) -> dict[str, Any]:
    """修改配置项，直接生效；auth 段不可修改。

    Args:
        section: 配置段名（支持下划线，自动归一化为连字符）。
        key: 配置键名。
        value: 配置值。

    Returns:
        {"status": "success", "message": "...", "data": {"section": ..., "key": ...}}

    Raises:
        ToolError: 非法段名或 auth 段拒绝。
    """
    # scope 校验：update_config 要求 write 权限
    token = get_access_token()
    if token is None:
        raise ToolError(
            "未找到访问令牌，无法修改配置。请提供含 write 权限的 access token。"
        )
    scopes = getattr(token, "scopes", [])
    if "write" not in scopes:
        raise ToolError(
            f"权限不足：update_config 需要 write scope，当前 scope: {scopes}"
        )

    # 归一化段名：下划线 → 连字符
    normalized = section.replace("_", "-")

    # auth 段黑名单
    if normalized == "auth":
        raise ToolError("auth 段不可通过 MCP 修改，请使用 Web 界面修改认证配置")

    # 校验段名合法性
    if not _is_valid_section(normalized):
        from app.core.config_schema import SECTIONS, multi_instance_prefixes

        valid_names: set[str] = set(SECTIONS.keys())
        valid_names.update(multi_instance_prefixes())
        raise ToolError(
            f"未知配置段: {section}。合法段包括: {', '.join(sorted(valid_names))}"
        )

    # 校验 key 合法性：非多实例段必须在 schema 字段列表中
    from app.core.config_schema import SECTIONS, multi_instance_prefixes

    _section_meta = SECTIONS.get(normalized)
    if _section_meta is None:
        # 多实例段（notify-webhook-1 等）：前缀匹配父段
        for prefix in multi_instance_prefixes():
            if normalized.startswith(f"{prefix}-"):
                _section_meta = SECTIONS.get(prefix)
                break

    if _section_meta is not None and _section_meta.fields:
        valid_keys = {f.name for f in _section_meta.fields}
        if key not in valid_keys:
            raise ToolError(
                f"非法配置键: {key}。段 {normalized} 的合法键包括: "
                f"{', '.join(sorted(valid_keys))}"
            )

    # 校验 value 长度上限（防止 INI 膨胀）
    _MAX_VALUE_LENGTH = 10000
    if isinstance(value, str) and len(value) > _MAX_VALUE_LENGTH:
        raise ToolError(f"配置值长度超限: {len(value)} > {_MAX_VALUE_LENGTH}")

    config_manager.set_config(normalized, key, value)

    return {
        "status": "success",
        "message": f"已更新配置: {normalized}.{key}",
        "data": {"section": normalized, "key": key},
    }

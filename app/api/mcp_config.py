"""
MCP 内部 API：配置读写端点

提供 /api/mcp/config（GET 读）、/api/mcp/config/schema（GET）、
/api/mcp/config/update（POST 写）三个端点，供 MCP 客户端读写项目配置。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import config_manager
from ..core.config_schema import (
    SECTIONS,
    is_sensitive_field,
    multi_instance_prefixes,
    serialize_schema,
)
from .deps import get_mcp_client

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# 敏感字段回显掩码
_MASK = "***"

# 模块级依赖实例，确保 dependency_overrides key 匹配
_mcp_read_dep = get_mcp_client()
_mcp_write_dep = get_mcp_client(require_scope="write")


def _valid_section_names() -> set[str]:
    """返回所有合法段名集合（含多实例段前缀）。"""
    names: set[str] = set(SECTIONS.keys())
    # 多实例段前缀（如 notify-webhook、notify-email、summary 等）
    names.update(multi_instance_prefixes())
    return names


def _is_valid_section(normalized: str) -> bool:
    """判断归一化后的段名是否合法（支持多实例段前缀匹配）。"""
    if normalized in _valid_section_names():
        return True
    # 多实例段前缀匹配：notify-webhook-1 → notify-webhook
    for prefix in multi_instance_prefixes():
        if normalized.startswith(f"{prefix}-"):
            return True
    return False


def _mask_sensitive(config_data: dict[str, Any]) -> dict[str, Any]:
    """将敏感字段值替换为掩码字符串（就地修改并返回）。"""
    for section, fields in config_data.items():
        if not isinstance(fields, dict):
            continue
        for key in list(fields.keys()):
            if is_sensitive_field(section, key):
                fields[key] = _MASK
    return config_data


@router.get("/config")
async def get_config(
    current_user: dict = Depends(_mcp_read_dep),
) -> dict[str, Any]:
    """获取全量配置（敏感字段已脱敏）。"""
    data = config_manager.get_all_config()
    _mask_sensitive(data)
    return {"status": "success", "data": data}


@router.get("/config/schema")
async def get_config_schema(
    current_user: dict = Depends(_mcp_read_dep),
) -> dict[str, Any]:
    """获取配置段元数据 schema。"""
    return {"status": "success", "data": serialize_schema()}


@router.post("/config/update")
async def update_config(
    payload: dict[str, Any],
    current_user: dict = Depends(_mcp_write_dep),
) -> dict[str, Any]:
    """更新配置并落盘。

    请求体格式：{section: {key: value, ...}, ...}
    段名支持下划线形式（自动归一化为连字符）。
    """
    if not payload:
        raise HTTPException(status_code=400, detail="请求体不能为空")

    # 禁止通过 MCP 修改 auth 段（Web 认证配置应仅通过 Web 界面修改）
    for section in payload:
        normalized = section.replace("_", "-")
        if normalized == "auth":
            raise HTTPException(
                status_code=403,
                detail="auth 段不可通过 MCP 修改，请使用 Web 界面修改认证配置",
            )

    valid_names = _valid_section_names()

    # 校验所有段名
    invalid_sections = []
    normalized_map = {}  # 原始 → 归一化
    for section in payload:
        normalized = section.replace("_", "-")
        normalized_map[section] = normalized
        if not _is_valid_section(normalized):
            invalid_sections.append(section)

    if invalid_sections:
        raise HTTPException(
            status_code=400,
            detail=(
                f"未知配置段: {', '.join(invalid_sections)}。"
                f"合法段包括: {', '.join(sorted(valid_names))}"
            ),
        )

    # 执行变更
    changed: list[dict[str, str]] = []
    for section, items in payload.items():
        if not isinstance(items, dict):
            continue
        normalized = normalized_map[section]
        for key, value in items.items():
            # 跳过空值/掩码值，避免误覆盖
            if value is None or (
                isinstance(value, str) and value.strip() in ("", _MASK)
            ):
                continue
            config_manager.set_config(normalized, key, value)
            changed.append({"section": normalized, "key": key})

    return {
        "status": "success",
        "message": f"已更新 {len(changed)} 项配置",
        "data": {"changed": changed},
    }

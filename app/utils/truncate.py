"""展示层截断工具（读取/展示侧截断，写入路径零截断）。

从 ``app/services/agent/trace.py`` 迁移而来，供展示层（T5）使用。
写入路径不再出现任何截断；截断仅发生在读取/展示侧。

语义：
- 小 payload：原样返回合法 JSON。
- 大 payload：结构化截断，保证结果仍是合法 JSON；截断时以降级包壳
  ``{"truncated": true, "preview": "<前缀>...[shrinked]"}`` 呈现，
  ``preview`` 字段以 ``...[shrinked]`` 标记结尾（供展示层检测截断）。
"""

from __future__ import annotations

import json
from typing import Any

# 观测摘要上限（截断 ≤2KB）
MAX_PAYLOAD_JSON_BYTES = 2 * 1024
SHRINKED_MARKER = "...[shrinked]"
# _shrink 递归深度上限：超过此深度以占位符替代，防止病态嵌套触发 RecursionError
MAX_SHRINK_DEPTH = 32
DEPTH_EXCEEDED_MARKER = "[max-depth-exceeded]"


def _shrink(obj: Any, max_bytes: int, _depth: int = 0) -> Any:
    """递归截断字符串叶子，使 JSON 序列化后 ≤ max_bytes（近似自底向上）。

    超过 MAX_SHRINK_DEPTH 的深层嵌套以占位符替代，避免 RecursionError。
    """
    if _depth >= MAX_SHRINK_DEPTH:
        return DEPTH_EXCEEDED_MARKER
    if len(json.dumps(obj, ensure_ascii=False).encode("utf-8")) <= max_bytes:
        return obj
    if isinstance(obj, str):
        return obj[: max(0, max_bytes - 16)]
    if isinstance(obj, dict):
        return {k: _shrink(v, max_bytes, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shrink(v, max_bytes, _depth + 1) for v in obj]
    return obj


def _safe_json_dumps(obj: Any, max_bytes: int) -> str | None:
    """尝试 json.dumps，深度嵌套导致 RecursionError 时返回 None。"""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except RecursionError:
        return None


def truncate_json(payload: Any, max_bytes: int = MAX_PAYLOAD_JSON_BYTES) -> str:
    """将 payload 序列化为 JSON 字符串，并在超出 max_bytes 时结构化截断。

    - 小 payload：原样返回合法 JSON。
    - 大 payload：先尝试递归缩短字符串叶子以保留原始结构；若仍超限，降级为
      ``{"truncated": true, "preview": "<前缀>...[shrinked]"}`` 包壳，确保：
      结果仍是合法 JSON、大小 ≤ max_bytes、且 ``preview`` 以 ``...[shrinked]`` 结尾。
    - 病态深嵌套（序列化本身触发 RecursionError）：直接走降级包壳，以占位
      标记替代，保证不抛异常且结果合法。
    """
    if isinstance(payload, str):
        text = payload
    else:
        dumped = _safe_json_dumps(payload, max_bytes)
        if dumped is None:
            # 序列化即 RecursionError（病态深嵌套）：直接降级包壳
            return _build_truncation_shell("", max_bytes)
        text = dumped
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    try:
        obj = json.loads(text) if isinstance(payload, str) else payload
    except (ValueError, TypeError):
        obj = text
    shrunk = _shrink(obj, max_bytes)
    result = json.dumps(shrunk, ensure_ascii=False)
    if len(result.encode("utf-8")) <= max_bytes:
        return result
    # 兜底：包成截断预览，保证合法 JSON、大小上限、preview 以标记结尾
    return _build_truncation_shell(text, max_bytes)


def _build_truncation_shell(text: str, max_bytes: int) -> str:
    """构建降级包壳 ``{"truncated": true, "preview": "<前缀>...[shrinked]"}``。

    保证结果合法 JSON、大小 ≤ max_bytes、preview 以 SHRINKED_MARKER 结尾。
    preview 由原始文本切片构成，嵌入 JSON 字符串时引号/反斜杠会被转义而膨胀，
    因此用迭代缩减保证最终序列化结果 ≤ max_bytes。
    """
    prefix = '{"truncated":true,"preview":"'
    suffix = '"}'
    overhead = len(prefix.encode("utf-8")) + len(suffix.encode("utf-8"))
    # 保守初始预算：预留标记空间后取半（最坏情况每字符都转义为 2 字节）
    budget = max(0, (max_bytes - overhead - len(SHRINKED_MARKER.encode("utf-8"))) // 2)
    while budget > 0:
        preview = text[:budget] + SHRINKED_MARKER
        result = prefix + preview + suffix
        if len(result.encode("utf-8")) <= max_bytes:
            # 验证 json 合法（preview 中的控制字符需被转义）；不合法则缩预算
            try:
                json.loads(result)
                return result
            except (ValueError, TypeError):
                pass
        budget -= 1
    # 预算耗尽：仅返回标记包壳
    return prefix + SHRINKED_MARKER + suffix

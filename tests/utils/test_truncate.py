"""展示层截断工具测试（truncate_json / _shrink / MAX_PAYLOAD_JSON_BYTES）。

覆盖：
- 小 payload 原样通过（合法 JSON）
- 大 payload 截断后仍合法 JSON 且 ≤ 上限
- 截断结果以 ...[shrinked] 标记结尾
- 字符串输入同样处理
"""

from __future__ import annotations

import json

from app.utils.truncate import (
    MAX_PAYLOAD_JSON_BYTES,
    SHRINKED_MARKER,
    truncate_json,
)


class TestTruncateJsonSmallPassthrough:
    def test_small_dict_passthrough(self):
        small = {"a": 1}
        out = truncate_json(small)
        assert out == '{"a": 1}'
        json.loads(out)  # 合法

    def test_small_string_passthrough(self):
        # 字符串输入原样返回（保持与 trace.truncate_json 一致的行为）
        out = truncate_json("hi")
        assert out == "hi"


class TestTruncateJsonLargeBoundedAndValid:
    def test_large_dict_truncated_to_valid_json_and_bounded(self):
        big = {"content": "x" * 5000, "meta": "keep"}
        out = truncate_json(big)
        parsed = json.loads(out)  # 必须可解析
        assert isinstance(parsed, dict)
        assert len(out.encode("utf-8")) <= MAX_PAYLOAD_JSON_BYTES

    def test_large_string_truncated_to_valid_json_and_bounded(self):
        big = "y" * 5000
        out = truncate_json(big)
        parsed = json.loads(out)  # 必须可解析
        assert isinstance(parsed, str)
        assert len(out.encode("utf-8")) <= MAX_PAYLOAD_JSON_BYTES


class TestTruncateJsonShrinkedMarker:
    def test_truncated_result_preview_ends_with_marker(self):
        """超大 payload 降级包壳：preview 字段以 ...[shrinked] 标记结尾。

        多字段大值使 _shrink 无法落入上限，强制走降级包壳路径。
        """
        huge = {f"field_{i}": "z" * 5000 for i in range(10)}
        out = truncate_json(huge)
        parsed = json.loads(out)
        assert parsed.get("truncated") is True
        preview = parsed.get("preview")
        assert isinstance(preview, str)
        assert preview.endswith(SHRINKED_MARKER)

    def test_truncated_result_is_valid_json_and_bounded(self):
        """截断结果必须是合法 JSON 且 ≤ 上限。"""
        huge = {f"field_{i}": "z" * 5000 for i in range(10)}
        out = truncate_json(huge)
        json.loads(out)  # 合法
        assert len(out.encode("utf-8")) <= MAX_PAYLOAD_JSON_BYTES

    def test_custom_max_bytes(self):
        """自定义上限同样生效。"""
        payload = "a" * 200
        out = truncate_json(payload, max_bytes=50)
        json.loads(out)
        assert len(out.encode("utf-8")) <= 50


class TestTruncateJsonDeepNesting:
    def test_deep_nested_does_not_raise_recursion_error(self):
        """1000+ 层嵌套不触发 RecursionError，返回合法 JSON。

        病态构造：内层字符串足够大，使每一层的序列化结果都超上限，
        强制 _shrink 在每一层都递归 → 无保护时触发 RecursionError。
        """
        deep = "x" * 3000
        for _ in range(1000):
            deep = {"n": deep}
        out = truncate_json(deep)
        json.loads(out)  # 必须可解析

    def test_deep_nested_returns_bounded_json(self):
        """超深嵌套截断后仍 ≤ 上限。"""
        deep = "x" * 3000
        for _ in range(1200):
            deep = {"wrap": deep}
        out = truncate_json(deep)
        assert len(out.encode("utf-8")) <= MAX_PAYLOAD_JSON_BYTES
        json.loads(out)

    def test_deep_nested_list_does_not_raise(self):
        """深层嵌套列表同样受保护。"""
        deep = ["x" * 3000]
        for _ in range(1100):
            deep = [deep]
        out = truncate_json(deep)
        json.loads(out)

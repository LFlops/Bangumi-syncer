"""llm 层只放通用设施：领域能力不得上浮（防回流守卫）。

对应评审意见 ``app/services/llm/output_parser.py:1``「应该迁移到 matching 模块，
理由：实际非通用能力，专为 matching 使用」。

领域能力一旦上浮进 ``llm`` 这类「通用」层，就会被其它场景误用（耦合伪装成复用），
且删除/改签名的影响面被放大。本文件把这条边界固化为可执行断言。
"""

from __future__ import annotations

import ast
from pathlib import Path

_LLM_DIR = Path("app/services/llm")
_MATCHING_DIR = Path("app/services/matching")

#: 领域概念标识（出现即视为领域能力上浮到 llm 层）。
_DOMAIN_TOKENS = (
    "subject_id",
    "sync_record",
    "bangumi",
    "Bangumi",
    "candidate_subject_id",
)


def test_output_parser_lives_in_matching_not_llm():
    """output_parser 归属 matching（原 llm 层），llm 层不得回流。"""
    assert (_MATCHING_DIR / "output_parser.py").exists(), (
        "output_parser 应位于 app/services/matching/"
    )
    assert not (_LLM_DIR / "output_parser.py").exists(), (
        "output_parser 不得回到 app/services/llm/（领域能力非通用设施）"
    )


def test_llm_layer_has_no_domain_specific_code():
    """llm 层源码不得出现领域概念标识（sync_record / subject_id / bangumi…）。"""
    offenders: dict[str, list[str]] = {}
    for path in sorted(_LLM_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = [tok for tok in _DOMAIN_TOKENS if tok in text]
        if hits:
            offenders[str(path)] = hits
    assert offenders == {}, f"llm 层应只含通用设施，检测到领域概念: {offenders}"


def test_output_parser_consumer_is_matching_only():
    """output_parser 只被 matching **导入**（注释/文档提及不算消费者）。

    用 AST 判真实 import：docstring 里说明「场景侧 output_parser 兜底解析」是
    合法交叉引用（agent / provider 层需要知道兜底存在），但不能成为依赖。
    """
    consumers: list[str] = []
    for base in (Path("app"), Path("tests"), Path("eval")):
        for path in sorted(base.rglob("*.py")):
            if path.name in ("output_parser.py", "test_output_parser.py"):
                continue
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                mod = None
                if isinstance(node, ast.ImportFrom):
                    mod = node.module
                elif isinstance(node, ast.Import):
                    mod = ",".join(a.name for a in node.names)
                if mod and "output_parser" in mod:
                    consumers.append(str(path))
                    break
    assert consumers, "output_parser 应至少有一个消费者（否则是死代码）"
    assert all("matching" in c for c in consumers), (
        f"output_parser 只应被 matching 导入，实际导入方: {consumers}"
    )

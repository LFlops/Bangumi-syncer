"""记忆数据模型（re-export）。

共享定义已迁移至 :mod:`app.models.memory`（供 core/database 与 services 共同引用，
消除 core → services 的倒置依赖）。本模块保留 re-export 以兼容现有 import。
"""

from app.models.memory import MemoryEntry  # noqa: F401

__all__ = ["MemoryEntry"]

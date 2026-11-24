"""可追查路径的分块 Max-Plus 矩阵合成与区间查询。

模块划分
--------
- :mod:`maxpath.model`      片段、修订、视图等不可变数据结构与范围校验
- :mod:`maxpath.store`      内容寻址节点存储（内存 / SQLite 两种后端）
- :mod:`maxpath.engine`     批量合成、字典序并列裁决、筛查、区间查询与解释
- :mod:`maxpath.publication` 出版快照凭据与保留期重放
- :mod:`maxpath.case`       采集资料（capture.json）装载适配
"""
from __future__ import annotations

from .model import Edge, SegmentSpec, SegmentView, ViewPlan, QuerySpec, QueryResult
from .engine import MaxPlusEngine
from .store import MemoryStore, SqliteStore
from .publication import PublicationBook
from .case import load_capture

__all__ = [
    "Edge",
    "SegmentSpec",
    "SegmentView",
    "ViewPlan",
    "QuerySpec",
    "QueryResult",
    "MaxPlusEngine",
    "MemoryStore",
    "SqliteStore",
    "PublicationBook",
    "load_capture",
]

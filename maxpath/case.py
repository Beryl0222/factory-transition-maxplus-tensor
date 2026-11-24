"""采集资料（capture.json）到引擎数据模型的适配器。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from .model import QuerySpec, SegmentSpec


def load_capture(path: str | Path) -> Tuple[List[str], Dict[str, SegmentSpec], List[dict], List[dict]]:
    """读取采集文件，返回 (片段顺序, 原始片段版本表, 修订列表, 查询列表)。

    与 ``caseio.load_case`` 不同，本函数不依赖 numpy（合成引擎不需要张量
    展开），并把边元组直接校验为 :class:`SegmentSpec`。
    """
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    records = envelope["records"]
    order = list(records["order"])
    segments: Dict[str, SegmentSpec] = {}
    for name, raw in records["segments"].items():
        segments[name] = SegmentSpec.create(
            name=name,
            edges=[tuple(e) for e in raw["edge"]],
            input_states=raw["input"],
            output_states=raw["output"],
        )
    revisions = list(records.get("revisions", []))
    queries = list(records.get("queries", []))
    return order, segments, revisions, queries


def apply_revision(
    versions: Mapping[str, SegmentSpec], revision: dict
) -> Dict[str, SegmentSpec]:
    """在版本集合上应用一条修订，返回新的版本表（不修改入参）。

    修订既可能只改边得分（交界字典不变），也可能改交界状态字典，
    两种情况都生成独立的 :class:`SegmentSpec` 身份，由视图层检测断裂。
    """
    target = revision["replaces"]
    if target not in versions:
        raise KeyError(f"修订 {revision['id']} 指向未知片段 {target!r}")
    updated = SegmentSpec.create(
        name=target,
        edges=[tuple(e) for e in revision["edge"]],
        input_states=revision["input"],
        output_states=revision["output"],
        revision_id=revision["id"],
        replaces=target,
    )
    result = dict(versions)
    result[target] = updated
    return result


def build_versions(
    base: Mapping[str, SegmentSpec], revision_ids: Sequence[str], revisions: Sequence[dict]
) -> Dict[str, SegmentSpec]:
    """按修订 ID 列表依次套用，得到一个视图的版本集合。"""
    by_id = {rev["id"]: rev for rev in revisions}
    versions = dict(base)
    for rev_id in revision_ids:
        if rev_id not in by_id:
            raise KeyError(f"未知修订 {rev_id!r}")
        versions = apply_revision(versions, by_id[rev_id])
    return versions


def query_from_capture(raw: dict) -> QuerySpec:
    return QuerySpec.create(
        start_segment=raw["range"][0],
        end_segment=raw["range"][1],
        start_state=raw["start"],
        end_state=raw["end"],
        excluded=raw.get("excluded", []),
    )

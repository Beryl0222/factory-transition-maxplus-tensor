"""出版快照、凭据重放与保留期管理。

语义
====
* 一次 *出版* 冻结某个视图（区间一致的版本集合）的全部段树节点身份，并颁发
  不透明凭据 token。复核员在保留期内凭 token 重放查询，命中的仍是当时的
  内容寻址节点——即使后续提交了修订或筛查产生了新节点，旧节点不会被改动。
* 每个主计划（``plan_key``）只保留最近 ``retain`` 个有效快照（交接要求：
  当前 + 上一个）。超出保留期的快照被吊销，其独占节点由标记-清除回收；
  仍被其他快照或在线视图引用的节点一律保留，实现“只有受修订或筛查影响的
  合成结果和证据才重算”。
* 元数据可选用 JSON 边车持久化（配合 :class:`SqliteStore`），不传路径时
  仅保存在进程内（配合 :class:`MemoryStore` 与测试）。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .engine import MaxPlusEngine
from .model import QueryResult, QuerySpec


@dataclass(frozen=True)
class Publication:
    """一个不可变出版快照的元数据。"""

    token: str
    plan_key: str
    view_id: str
    created_at: str
    order: Tuple[str, ...]
    revisions: Dict[str, str]
    breaks: Tuple[Tuple[int, int, str, str, Tuple[str, ...], Tuple[str, ...]], ...]
    node_ids: Tuple[str, ...]
    description: str = ""
    revoked: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["order"] = list(self.order)
        data["node_ids"] = list(self.node_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Publication":
        return cls(
            token=data["token"],
            plan_key=data["plan_key"],
            view_id=data["view_id"],
            created_at=data["created_at"],
            order=tuple(data["order"]),
            revisions=dict(data["revisions"]),
            breaks=tuple(
                (b[0], b[1], b[2], b[3], tuple(b[4]), tuple(b[5]))
                for b in data["breaks"]
            ),
            node_ids=tuple(data["node_ids"]),
            description=data.get("description", ""),
            revoked=data.get("revoked", False),
        )


class PublicationBook:
    """管理出版凭据、保留期轮转、重放与存储回收。"""

    def __init__(
        self,
        engine: MaxPlusEngine,
        plan_key: str = "main",
        retain: int = 2,
        state_path: Optional[str] = None,
    ) -> None:
        if retain < 1:
            raise ValueError("保留快照数至少为 1")
        self.engine = engine
        self.plan_key = plan_key
        self.retain = retain
        self.state_path = Path(state_path) if state_path else None
        # token -> Publication（含已吊销记录，重放时给出明确错误）
        self._records: Dict[str, Publication] = {}
        if self.state_path and self.state_path.exists():
            self._load()

    # ------------------------------------------------------------------
    def publish(self, view_id: str, description: str = "") -> Publication:
        """出版视图快照并轮转保留期，返回新快照凭据。"""
        plan = self.engine.get_view(view_id)
        node_ids = self._collect_view_node_ids(view_id)
        publication = Publication(
            token=secrets.token_hex(16),
            plan_key=self.plan_key,
            view_id=view_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            order=plan.order,
            revisions={sv.name: sv.revision_id for sv in plan.segments},
            breaks=plan.boundary_breaks,
            node_ids=tuple(sorted(node_ids)),
            description=description,
        )
        self._records[publication.token] = publication
        self._rotate()
        self._garbage_collect()
        self._save()
        return publication

    def replay(self, token: str, query: QuerySpec) -> QueryResult:
        """凭出版凭据重放查询；快照吊销或被篡改时拒绝。"""
        record = self._require_live(token)
        plan = self.engine.get_view(record.view_id)
        # 完整性核对：视图的版本集合必须与出版时逐片段一致。
        current = {sv.name: sv.revision_id for sv in plan.segments}
        if current != record.revisions or plan.order != record.order:
            raise RuntimeError(
                f"快照 {token} 的视图 {record.view_id} 版本集合已偏离出版内容"
            )
        return self.engine.query(record.view_id, query)

    def get(self, token: str) -> Publication:
        try:
            return self._records[token]
        except KeyError as exc:
            raise KeyError(f"未知出版凭据 {token!r}") from exc

    def list_live(self) -> List[Publication]:
        return [
            rec
            for rec in self._records.values()
            if not rec.revoked and rec.plan_key == self.plan_key
        ]

    # ------------------------------------------------------------------
    def _require_live(self, token: str) -> Publication:
        record = self.get(token)
        if record.revoked:
            raise PermissionError(f"快照 {token} 已超出保留期吊销，无法重放")
        if record.plan_key != self.plan_key:
            raise KeyError(f"凭据 {token} 不属于主计划 {self.plan_key!r}")
        return record

    def _rotate(self) -> None:
        """每个主计划仅保留最近 retain 个快照，其余标记吊销。"""
        grouped = [
            rec
            for rec in self._records.values()
            if rec.plan_key == self.plan_key and not rec.revoked
        ]
        grouped.sort(key=lambda rec: rec.created_at)
        for stale in grouped[: -self.retain] if len(grouped) > self.retain else []:
            self._records[stale.token] = replace(stale, revoked=True)

    def _garbage_collect(self) -> None:
        """标记-清除：回收无任何存活快照或在线视图引用的节点。"""
        reachable: set[str] = set()
        for record in self._records.values():
            if not record.revoked:
                reachable.update(record.node_ids)
        for view_id in self.engine._views:  # noqa: SLF001 - 同包协作
            reachable.update(self._collect_view_node_ids(view_id))
        all_nodes = set(self.engine.store.iter_node_ids())
        self.engine.store.delete_nodes(all_nodes - reachable)
        self.engine.store.commit()

    def _collect_view_node_ids(self, view_id: str) -> set[str]:
        ids: set[str] = set()
        for level in self.engine._tree_levels[view_id]:  # noqa: SLF001
            ids.update(node_id for node_id in level if node_id is not None)
        ids.update(
            node_id
            for (vid, _, _, _), node_id in self.engine._shadow_cache.items()  # noqa: SLF001
            if vid == view_id
        )
        return ids

    # ------------------------------------------------------------------
    def _load(self) -> None:
        assert self.state_path is not None
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._records = {
            token: Publication.from_dict(data) for token, data in raw.items()
        }

    def _save(self) -> None:
        if self.state_path is None:
            return
        payload = {token: rec.to_dict() for token, rec in self._records.items()}
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

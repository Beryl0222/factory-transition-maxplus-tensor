"""内容寻址的 Max-Plus 合成节点存储。

设计要点
--------
1. 每个合成节点（叶子片段或内部合成结果）由其 *内容* 的 SHA-256 摘要标识：
   叶子由位置、版本、保留边集合决定；内部节点由区间与两个子节点 ID 决定。
   因此不同视图、不同查询、不同合并树形状下的等价结果自动共享同一份存储，
   修订或筛查只让受影响的子树产生新 ID，其余结果原样复用。

2. 单元格只保存 O(1) 的追溯信息（叶子：一条源边 ID；内部：分裂交界状态与
   左右子单元格坐标），**绝不物化完整边序列**。完整路径在查询时沿指针链
   按需重建，满足大维度下工作区无法容纳每对端点完整路径的限制。

3. 每个单元格带一个节点内全局 ``rank``：同一节点内按完整边 ID 序列字典序
   排名（实现时只用子节点 rank 做 O(1) 比较，不物化序列）。父节点候选的
   并列比较因此退化为整数比较，且结果与合并树形状无关（见 engine 模块）。

提供两种后端：
- :class:`MemoryStore`  进程内字典，适合小工作区与测试；
- :class:`SqliteStore`  单文件持久化，构建过程流式落盘，进程内存保持常量级。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

#: 不可达坐标在存储层根本不存在；负数是合法得分，不使用哨兵值。
CellPointer = Tuple[str, str, str]  # (node_id, in_state, out_state)


@dataclass(frozen=True)
class NodeRecord:
    """一个合成节点的元数据。"""

    node_id: str
    kind: str  # "leaf" | "internal"
    lo: int
    hi: int
    input_states: Tuple[str, ...]
    output_states: Tuple[str, ...]
    # 叶子字段
    segment_name: Optional[str] = None
    revision_id: Optional[str] = None
    excluded: Tuple[str, ...] = ()
    # 内部字段
    split: Optional[int] = None
    left_id: Optional[str] = None
    right_id: Optional[str] = None
    junction: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CellRecord:
    """节点内一个 ``(入口状态, 出口状态)`` 单元格。

    ``lex_key`` 是该单元格完整边 ID 序列的 *嵌套* 字典序键（Python 对象）：
    叶子为 ``[edge_id]``；内部单元格为 ``[左子键, 右子键]``。
    嵌套 list 的逐项比较与展开后的完整边 ID 序列 ASCII 字典序严格一致，
    嵌套深度只等于树高，不物化完整路径。SQLite 后端以 JSON 文本持久化，
    读取后还原为嵌套 list；所有比较都在 Python 对象上进行。
    """

    in_state: str
    out_state: str
    score: int
    rank: int
    lex_key: list
    edge_id: Optional[str] = None
    split_state: Optional[str] = None
    left: Optional[CellPointer] = None
    right: Optional[CellPointer] = None


def _canonical_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def leaf_content_id(
    position: int,
    revision_id: str,
    input_states: Iterable[str],
    output_states: Iterable[str],
    edges: Iterable[Tuple[str, str, str, int]],
) -> str:
    """叶子节点内容 ID：版本与实际保留边（筛查结果）的函数。"""
    payload = [
        "L",
        position,
        revision_id,
        list(input_states),
        list(output_states),
        sorted((eid, entry, ex, w) for eid, entry, ex, w in edges),
    ]
    digest = hashlib.sha256(_canonical_dumps(payload).encode("utf-8"))
    return "L" + digest.hexdigest()


def internal_content_id(
    lo: int,
    hi: int,
    left_id: str,
    right_id: str,
    junction: Iterable[str],
) -> str:
    """内部节点内容 ID：区间、子节点 ID、共享交界字典的函数。"""
    payload = ["I", lo, hi, left_id, right_id, sorted(junction)]
    digest = hashlib.sha256(_canonical_dumps(payload).encode("utf-8"))
    return "I" + digest.hexdigest()


class NodeStore:
    """存储后端共同接口。"""

    def has_node(self, node_id: str) -> bool:
        raise NotImplementedError

    def get_node(self, node_id: str) -> Optional[NodeRecord]:
        raise NotImplementedError

    def put_node(self, record: NodeRecord, cells: Iterable[CellRecord]) -> str:
        raise NotImplementedError

    def get_cell(
        self, node_id: str, in_state: str, out_state: str
    ) -> Optional[CellRecord]:
        raise NotImplementedError

    def iter_cells(self, node_id: str) -> List[CellRecord]:
        raise NotImplementedError

    def iter_node_ids(self) -> List[str]:
        raise NotImplementedError

    def delete_nodes(self, node_ids: Iterable[str]) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        """批量构建后的提交点（SQLite 后端用于落盘，内存后端为空操作）。"""

    def close(self) -> None:
        """释放底层资源；内存后端为空操作。"""


class MemoryStore(NodeStore):
    """进程内字典后端。"""

    def __init__(self) -> None:
        self._nodes: Dict[str, NodeRecord] = {}
        self._cells: Dict[str, Dict[Tuple[str, str], CellRecord]] = {}

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def get_node(self, node_id: str) -> Optional[NodeRecord]:
        return self._nodes.get(node_id)

    def put_node(self, record: NodeRecord, cells: Iterable[CellRecord]) -> str:
        if record.node_id in self._nodes:
            return record.node_id
        cell_map = {(c.in_state, c.out_state): c for c in cells}
        self._nodes[record.node_id] = record
        self._cells[record.node_id] = cell_map
        return record.node_id

    def get_cell(
        self, node_id: str, in_state: str, out_state: str
    ) -> Optional[CellRecord]:
        return self._cells.get(node_id, {}).get((in_state, out_state))

    def iter_cells(self, node_id: str) -> List[CellRecord]:
        return list(self._cells.get(node_id, {}).values())

    def iter_node_ids(self) -> List[str]:
        return list(self._nodes)

    def delete_nodes(self, node_ids: Iterable[str]) -> None:
        for node_id in node_ids:
            self._nodes.pop(node_id, None)
            self._cells.pop(node_id, None)

    def close(self) -> None:
        return None


class SqliteStore(NodeStore):
    """SQLite 单文件后端；节点与单元格流式落盘，内存占用为常量级。

    写入采用显式事务：引擎在一次批量构建前后调用 :meth:`begin` /
    :meth:`commit`，使上万节点的构建只付出一次提交成本。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS nodes (
        node_id       TEXT PRIMARY KEY,
        kind          TEXT NOT NULL,
        lo            INTEGER NOT NULL,
        hi            INTEGER NOT NULL,
        input_json    TEXT NOT NULL,
        output_json   TEXT NOT NULL,
        segment       TEXT,
        revision      TEXT,
        excluded_json TEXT,
        split         INTEGER,
        left_id       TEXT,
        right_id      TEXT,
        junction_json TEXT
    );
    CREATE TABLE IF NOT EXISTS cells (
        node_id     TEXT NOT NULL,
        in_state    TEXT NOT NULL,
        out_state   TEXT NOT NULL,
        score       INTEGER NOT NULL,
        rank        INTEGER NOT NULL,
        lex_key     TEXT NOT NULL,
        edge_id     TEXT,
        split_state TEXT,
        l_node      TEXT,
        l_in        TEXT,
        l_out        TEXT,
        r_node      TEXT,
        r_in        TEXT,
        r_out        TEXT,
        PRIMARY KEY (node_id, in_state, out_state)
    );
    CREATE INDEX IF NOT EXISTS idx_cells_node ON cells(node_id);
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self.SCHEMA)
        self._in_tx = False

    # -- 事务控制 -------------------------------------------------------
    def begin(self) -> None:
        if not self._in_tx:
            self._conn.execute("BEGIN")
            self._in_tx = True

    def commit(self) -> None:
        if self._in_tx:
            self._conn.commit()
            self._in_tx = False

    def has_node(self, node_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM nodes WHERE node_id = ? LIMIT 1", (node_id,)
        )
        return cur.fetchone() is not None

    def get_node(self, node_id: str) -> Optional[NodeRecord]:
        cur = self._conn.execute(
            "SELECT node_id, kind, lo, hi, input_json, output_json, "
            "segment, revision, excluded_json, split, left_id, right_id, "
            "junction_json FROM nodes WHERE node_id = ?",
            (node_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return NodeRecord(
            node_id=row[0],
            kind=row[1],
            lo=row[2],
            hi=row[3],
            input_states=tuple(json.loads(row[4])),
            output_states=tuple(json.loads(row[5])),
            segment_name=row[6],
            revision_id=row[7],
            excluded=tuple(json.loads(row[8])) if row[8] else (),
            split=row[9],
            left_id=row[10],
            right_id=row[11],
            junction=tuple(json.loads(row[12])) if row[12] else (),
        )

    def put_node(self, record: NodeRecord, cells: Iterable[CellRecord]) -> str:
        if self.has_node(record.node_id):
            return record.node_id
        if not self._in_tx:
            self._conn.execute("BEGIN")
            self._in_tx = True
        self._conn.execute(
            "INSERT INTO nodes (node_id, kind, lo, hi, input_json, output_json, "
            "segment, revision, excluded_json, split, left_id, right_id, "
            "junction_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.node_id,
                record.kind,
                record.lo,
                record.hi,
                json.dumps(list(record.input_states), ensure_ascii=False),
                json.dumps(list(record.output_states), ensure_ascii=False),
                record.segment_name,
                record.revision_id,
                json.dumps(list(record.excluded), ensure_ascii=False)
                if record.excluded
                else None,
                record.split,
                record.left_id,
                record.right_id,
                json.dumps(list(record.junction), ensure_ascii=False)
                if record.junction
                else None,
            ),
        )
        rows = [
            (
                record.node_id,
                c.in_state,
                c.out_state,
                c.score,
                c.rank,
                json.dumps(c.lex_key, ensure_ascii=False, separators=(",", ":")),
                c.edge_id,
                c.split_state,
                c.left[0] if c.left else None,
                c.left[1] if c.left else None,
                c.left[2] if c.left else None,
                c.right[0] if c.right else None,
                c.right[1] if c.right else None,
                c.right[2] if c.right else None,
            )
            for c in cells
        ]
        self._conn.executemany(
            "INSERT INTO cells (node_id, in_state, out_state, score, rank, "
            "lex_key, edge_id, split_state, l_node, l_in, l_out, r_node, r_in, r_out) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return record.node_id

    def get_cell(
        self, node_id: str, in_state: str, out_state: str
    ) -> Optional[CellRecord]:
        cur = self._conn.execute(
            "SELECT in_state, out_state, score, rank, lex_key, edge_id, split_state, "
            "l_node, l_in, l_out, r_node, r_in, r_out FROM cells "
            "WHERE node_id = ? AND in_state = ? AND out_state = ?",
            (node_id, in_state, out_state),
        )
        row = cur.fetchone()
        return None if row is None else _row_to_cell(row)

    def iter_cells(self, node_id: str) -> List[CellRecord]:
        cur = self._conn.execute(
            "SELECT in_state, out_state, score, rank, lex_key, edge_id, split_state, "
            "l_node, l_in, l_out, r_node, r_in, r_out FROM cells "
            "WHERE node_id = ?",
            (node_id,),
        )
        return [_row_to_cell(row) for row in cur.fetchall()]

    def iter_node_ids(self) -> List[str]:
        return [r[0] for r in self._conn.execute("SELECT node_id FROM nodes")]

    def delete_nodes(self, node_ids: Iterable[str]) -> None:
        ids = list(node_ids)
        if not ids:
            return
        if not self._in_tx:
            self._conn.execute("BEGIN")
            self._in_tx = True
        self._conn.executemany("DELETE FROM cells WHERE node_id = ?", [(i,) for i in ids])
        self._conn.executemany("DELETE FROM nodes WHERE node_id = ?", [(i,) for i in ids])

    def close(self) -> None:
        self.commit()
        self._conn.close()


def _row_to_cell(row: sqlite3.Row | tuple) -> CellRecord:
    left = (row[7], row[8], row[9]) if row[7] is not None else None
    right = (row[10], row[11], row[12]) if row[10] is not None else None
    return CellRecord(
        in_state=row[0],
        out_state=row[1],
        score=row[2],
        rank=row[3],
        lex_key=json.loads(row[4]),
        edge_id=row[5],
        split_state=row[6],
        left=left,
        right=right,
    )

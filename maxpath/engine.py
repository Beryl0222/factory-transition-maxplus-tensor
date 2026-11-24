"""批量 Max-Plus 矩阵合成与可追查区间查询引擎。

并列裁决的正确性（与合并树形状无关）
====================================

定义：对覆盖同一段集合的合法路径 P、Q，``P < Q`` 当且仅当两边的完整源边 ID
序列按 ASCII 字典序 P 更小。每个单元格携带一个 *嵌套字典序键* ``lex_key``：

* 叶子单元格：``lex_key = [edge_id]``；
* 内部单元格（L ⊗ R 在交界 k 处拼接）：``lex_key = [lex_key_L, lex_key_R]``。

嵌套 list 的逐项字典序与展开后的完整边 ID 序列字典序严格一致（逐项比较、
短序列前缀优先），且键的嵌套深度只等于树高，不物化完整路径。候选比较因此
是两级规则：

1. 得分严格取大（max-plus 主体，负数权重合法参与）；
2. 得分相同时比较 ``lex_key``——等价于完整边 ID 序列的 ASCII 字典序。

内容寻址 + 嵌套键使裁决只依赖段内容：任意两种合并树得到的最优路径序列
一致（max-plus 结合律），同分序列的字典序关系也一致，故结果不随并行合并
树形状或分组大小变化。节点内 ``rank`` 仅作为解释层的人类可读序号，按
``(入口字典位置, lex_key, 出口字典位置)`` 分配。

缺席与负数
==========
空缺坐标在存储中不存在单元格（不使用哨兵），合法负权重照常参与取大运算。

内存
====
节点单元格只保存 O(树高) 的嵌套键与 O(1) 指针；单次合成的暂存规模为
``|入口| × |交界|`` 量级的临时字典，路径在查询时沿指针链按需重建。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from .model import (
    MAX_ABS_SCORE,
    Edge,
    QueryResult,
    QuerySpec,
    SegmentSpec,
    SegmentView,
    ViewPlan,
    validate_closed_range,
)
from .store import (
    CellPointer,
    CellRecord,
    NodeRecord,
    NodeStore,
    internal_content_id,
    leaf_content_id,
)

CellKey = Tuple[str, str]


def _nested_key(parts: Sequence[Any]) -> list:
    """构造嵌套字典序键（叶子 [edge_id]；内部 [左键, 右键]）。"""
    return list(parts)


@dataclass(frozen=True)
class _Candidate:
    score: int
    lex_key: list
    split_state: str
    left: CellPointer
    right: CellPointer

    def dominates(self, other: "_Candidate") -> bool:
        """得分严格优先；同分时以嵌套字典序键决出。"""
        if self.score != other.score:
            return self.score > other.score
        return self.lex_key < other.lex_key


class MaxPlusEngine:
    """持有一个 :class:`NodeStore`，管理视图、合成节点与区间查询。"""

    def __init__(self, store: NodeStore) -> None:
        self.store = store
        self._views: Dict[str, ViewPlan] = {}
        # view_id -> 分层段树；levels[k][i] 是覆盖 [i, i+2^k) 的节点 ID，
        # 跨断裂或越界位置为 None。
        self._tree_levels: Dict[str, List[List[Optional[str]]]] = {}
        # (view_id, lo, hi, 相关排除边元组) -> 筛查影子节点 ID
        self._shadow_cache: Dict[Tuple[str, int, int, Tuple[str, ...]], str] = {}
        # view_id -> 槽位 -> SegmentSpec
        self._specs: Dict[str, List[SegmentSpec]] = {}

    # ------------------------------------------------------------------
    # 视图与基础段树
    # ------------------------------------------------------------------
    def build_view(
        self,
        view_id: str,
        order: Sequence[str],
        versions: Dict[str, SegmentSpec],
        description: str = "",
    ) -> ViewPlan:
        """登记区间一致的版本集合并构建基础（无筛查）二叉段树。

        相邻段输出/输入字典无交集时登记为断裂；断裂两侧子树仍正常构建，
        只有跨断裂的合成位置为 None，查询跨断裂时明确报告边界与片段。
        """
        if view_id in self._views:
            raise ValueError(f"视图 {view_id!r} 已存在")
        order = list(order)
        if len(set(order)) != len(order):
            raise ValueError("片段顺序中存在重复片段")
        missing = [name for name in order if name not in versions]
        if missing:
            raise KeyError(f"以下片段缺少选用版本: {missing}")

        specs = [versions[name] for name in order]
        segment_views = [
            SegmentView(position=pos, name=name, revision_id=spec.revision_id, spec=spec)
            for pos, (name, spec) in enumerate(zip(order, specs))
        ]

        breaks: List[Tuple[int, int, str, str, Tuple[str, ...], Tuple[str, ...]]] = []
        for pos in range(len(order) - 1):
            shared = set(specs[pos].output_states) & set(specs[pos + 1].input_states)
            if not shared:
                breaks.append(
                    (
                        pos,
                        pos + 1,
                        order[pos],
                        order[pos + 1],
                        specs[pos].output_states,
                        specs[pos + 1].input_states,
                    )
                )

        plan = ViewPlan(
            view_id=view_id,
            order=tuple(order),
            segments=tuple(segment_views),
            boundary_breaks=tuple(breaks),
            description=description,
        )
        self._views[view_id] = plan
        self._specs[view_id] = specs

        leaves = [
            self._build_leaf(view_id, pos, spec, frozenset())
            for pos, spec in enumerate(specs)
        ]
        self._tree_levels[view_id] = self._build_segment_tree(leaves, {b[0] for b in breaks})
        self.store.commit()
        return plan

    def _build_segment_tree(
        self, leaves: List[str], break_lefts: set[int]
    ) -> List[List[Optional[str]]]:
        """构建分层段树；levels[k][i] 覆盖 [i, i+2^k)，i 与 2^k 对齐。"""
        n = len(leaves)
        # levels[k][i] 覆盖绝对位置 [i, i+2^k)；未对齐或越界位置为 None。
        levels: List[List[Optional[str]]] = [list(leaves)]
        span = 1
        while span < n:
            prev = levels[-1]
            current: List[Optional[str]] = [None] * n
            # 仅在 i 与 2*span 对齐、且两个 span 子块都落在 n 内时建节点；
            # 不足整块的尾部留待区间查询时以更小粒度折叠。
            for i in range(0, n - 2 * span + 1, span * 2):
                left_id, right_id = prev[i], prev[i + span]
                block_breaks = any(j in break_lefts for j in range(i, i + span))
                if left_id is None or right_id is None or block_breaks:
                    current[i] = None
                else:
                    current[i] = self._compose_cached(
                        i, i + 2 * span - 1, left_id, right_id
                    )
            levels.append(current)
            span *= 2
        return levels

    def get_view(self, view_id: str) -> ViewPlan:
        try:
            return self._views[view_id]
        except KeyError as exc:
            raise KeyError(f"未知视图 {view_id!r}") from exc

    # ------------------------------------------------------------------
    # 节点构建
    # ------------------------------------------------------------------
    def _build_leaf(
        self, view_id: str, position: int, spec: SegmentSpec, excluded: FrozenSet[str]
    ) -> str:
        kept = sorted(
            (e for e in spec.edges if e.edge_id not in excluded),
            key=lambda e: e.edge_id,
        )
        node_id = leaf_content_id(
            position,
            spec.revision_id,
            spec.input_states,
            spec.output_states,
            ((e.edge_id, e.entry, e.exit_, e.weight) for e in kept),
        )
        if self.store.has_node(node_id):
            return node_id

        # 同一 (入口, 出口) 的多条边：最大权重优先，同分取最小 edge_id。
        best: Dict[CellKey, Edge] = {}
        for edge in kept:
            key = (edge.entry, edge.exit_)
            current = best.get(key)
            if (
                current is None
                or edge.weight > current.weight
                or (edge.weight == current.weight and edge.edge_id < current.edge_id)
            ):
                best[key] = edge

        in_index = {s: i for i, s in enumerate(spec.input_states)}
        out_index = {s: i for i, s in enumerate(spec.output_states)}
        ordered = sorted(
            best.values(),
            key=lambda e: (in_index[e.entry], e.edge_id, out_index[e.exit_]),
        )
        cells = [
            CellRecord(
                in_state=edge.entry,
                out_state=edge.exit_,
                score=edge.weight,
                rank=rank,
                lex_key=_nested_key([edge.edge_id]),
                edge_id=edge.edge_id,
            )
            for rank, edge in enumerate(ordered)
        ]
        record = NodeRecord(
            node_id=node_id,
            kind="leaf",
            lo=position,
            hi=position,
            input_states=spec.input_states,
            output_states=spec.output_states,
            segment_name=spec.name,
            revision_id=spec.revision_id,
            excluded=tuple(sorted(excluded)),
        )
        self.store.put_node(record, cells)
        return node_id

    def _compose_cached(
        self, lo: int, hi: int, left_id: str, right_id: str
    ) -> Optional[str]:
        """两个相邻节点的 max-plus 合成；交界字典无交集时返回 None。"""
        left_node = self.store.get_node(left_id)
        right_node = self.store.get_node(right_id)
        assert left_node is not None and right_node is not None
        right_input_set = set(right_node.input_states)
        junction = tuple(s for s in left_node.output_states if s in right_input_set)
        if not junction:
            return None
        node_id = internal_content_id(lo, hi, left_id, right_id, junction)
        if self.store.has_node(node_id):
            return node_id

        left_cells = {
            (c.in_state, c.out_state): c for c in self.store.iter_cells(left_id)
        }
        right_cells = {
            (c.in_state, c.out_state): c for c in self.store.iter_cells(right_id)
        }
        # 按入口/出口预索引，合成复杂度为 |入口| × |交界| × 扇出，不做全笛卡尔积。
        left_by_in: Dict[str, List[CellRecord]] = {
            s: [] for s in left_node.input_states
        }
        for cell in left_cells.values():
            if cell.out_state in junction:
                left_by_in[cell.in_state].append(cell)
        right_by_in: Dict[str, List[CellRecord]] = {s: [] for s in junction}
        for cell in right_cells.values():
            if cell.in_state in junction:
                right_by_in[cell.in_state].append(cell)

        winners: Dict[CellKey, _Candidate] = {}
        for in_state in left_node.input_states:
            for lc in left_by_in[in_state]:
                for rc in right_by_in[lc.out_state]:
                    score = lc.score + rc.score
                    if abs(score) > MAX_ABS_SCORE:  # 防御性校验
                        raise OverflowError(
                            f"合成得分 {score} 超出交接范围 |s| <= {MAX_ABS_SCORE}"
                        )
                    cand = _Candidate(
                        score=score,
                        lex_key=_nested_key([lc.lex_key, rc.lex_key]),
                        split_state=lc.out_state,
                        left=(left_id, lc.in_state, lc.out_state),
                        right=(right_id, rc.in_state, rc.out_state),
                    )
                    key = (in_state, rc.out_state)
                    old = winners.get(key)
                    if old is None or cand.dominates(old):
                        winners[key] = cand

        in_index = {s: i for i, s in enumerate(left_node.input_states)}
        out_index = {s: i for i, s in enumerate(right_node.output_states)}
        ordered_keys = sorted(
            winners,
            key=lambda key: (
                in_index[key[0]],
                winners[key].lex_key,
                out_index[key[1]],
            ),
        )
        cells = [
            CellRecord(
                in_state=key[0],
                out_state=key[1],
                score=winners[key].score,
                rank=rank,
                lex_key=winners[key].lex_key,
                split_state=winners[key].split_state,
                left=winners[key].left,
                right=winners[key].right,
            )
            for rank, key in enumerate(ordered_keys)
        ]
        record = NodeRecord(
            node_id=node_id,
            kind="internal",
            lo=lo,
            hi=hi,
            input_states=left_node.input_states,
            output_states=right_node.output_states,
            split=left_node.hi,
            left_id=left_id,
            right_id=right_id,
            junction=junction,
        )
        self.store.put_node(record, cells)
        return node_id

    # ------------------------------------------------------------------
    # 区间分解与筛查影子节点
    # ------------------------------------------------------------------
    def _canonical_nodes(
        self, view_id: str, lo: int, hi: int, excluded: FrozenSet[str]
    ) -> List[str]:
        """把闭合区间 [lo, hi] 分解为左到右排列的对齐二幂块节点。"""
        levels = self._tree_levels[view_id]
        specs = self._specs[view_id]
        nodes: List[str] = []
        cur = lo
        while cur <= hi:
            max_span = hi - cur + 1
            level_idx = min(
                (len(levels) - 1),
                max_span.bit_length() - 1,
            )
            # 贪心取最大可行块；遇到断裂块则降级到更小粒度。
            while level_idx >= 0:
                span = 1 << level_idx
                block_hi = cur + span - 1
                if block_hi > hi:
                    level_idx -= 1
                    continue
                node_id = self._shadow_node(
                    view_id, cur, block_hi, level_idx, excluded, specs
                )
                if node_id is not None:
                    nodes.append(node_id)
                    cur = block_hi + 1
                    break
                level_idx -= 1
            else:  # pragma: no cover - 叶子无断裂，理论上不可达
                raise RuntimeError(f"区间 [{lo}, {hi}] 分解失败，可能存在未报告断裂")
        return nodes

    def _shadow_node(
        self,
        view_id: str,
        lo: int,
        hi: int,
        level_idx: int,
        excluded: FrozenSet[str],
        specs: List[SegmentSpec],
    ) -> Optional[str]:
        """带排除边的块节点；只有跨度内确实命中排除边的块才重算。"""
        relevant = tuple(
            sorted(
                {
                    eid
                    for pos in range(lo, hi + 1)
                    for eid in excluded
                    if any(e.edge_id == eid for e in specs[pos].edges)
                }
            )
        )
        if not relevant:
            return self._tree_levels[view_id][level_idx][lo]

        cache_key = (view_id, lo, hi, relevant)
        cached = self._shadow_cache.get(cache_key)
        if cached is not None:
            return cached

        if level_idx == 0:
            node_id: Optional[str] = self._build_leaf(
                view_id, lo, specs[lo], frozenset(relevant)
            )
        else:
            half = 1 << (level_idx - 1)
            left_id = self._shadow_node(
                view_id, lo, lo + half - 1, level_idx - 1, frozenset(relevant), specs
            )
            right_id = self._shadow_node(
                view_id, lo + half, hi, level_idx - 1, frozenset(relevant), specs
            )
            node_id = (
                None
                if left_id is None or right_id is None
                else self._compose_cached(lo, hi, left_id, right_id)
            )
        if node_id is not None:
            self._shadow_cache[cache_key] = node_id
        return node_id

    def _fold_nodes(self, lo: int, node_ids: Sequence[str]) -> Optional[str]:
        """左到右顺序折叠规范节点（内容寻址，跨查询自动缓存去重）。"""
        cur_id = node_ids[0]
        for nxt_id in node_ids[1:]:
            nxt_node = self.store.get_node(nxt_id)
            assert nxt_node is not None
            composed = self._compose_cached(lo, nxt_node.hi, cur_id, nxt_id)
            if composed is None:
                return None
            cur_id = composed
        return cur_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(self, view_id: str, spec: QuerySpec) -> QueryResult:
        """执行区间查询：返回得分、边身份、不可达位置或断裂边界。"""
        plan = self.get_view(view_id)
        lo = plan.index_of(spec.start_segment)
        hi = plan.index_of(spec.end_segment)
        validate_closed_range(lo, hi, len(plan.order))
        excluded_tuple = tuple(sorted(spec.excluded))
        base_kwargs = dict(
            view_id=view_id,
            start_segment=spec.start_segment,
            end_segment=spec.end_segment,
            start_state=spec.start_state,
            end_state=spec.end_state,
            excluded=excluded_tuple,
        )

        # 1) 交界字典断裂：新视图无法跨边界合成，指出边界与涉及片段。
        breaks = plan.breaks_in(lo, hi)
        if breaks:
            return QueryResult(status="incompatible", breaks=breaks, **base_kwargs)

        # 2) 端点状态身份必须落在首尾片段字典内，否则属于合法不可达。
        first_spec = self._specs[view_id][lo]
        last_spec = self._specs[view_id][hi]
        if spec.start_state not in first_spec.input_states:
            return QueryResult(
                status="unreachable",
                unreachable_at=(spec.start_segment, spec.start_state),
                **base_kwargs,
            )
        if spec.end_state not in last_spec.output_states:
            return QueryResult(
                status="unreachable",
                unreachable_at=(spec.end_segment, spec.end_state),
                **base_kwargs,
            )

        # 3) 规范分解 + 顺序折叠；筛查只重算受影响的影子子树。
        node_ids = self._canonical_nodes(view_id, lo, hi, spec.excluded)
        root_id = self._fold_nodes(lo, node_ids)
        self.store.commit()
        assert root_id is not None  # 无断裂时合成必成功
        cell = self.store.get_cell(root_id, spec.start_state, spec.end_state)
        if cell is None:
            unreachable = self._locate_unreachable(
                view_id, node_ids, spec.start_state, spec.end_state
            )
            return QueryResult(
                status="unreachable", unreachable_at=unreachable, **base_kwargs
            )

        edge_ids = self.reconstruct_path(root_id, spec.start_state, spec.end_state)
        explanation = self.explain_choice(root_id, spec.start_state, spec.end_state)
        return QueryResult(
            status="ok",
            score=cell.score,
            edge_ids=tuple(edge_ids),
            tie_break=explanation,
            **base_kwargs,
        )

    def _locate_unreachable(
        self,
        view_id: str,
        node_ids: Sequence[str],
        start_state: str,
        end_state: str,
    ) -> Tuple[str, str]:
        """逐规范节点推进可达状态集合，定位第一个无法继续的交界。"""
        plan = self.get_view(view_id)
        reachable = {start_state}
        for node_id in node_ids:
            node = self.store.get_node(node_id)
            assert node is not None
            next_reachable = {
                cell.out_state
                for cell in self.store.iter_cells(node_id)
                if cell.in_state in reachable
            }
            if not next_reachable:
                return (plan.order[node.lo], next(iter(reachable)))
            reachable = next_reachable
        last_node = self.store.get_node(node_ids[-1])
        assert last_node is not None
        return (plan.order[last_node.hi], end_state)

    # ------------------------------------------------------------------
    # 路径重建与并列解释
    # ------------------------------------------------------------------
    def reconstruct_path(
        self, node_id: str, in_state: str, out_state: str
    ) -> List[str]:
        """沿单元格指针链按需重建完整源边 ID 序列（左到右）。"""
        work: List[CellPointer] = [(node_id, in_state, out_state)]
        edges: List[str] = []
        while work:
            cur_node, cur_in, cur_out = work.pop()
            cell = self.store.get_cell(cur_node, cur_in, cur_out)
            if cell is None:
                raise KeyError(f"单元格 ({cur_in}, {cur_out}) 在节点 {cur_node} 不存在")
            if cell.edge_id is not None:
                edges.append(cell.edge_id)
                continue
            assert cell.left is not None and cell.right is not None
            work.append(cell.right)  # 先压右后压左，左子树先展开
            work.append(cell.left)
        return edges

    def explain_choice(
        self, node_id: str, in_state: str, out_state: str
    ) -> str:
        """生成同分裁决理由。

        沿获胜单元格的指针链遍历 *全部* 内部节点（不能在无争议子树的叶子处
        提前返回），收集所有存在多条同分候选的分裂点，取第一处分歧边位置
        最早者——它决定了完整序列字典序的最终归属。若整条链没有任何争议
        分裂，则裁决仅来自叶子同坐标规则。
        """
        winner_seq = self.reconstruct_path(node_id, in_state, out_state)
        root_node = self.store.get_node(node_id)
        assert root_node is not None
        root_lo = root_node.lo
        contested: List[Tuple[int, List[str], dict, int]] = []

        stack: List[CellPointer] = [(node_id, in_state, out_state)]
        leaf_terminal: Optional[CellRecord] = None
        leaf_node_name: Optional[str] = None
        while stack:
            cur_node_id, cur_in, cur_out = stack.pop()
            node = self.store.get_node(cur_node_id)
            cell = self.store.get_cell(cur_node_id, cur_in, cur_out)
            assert node is not None and cell is not None
            if node.kind == "leaf":
                leaf_terminal, leaf_node_name = cell, node.segment_name
                continue
            tied = self._equal_score_candidates(cur_node_id, cur_in, cur_out)
            if len(tied) > 1:
                # 候选 runner 只覆盖本子节点跨度；换算为全局边序列后再定位分歧。
                local_runner = self._path_of_virtual(tied[1], cur_in, cur_out)
                offset = node.lo - root_lo
                global_runner = (
                    winner_seq[:offset]
                    + local_runner
                    + winner_seq[offset + len(local_runner):]
                )
                first_diff = next(
                    i
                    for i, (a, b) in enumerate(zip(winner_seq, global_runner))
                    if a != b
                )
                contested.append((first_diff, global_runner, tied[1], len(tied)))
            assert cell.left is not None and cell.right is not None
            # 先压右后压左，左子树优先展开，但两侧都会被访问。
            stack.append(cell.right)
            stack.append(cell.left)

        if not contested:
            assert leaf_terminal is not None and leaf_node_name is not None
            return (
                f"叶子片段 {leaf_node_name} 坐标 ({in_state}, {out_state}) "
                f"获胜边 {leaf_terminal.edge_id}（叶子同坐标规则：权重取大，"
                "同分时取 edge_id ASCII 字典序最小者；负权重为合法值，"
                "只有坐标缺席才表示不可达）"
            )

        first_diff, runner_seq, runner, candidate_count = min(
            contested, key=lambda item: item[0]
        )
        return (
            f"分裂状态 {runner['junction']} 处有 {candidate_count} 条同分候选 "
            f"(score={self.store.get_cell(node_id, in_state, out_state).score})；"
            "按全段边 ID 序列 ASCII 字典序，获胜序列在第 "
            f"{first_diff + 1} 条边以 {winner_seq[first_diff]!r} < "
            f"{runner_seq[first_diff]!r} 胜出（嵌套 lex_key 仅取决于段内容，"
            "与分组大小及合并树形状无关）"
        )

    def _equal_score_candidates(
        self, node_id: str, in_state: str, out_state: str
    ) -> List[dict]:
        """枚举某单元格在全部交界状态上的同分候选，按嵌套字典序键排序。"""
        node = self.store.get_node(node_id)
        assert node is not None and node.left_id and node.right_id
        winner = self.store.get_cell(node_id, in_state, out_state)
        assert winner is not None
        tied: List[dict] = []
        for junction_state in node.junction:
            lc = self.store.get_cell(node.left_id, in_state, junction_state)
            rc = self.store.get_cell(node.right_id, junction_state, out_state)
            if lc is None or rc is None:
                continue
            if lc.score + rc.score != winner.score:
                continue
            tied.append(
                {
                    "junction": junction_state,
                    "lex_key": _nested_key([lc.lex_key, rc.lex_key]),
                    "left": (node.left_id, lc.in_state, lc.out_state),
                    "right": (node.right_id, rc.in_state, rc.out_state),
                    "is_winner": junction_state == winner.split_state,
                }
            )
        tied.sort(key=lambda item: item["lex_key"])
        return tied

    def _path_of_virtual(
        self, candidate: dict, in_state: str, out_state: str
    ) -> List[str]:
        left = candidate["left"]
        right = candidate["right"]
        return (
            self.reconstruct_path(left[0], left[1], left[2])
            + self.reconstruct_path(right[0], right[1], right[2])
        )

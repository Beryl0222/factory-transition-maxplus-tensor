"""Max-Plus 合成引擎的核心行为测试。"""
from __future__ import annotations

import itertools
import random
import tracemalloc
import unittest
from pathlib import Path

from maxpath.case import build_versions, load_capture, query_from_capture
from maxpath.engine import MaxPlusEngine
from maxpath.model import (
    MAX_ABS_SCORE,
    Edge,
    QuerySpec,
    SegmentSpec,
)
from maxpath.store import MemoryStore, SqliteStore

DATA = Path(__file__).resolve().parents[1] / "data"


def make_engine(store=None) -> MaxPlusEngine:
    return MaxPlusEngine(store or MemoryStore())


def spec(name, inputs, outputs, edge_rows):
    return SegmentSpec.create(
        name=name, edges=edge_rows, input_states=inputs, output_states=outputs
    )


class CaptureContractTests(unittest.TestCase):
    def setUp(self):
        self.order, self.base, self.revs, self.queries = load_capture(
            DATA / "capture.json"
        )

    def test_base_queries_match_hand_analysis(self):
        eng = make_engine()
        eng.build_view("base", self.order, self.base)
        results = [eng.query("base", query_from_capture(q)) for q in self.queries]

        # 暴力枚举真值：最优得分 8，三条同分路径中字典序最小者胜。
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(results[0].score, 8)
        self.assertEqual(results[0].edge_ids, ("e1", "b9", "c1", "d1"))
        self.assertIn("字典序", results[0].tie_break)

        # 排除 e1、b1 后：获胜路径切换为 (e2,b2,c0,d2)，得分仍为 8。
        self.assertEqual(results[1].status, "ok")
        self.assertEqual(results[1].score, 8)
        self.assertEqual(results[1].edge_ids, ("e2", "b2", "c0", "d2"))
        for banned in ("e1", "b1"):
            self.assertNotIn(banned, results[1].edge_ids)

        # B-D、u0->d1：可达出口只有 d0，d1 合法不可达。
        self.assertEqual(results[2].status, "unreachable")
        self.assertEqual(results[2].unreachable_at, ("D", "d1"))

    def test_score_revision_changes_winner_but_keeps_snapshot(self):
        eng = make_engine()
        eng.build_view("base", self.order, self.base)
        before = eng.query("base", query_from_capture(self.queries[0]))

        versions_b2 = build_versions(self.base, ["B2"], self.revs)
        plan_b2 = eng.build_view("b2", self.order, versions_b2)
        self.assertEqual(plan_b2.boundary_breaks, ())
        after = eng.query("b2", query_from_capture(self.queries[0]))
        # B2 把 b9 权重从 1 降为 0：e1 路径降为 7，e2 路径反超（8）。
        self.assertEqual(after.edge_ids, ("e2", "b1", "c0", "d2"))
        self.assertEqual(after.score, 8)
        # 旧视图仍可查询，路径不被修订影响。
        self.assertEqual(
            eng.query("base", query_from_capture(self.queries[0])).edge_ids,
            before.edge_ids,
        )

    def test_dictionary_revision_marks_boundary_break(self):
        eng = make_engine()
        versions = build_versions(self.base, ["B2", "C2"], self.revs)
        plan = eng.build_view("broken", self.order, versions)
        self.assertEqual(len(plan.boundary_breaks), 1)
        brk = plan.boundary_breaks[0]
        self.assertEqual((brk[2], brk[3]), ("B", "C"))

        result = eng.query("broken", query_from_capture(self.queries[0]))
        self.assertEqual(result.status, "incompatible")
        self.assertEqual(result.breaks[0][2:4], ("B", "C"))

        # 断裂左侧的子区间仍可合法合成。
        left = eng.query(
            "broken", QuerySpec.create("A", "B", "a0", "v0")
        )
        self.assertEqual(left.status, "ok")
        self.assertEqual(left.edge_ids, ("e2", "b1"))

    def test_revision_recomputes_only_affected_subtrees(self):
        """只改 B 的得分：A/C/D 叶子与不覆盖 B 的 C-D 块必须原样复用。"""
        eng = make_engine()
        eng.build_view("base", self.order, self.base)
        base_leaves = list(eng._tree_levels["base"][0])
        base_cd_block = eng._tree_levels["base"][1][2]  # 覆盖位置 2..3

        versions_b2 = build_versions(self.base, ["B2"], self.revs)
        eng.build_view("b2", self.order, versions_b2)
        new_leaves = eng._tree_levels["b2"][0]
        new_cd_block = eng._tree_levels["b2"][1][2]

        # 位置 0/2/3（A/C/D）叶子内容寻址 ID 完全相同；只有 B（位置 1）变化。
        self.assertEqual(new_leaves[0], base_leaves[0])
        self.assertNotEqual(new_leaves[1], base_leaves[1])
        self.assertEqual(new_leaves[2], base_leaves[2])
        self.assertEqual(new_leaves[3], base_leaves[3])
        # 不覆盖 B 的合成块原样复用；覆盖 B 的块重新计算。
        self.assertEqual(new_cd_block, base_cd_block)
        self.assertNotEqual(
            eng._tree_levels["b2"][1][0], eng._tree_levels["base"][1][0]
        )

    def test_negative_weight_is_legal_not_absent(self):
        # e3 = -9 是 a1->u0 的唯一合法边，必须参与合成而非被当作缺席。
        eng = make_engine()
        eng.build_view("base", self.order, self.base)
        result = eng.query(
            "base", QuerySpec.create("A", "D", "a1", "d0")
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.edge_ids[0], "e3")
        self.assertEqual(result.score, -6)
        self.assertLess(result.score, 0)


class TieBreakTests(unittest.TestCase):
    def _chain_specs(self):
        # 两段：S0(x0,x1 -> y0,y1)，S1(y0,y1 -> z0)；构造多条完全同分路径。
        s0 = spec(
            "S0",
            ["x0"],
            ["y0", "y1"],
            [
                ("a1", "x0", "y0", 1),
                ("a2", "x0", "y1", 1),  # 同分，走 y1 的前缀边 id 更大
            ],
        )
        s1 = spec(
            "S1",
            ["y0", "y1"],
            ["z0"],
            [
                ("b1", "y0", "z0", 1),
                ("b2", "y1", "z0", 1),
            ],
        )
        return s0, s1

    def test_lexicographic_full_edge_sequence(self):
        s0, s1 = self._chain_specs()
        eng = make_engine()
        eng.build_view("v", ["S0", "S1"], {"S0": s0, "S1": s1})
        result = eng.query("v", QuerySpec.create("S0", "S1", "x0", "z0"))
        # (a1,b1) 与 (a2,b2) 同分；a1 < a2，故前者胜。
        self.assertEqual(result.edge_ids, ("a1", "b1"))
        self.assertIn("2 条同分候选", result.tie_break)

    def test_explanation_finds_deep_split_beyond_uncontested_prefix(self):
        # 前两条边 a、b 在两条同分路径上完全相同，分歧首次出现在第 3 条边。
        s0 = spec("S0", ["x"], ["y"], [("a", "x", "y", 1)])
        s1 = spec("S1", ["y"], ["z"], [("b", "y", "z", 1)])
        s2 = spec(
            "S2",
            ["z"],
            ["w0", "w1"],
            [("c1", "z", "w0", 1), ("c2", "z", "w1", 1)],
        )
        s3 = spec(
            "S3",
            ["w0", "w1"],
            ["o"],
            [("d1", "w0", "o", 1), ("d2", "w1", "o", 1)],
        )
        order = ["S0", "S1", "S2", "S3"]
        eng = make_engine()
        eng.build_view("v", order, dict(zip(order, [s0, s1, s2, s3])))
        result = eng.query("v", QuerySpec.create("S0", "S3", "x", "o"))
        self.assertEqual(result.edge_ids, ("a", "b", "c1", "d1"))
        # 解释必须越过无争议的 S0/S1 前缀，定位到第 3 条边的真实分裂。
        self.assertIn("第 3 条边", result.tie_break)
        self.assertIn("'c1' < 'c2'", result.tie_break)

    def test_tie_independent_of_grouping(self):
        # 四段链，多种分组方式（左结合、右结合、平衡树）必须给出同一路径。
        rng = random.Random(7)
        states = [[f"s{i}_0", f"s{i}_1"] for i in range(5)]
        segments = {}
        rows_by_segment = {}
        for i in range(4):
            rows = []
            for entry in states[i]:
                for exit_ in states[i + 1]:
                    # 权重只取 0..2，制造大量并列；边 ID 仅用字母数字。
                    token_entry = entry.replace("s", "S").replace("_", "")
                    token_exit = exit_.replace("s", "S").replace("_", "")
                    rows.append(
                        (f"e{i}{token_entry}{token_exit}", entry, exit_, rng.randint(0, 2))
                    )
            rows_by_segment[f"S{i}"] = rows
            segments[f"S{i}"] = spec(
                f"S{i}", states[i], states[i + 1], rows
            )
        order = [f"S{i}" for i in range(4)]

        engine_tree = make_engine()
        engine_tree.build_view("tree", order, segments)
        expected = engine_tree.query(
            "tree", QuerySpec.create("S0", "S3", states[0][0], states[4][0])
        )

        # 暴力枚举全部路径，独立实现最优 + 字典序裁决。
        all_paths = []
        for choices in itertools.product(*[rows_by_segment[n] for n in order]):
            ok = all(
                choices[i][2] == choices[i + 1][1] for i in range(len(choices) - 1)
            )
            if ok and choices[0][1] == states[0][0] and choices[-1][2] == states[4][0]:
                all_paths.append(
                    (sum(c[3] for c in choices), tuple(c[0] for c in choices))
                )
        best_score = max(p[0] for p in all_paths)
        brute_edges = min(p[1] for p in all_paths if p[0] == best_score)
        self.assertEqual(expected.edge_ids, brute_edges)
        self.assertEqual(expected.score, best_score)

        # 左结合 / 右结合的折叠顺序产生不同节点 ID（树形状不同），
        # 但根单元格的得分与边序列必须完全一致——内容寻址只复用等价子树，
        # 并列裁决由嵌套 lex_key 保证与分组方式无关。
        store = MemoryStore()
        eng2 = MaxPlusEngine(store)
        eng2.build_view("v2", order, segments)
        nodes = eng2._canonical_nodes("v2", 0, 3, frozenset())
        left_root = eng2._fold_nodes(0, nodes)
        leaf_ids = eng2._tree_levels["v2"][0]
        right_id = leaf_ids[3]
        for pos in range(2, -1, -1):
            right_id = eng2._compose_cached(pos, 3, leaf_ids[pos], right_id)
        self.assertNotEqual(left_root, right_id)
        left_cell = eng2.store.get_cell(left_root, states[0][0], states[4][0])
        right_cell = eng2.store.get_cell(right_id, states[0][0], states[4][0])
        self.assertEqual(left_cell.score, right_cell.score)
        self.assertEqual(
            tuple(eng2.reconstruct_path(left_root, states[0][0], states[4][0])),
            tuple(eng2.reconstruct_path(right_id, states[0][0], states[4][0])),
        )
        self.assertEqual(
            tuple(eng2.reconstruct_path(left_root, states[0][0], states[4][0])),
            brute_edges,
        )

        # 嵌套 lex_key 按前序展开必须等于该单元格的真实边 ID 序列。
        def flatten(key):
            out = []
            for part in key:
                out.extend(flatten(part) if isinstance(part, list) else [part])
            return out

        for node_id in (left_root, right_id):
            for cell in eng2.store.iter_cells(node_id):
                self.assertEqual(
                    flatten(cell.lex_key),
                    eng2.reconstruct_path(node_id, cell.in_state, cell.out_state),
                )
        # 同节点内 rank 顺序与 lex_key 顺序一致。
        cells = eng2.store.iter_cells(left_root)
        keys = [c.lex_key for c in sorted(cells, key=lambda c: c.rank)]
        self.assertEqual(keys, sorted(keys))


class ExclusionTests(unittest.TestCase):
    def test_excluded_edges_recompute_only_shadow_subtrees(self):
        order, base, revs, queries = load_capture(DATA / "capture.json")
        eng = make_engine()
        eng.build_view("base", order, base)
        base_leaves = list(eng._tree_levels["base"][0])
        base_node_count = len(eng.store.iter_node_ids())

        excluded = frozenset({"b1"})
        nodes = eng._canonical_nodes("base", 0, 3, excluded)
        # 筛查只重算命中排除边的影子子树：A 叶子（位置 0）不含 b1，
        # 其内容寻址 ID 必须与基础叶子完全相同。
        shadow_a = eng._build_leaf("base", 0, eng._specs["base"][0], frozenset({"b1"}))
        self.assertEqual(shadow_a, base_leaves[0])
        self.assertGreater(len(eng.store.iter_node_ids()), base_node_count)
        # 分解结果均为已存在节点，未凭空产生断裂。
        self.assertTrue(all(eng.store.has_node(n) for n in nodes))

        result = eng.query(
            "base", QuerySpec.create("A", "D", "a0", "d0", excluded=excluded)
        )
        self.assertNotIn("b1", result.edge_ids)
        # e1 路径不经过 b1，仍以字典序最小保持获胜。
        self.assertEqual(result.edge_ids, ("e1", "b9", "c1", "d1"))

        # 同时排除 e1 与 b1 后才强制切换到次优的 e2/b2 路径。
        forced = eng.query(
            "base",
            QuerySpec.create(
                "A", "D", "a0", "d0", excluded=frozenset({"e1", "b1"})
            ),
        )
        self.assertEqual(forced.edge_ids, ("e2", "b2", "c0", "d2"))
        self.assertEqual(forced.score, 8)

    def test_exclusion_can_make_unreachable(self):
        order, base, _, _ = load_capture(DATA / "capture.json")
        eng = make_engine()
        eng.build_view("base", order, base)
        result = eng.query(
            "base",
            QuerySpec.create("A", "B", "a1", "v1", excluded=frozenset({"b9"})),
        )
        self.assertEqual(result.status, "unreachable")

    def test_unknown_excluded_edge_is_validated_as_query_filter(self):
        order, base, _, _ = load_capture(DATA / "capture.json")
        eng = make_engine()
        eng.build_view("base", order, base)
        # 排除一个不存在的边 ID 不影响任何结果（筛查按实际候选算分）。
        result = eng.query(
            "base",
            QuerySpec.create("A", "D", "a0", "d0", excluded=frozenset({"zzz"})),
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.edge_ids, ("e1", "b9", "c1", "d1"))


class StorageBackedTests(unittest.TestCase):
    def test_sqlite_backend_matches_memory_and_persists(self):
        import tempfile

        order, base, revs, queries = load_capture(DATA / "capture.json")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nodes.sqlite"
            store = SqliteStore(str(db_path))
            eng = MaxPlusEngine(store)
            eng.build_view("base", order, base)
            expected = eng.query("base", query_from_capture(queries[0]))
            store.close()

            store2 = SqliteStore(str(db_path))
            eng2 = MaxPlusEngine(store2)
            eng2.build_view("base", order, base)  # 内容寻址命中既有节点
            reopened = eng2.query("base", query_from_capture(queries[0]))
            self.assertEqual(reopened.edge_ids, expected.edge_ids)
            self.assertEqual(reopened.score, expected.score)
            store2.close()


class RangeValidationTests(unittest.TestCase):
    def test_reversed_and_oversized_ranges_rejected(self):
        order, base, _, _ = load_capture(DATA / "capture.json")
        eng = make_engine()
        eng.build_view("base", order, base)
        with self.assertRaises(ValueError):
            eng.query("base", QuerySpec.create("D", "A", "a0", "d0"))
        with self.assertRaises(KeyError):
            eng.query("base", QuerySpec.create("A", "Z", "a0", "d0"))

    def test_weight_and_score_bounds_enforced(self):
        with self.assertRaises(ValueError):
            Edge("e", "x", "y", 10**12 + 1)
        # 边界值合法。
        edge = Edge("e", "x", "y", -(10**12))
        self.assertEqual(edge.weight, -(10**12))
        self.assertEqual(MAX_ABS_SCORE, 10**16)


class ScratchMemoryTests(unittest.TestCase):
    def test_composition_scratch_within_capacity_budget(self):
        # 按 capacity.json 规模（80 状态、稀疏 16 扇出）构建一次内部合成，
        # 断言 Python 侧峰值增量远小于 8 MiB 暂存预算（路径不物化）。
        capacity_path = DATA / "capacity.json"
        import json

        capacity = json.loads(capacity_path.read_text(encoding="utf-8"))
        n_states = capacity["state_count"]
        fanout = capacity["max_sources_per_state"]
        rng = random.Random(capacity["seed"])

        states_l = [f"l{i}" for i in range(n_states)]
        states_m = [f"m{i}" for i in range(n_states)]
        states_r = [f"r{i}" for i in range(n_states)]

        def rows(prefix, ins, outs):
            out = []
            for i, entry in enumerate(ins):
                for j in range(fanout):
                    exit_ = outs[(i + j) % len(outs)]
                    out.append(
                        (f"{prefix}{i:02d}{j:02d}", entry, exit_, rng.randint(-100, 100))
                    )
            return out

        left = spec("L", states_l, states_m, rows("le", states_l, states_m))
        right = spec("R", states_m, states_r, rows("re", states_m, states_r))
        eng = make_engine()
        left_id = eng._build_leaf("v", 0, left, frozenset())
        right_id = eng._build_leaf("v", 1, right, frozenset())

        tracemalloc.start()
        snap_before = tracemalloc.take_snapshot()
        composed = eng._compose_cached(0, 1, left_id, right_id)
        snap_after = tracemalloc.take_snapshot()
        stats = snap_after.compare_to(snap_before, "filename")
        peak_delta = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
        tracemalloc.stop()

        self.assertIsNotNone(composed)
        self.assertLess(peak_delta, capacity["peak_scratch_bytes"])
        # 路径重建结果长度为 2，而非每对端点一份完整序列。
        path = eng.reconstruct_path(composed, states_l[0], states_r[0])
        self.assertEqual(len(path), 2)


if __name__ == "__main__":
    unittest.main()

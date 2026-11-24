"""随机化不变量测试：引擎结果与独立暴力枚举在大量场景下逐案对齐。"""
from __future__ import annotations

import itertools
import random
import unittest

from maxpath.engine import MaxPlusEngine
from maxpath.model import QuerySpec, SegmentSpec
from maxpath.store import MemoryStore, SqliteStore


def brute_force(specs, order, lo, hi, start, end, excluded):
    pools = [
        [e for e in specs[order[i]].edges if e.edge_id not in excluded]
        for i in range(lo, hi + 1)
    ]
    candidates = []
    for combo in itertools.product(*pools):
        if combo[0].entry != start or combo[-1].exit_ != end:
            continue
        if any(combo[i].exit_ != combo[i + 1].entry for i in range(len(combo) - 1)):
            continue
        candidates.append(
            (sum(e.weight for e in combo), tuple(e.edge_id for e in combo))
        )
    if not candidates:
        return None
    best = max(score for score, _ in candidates)
    return best, min(path for score, path in candidates if score == best)


class RandomizedInvariantTests(unittest.TestCase):
    def test_random_chains_against_brute_force(self):
        rng = random.Random(20261124)
        for trial in range(40):
            n_segments = rng.randint(2, 7)
            width = rng.randint(1, 4)
            shared_pool = [f"q{i}" for i in range(width + 1)]
            order = [f"S{i}" for i in range(n_segments)]
            specs = {}
            all_edge_ids = []
            for i in range(n_segments):
                ins = shared_pool[: rng.randint(1, width + 1)]
                outs = shared_pool[: rng.randint(1, width + 1)]
                rows = []
                for entry in ins:
                    for exit_ in outs:
                        if rng.random() < 0.55:  # 制造空缺坐标
                            eid = f"t{trial:02d}s{i}e{len(rows):02d}"
                            rows.append(
                                (eid, entry, exit_, rng.randint(-50, 50))
                            )
                            all_edge_ids.append(eid)
                if not rows:  # 保证每段至少一条边
                    eid = f"t{trial:02d}s{i}e0"
                    rows.append((eid, ins[0], outs[0], rng.randint(-50, 50)))
                    all_edge_ids.append(eid)
                specs[order[i]] = SegmentSpec.create(
                    order[i], rows, ins, outs
                )

            for store in (MemoryStore(),):
                eng = MaxPlusEngine(store)
                plan = eng.build_view(f"v{trial}", order, specs)

                lo = rng.randint(0, n_segments - 1)
                hi = rng.randint(lo, n_segments - 1)
                start = specs[order[lo]].input_states[0]
                end = specs[order[hi]].output_states[-1]
                excluded = frozenset(
                    rng.sample(all_edge_ids, k=min(2, len(all_edge_ids)))
                )

                breaks = plan.breaks_in(lo, hi)
                result = eng.query(
                    f"v{trial}",
                    QuerySpec.create(
                        order[lo], order[hi], start, end, excluded=excluded
                    ),
                )
                if breaks:
                    self.assertEqual(result.status, "incompatible")
                    continue

                truth = brute_force(
                    specs, order, lo, hi, start, end, excluded
                )
                if truth is None:
                    self.assertEqual(result.status, "unreachable")
                else:
                    self.assertEqual(result.status, "ok")
                    self.assertEqual(result.score, truth[0])
                    self.assertEqual(tuple(result.edge_ids), truth[1])
                    # 重建路径必须逐边可追查到源片段。
                    rebuilt = eng.reconstruct_path
                    root = eng._fold_nodes(
                        lo,
                        eng._canonical_nodes(
                            f"v{trial}", lo, hi, excluded
                        ),
                    )
                    self.assertEqual(
                        tuple(rebuilt(root, start, end)), truth[1]
                    )

    def test_sqlite_and_memory_backends_agree(self):
        rng = random.Random(99)
        order = [f"S{i}" for i in range(5)]
        specs = {}
        for i in range(5):
            rows = [
                (f"e{i}{j}", f"x{j % 3}", f"y{(j + i) % 3}", rng.randint(-10, 10))
                for j in range(6)
            ]
            specs[order[i]] = SegmentSpec.create(
                order[i], rows, [f"x{k}" for k in range(3)], [f"y{k}" for k in range(3)]
            )
        mem = MaxPlusEngine(MemoryStore())
        mem.build_view("v", order, specs)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            sql = MaxPlusEngine(
                SqliteStore(str(Path(tmp) / "x.sqlite"))
            )
            sql.build_view("v", order, specs)
            q = QuerySpec.create("S0", "S4", "x0", "y0")
            a = mem.query("v", q)
            b = sql.query("v", q)
            self.assertEqual(a.status, b.status)
            self.assertEqual(a.score, b.score)
            self.assertEqual(a.edge_ids, b.edge_ids)
            sql.store.close()


if __name__ == "__main__":
    unittest.main()

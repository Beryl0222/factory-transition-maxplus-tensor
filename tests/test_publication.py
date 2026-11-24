"""出版快照、保留期重放与增量回收的测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from maxpath.case import build_versions, load_capture, query_from_capture
from maxpath.engine import MaxPlusEngine
from maxpath.model import QuerySpec
from maxpath.publication import PublicationBook
from maxpath.store import MemoryStore, SqliteStore

DATA = Path(__file__).resolve().parents[1] / "data"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.order, self.base, self.revs, self.queries = load_capture(
            DATA / "capture.json"
        )
        self.query = query_from_capture(self.queries[0])

    def test_replay_returns_published_path_after_revision(self):
        eng = MaxPlusEngine(MemoryStore())
        eng.build_view("base", self.order, self.base)
        book = PublicationBook(eng, retain=2)
        record = book.publish("base", description="初版")

        replayed = book.replay(record.token, self.query)
        self.assertEqual(replayed.edge_ids, ("e1", "b9", "c1", "d1"))

        # 提交 B2 修订并出版新快照；旧凭据仍指向旧版本集合。
        versions_b2 = build_versions(self.base, ["B2"], self.revs)
        eng.build_view("b2", self.order, versions_b2)
        second = book.publish("b2", description="B2 修订")
        self.assertEqual(
            book.replay(record.token, self.query).edge_ids,
            ("e1", "b9", "c1", "d1"),
        )
        self.assertEqual(
            book.replay(second.token, self.query).edge_ids,
            ("e2", "b1", "c0", "d2"),
        )
        self.assertEqual(len(book.list_live()), 2)

    def test_retention_rotates_and_revokes_oldest(self):
        eng = MaxPlusEngine(MemoryStore())
        book = PublicationBook(eng, retain=2)
        tokens = []
        for idx, rev_ids in enumerate([[], ["B2"], ["B2", "C2"]]):
            versions = build_versions(self.base, rev_ids, self.revs)
            view_id = f"v{idx}"
            eng.build_view(view_id, self.order, versions)
            tokens.append(book.publish(view_id).token)

        self.assertEqual(len(book.list_live()), 2)
        with self.assertRaises(PermissionError):
            book.replay(tokens[0], self.query)
        # 最近两个快照仍可重放（含断裂视图的子区间查询）。
        ok = book.replay(
            tokens[2], QuerySpec.create("A", "B", "a0", "v0")
        )
        self.assertEqual(ok.status, "ok")

    def test_garbage_collection_keeps_shared_nodes(self):
        eng = MaxPlusEngine(MemoryStore())
        eng.build_view("base", self.order, self.base)
        book = PublicationBook(eng, retain=1)
        first = book.publish("base")
        before = set(eng.store.iter_node_ids())

        versions_b2 = build_versions(self.base, ["B2"], self.revs)
        eng.build_view("b2", self.order, versions_b2)
        book.publish("b2")  # 触发轮转 + GC

        after = set(eng.store.iter_node_ids())
        # 未受 B2 影响的 A/C/D 叶子内容寻址 ID 必须仍然存在、可被复用。
        shared = {
            node_id
            for node_id in before
            if (eng.store.get_node(node_id) or None) is not None
            and (eng.store.get_node(node_id).segment_name in {"A", "C", "D"})
        }
        self.assertTrue(shared.issubset(after))
        with self.assertRaises(PermissionError):
            book.replay(first.token, self.query)

    def test_persisted_state_round_trip_with_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "nodes.sqlite"
            state = Path(tmp) / "pubs.json"
            store = SqliteStore(str(db))
            eng = MaxPlusEngine(store)
            eng.build_view("base", self.order, self.base)
            book = PublicationBook(eng, state_path=str(state))
            record = book.publish("base")
            store.close()

            store2 = SqliteStore(str(db))
            eng2 = MaxPlusEngine(store2)
            eng2.build_view("base", self.order, self.base)
            book2 = PublicationBook(eng2, state_path=str(state))
            replayed = book2.replay(record.token, self.query)
            self.assertEqual(replayed.edge_ids, ("e1", "b9", "c1", "d1"))
            store2.close()


if __name__ == "__main__":
    unittest.main()

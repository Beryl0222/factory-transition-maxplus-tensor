"""命令行入口：构建视图、执行采集查询、出版与重放。

示例
----
python -m maxpath.cli run data/capture.json --store /tmp/maxpath.sqlite
python -m maxpath.cli run data/capture.json --revisions B2
python -m maxpath.cli run data/capture.json --revisions B2,C2 --publish
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

if __package__ in (None, ""):  # 直接以脚本方式、-I 隔离运行时的引导
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxpath.case import build_versions, load_capture, query_from_capture
from maxpath.engine import MaxPlusEngine
from maxpath.publication import PublicationBook
from maxpath.store import MemoryStore, SqliteStore


def _parse_revisions(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Max-Plus 区间合成与可追查查询")
    parser.add_argument("capture", type=Path, help="采集资料 capture.json 路径")
    parser.add_argument(
        "--revisions",
        default="",
        help="逗号分隔的修订 ID，按顺序套用（如 B2,C2）",
    )
    parser.add_argument(
        "--store",
        default=":memory:",
        help="SQLite 存储路径；默认 :memory: 进程内后端",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="出版凭据 JSON 边车路径（使用持久化存储时建议提供）",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="构建后出版快照并打印凭据",
    )
    args = parser.parse_args(argv)

    order, base_segments, revisions, raw_queries = load_capture(args.capture)
    versions = build_versions(base_segments, _parse_revisions(args.revisions), revisions)
    view_id = "view:" + (",".join(_parse_revisions(args.revisions)) or "base")

    store = SqliteStore(args.store) if args.store != ":memory:" else MemoryStore()
    engine = MaxPlusEngine(store)
    plan = engine.build_view(view_id, order, versions, description="cli 构建")

    print(f"视图 {view_id} 构建完成，共 {len(order)} 段")
    if plan.boundary_breaks:
        for brk in plan.boundary_breaks:
            print(
                f"  交界断裂: 位置 {brk[0]}|{brk[1]} 片段 {brk[2]}|{brk[3]} "
                f"输出 {list(brk[4])} 与输入 {list(brk[5])} 无共享身份"
            )

    results = [engine.query(view_id, query_from_capture(q)) for q in raw_queries]
    print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))

    if args.publish:
        book = PublicationBook(engine, state_path=args.state)
        record = book.publish(view_id, description="cli 出版")
        print(f"已出版快照 token={record.token}")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure the opt-in dirty-row SQLite commit against the full rewrite.

This benchmark is observational: it does not alter ``SQLiteTreeStore``.  It
builds identical ordered trees, then commits isolated leaf updates and reports
both wall time and SQLite's logical ``total_changes`` row-write count.  Run:

    uv run python benchmarks/bench_sqlite_incremental.py --nodes 2000 --updates 20
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

from grove import SQLiteTreeStore
from grove.sqlite_incremental_experiment import SQLiteIncrementalTreeStore


def _build(store, nodes: int) -> None:
    with store.transaction() as tx:
        for i in range(nodes):
            tx.create(f"node-{i}", node_id=f"node-{i}", properties={"index": i})


def _measure(store, nodes: int, updates: int) -> dict[str, object]:
    elapsed: list[float] = []
    writes: list[int] = []
    for i in range(updates):
        before = store._conn.total_changes
        started = time.perf_counter()
        store.update(f"node-{i % nodes}", properties={"index": i, "updated": True})
        elapsed.append((time.perf_counter() - started) * 1000)
        writes.append(store._conn.total_changes - before)
    result: dict[str, object] = {
        "commit_p50_ms": round(statistics.median(elapsed), 3),
        "commit_p95_ms": round(sorted(elapsed)[max(0, int(len(elapsed) * .95) - 1)], 3),
        "row_writes_p50": int(statistics.median(writes)),
        "row_writes_total": sum(writes),
        "row_writes": writes,
    }
    stats = getattr(store, "last_commit_stats", None)
    if stats is not None:
        result["last_dirty_stats"] = stats
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=2_000)
    parser.add_argument("--updates", type=int, default=20)
    args = parser.parse_args(argv)
    if args.nodes <= 0 or args.updates <= 0:
        parser.error("--nodes and --updates must be positive")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        full = SQLiteTreeStore(root / "full.db")
        dirty = SQLiteIncrementalTreeStore(root / "incremental.db")
        try:
            _build(full, args.nodes)
            _build(dirty, args.nodes)
            full_result = _measure(full, args.nodes, args.updates)
            dirty_result = _measure(dirty, args.nodes, args.updates)
        finally:
            full.close()
            dirty.close()
    full_p50 = float(full_result["commit_p50_ms"])
    dirty_p50 = float(dirty_result["commit_p50_ms"])
    full_rows = int(full_result["row_writes_p50"])
    dirty_rows = int(dirty_result["row_writes_p50"])
    result = {
        "format_version": 1,
        "nodes": args.nodes,
        "updates": args.updates,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "full_rewrite": full_result,
        "incremental_dirty_rows": dirty_result,
        "median_commit_speedup": round(full_p50 / dirty_p50, 3) if dirty_p50 else None,
        "median_row_write_reduction": round(full_rows / dirty_rows, 3) if dirty_rows else None,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

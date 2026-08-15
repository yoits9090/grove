"""Compare current materialized queries with durable SQLite scalar lookup.

This is an observational experiment.  It is intentionally outside GROVE's
public package API and reports both correctness and wall-clock medians.  The
indexed path still materializes a coherent tree snapshot for final Query
semantics; the sidecar B-tree only narrows candidate IDs.
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
from grove.sqlite_property_index_experiment import SQLiteScalarPropertyIndexExperiment


def _build(store, nodes: int, groups: int) -> None:
    with store.transaction() as tx:
        for i in range(nodes):
            tx.create(f"node-{i}", node_id=f"node-{i}", properties={"group": i % groups, "payload": {"i": i}})


def _median(samples: list[float]) -> float:
    return round(statistics.median(samples), 3)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=2_000)
    parser.add_argument("--groups", type=int, default=100)
    parser.add_argument("--reads", type=int, default=500)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(argv)
    if min(args.nodes, args.groups, args.reads, args.repeats) <= 0:
        parser.error("--nodes, --groups, --reads and --repeats must be positive")
    with tempfile.TemporaryDirectory() as directory:
        base_path = Path(directory) / "materialized.db"
        indexed_path = Path(directory) / "indexed.db"
        baseline = SQLiteTreeStore(base_path)
        indexed = SQLiteScalarPropertyIndexExperiment(indexed_path)
        try:
            _build(baseline, args.nodes, args.groups)
            _build(indexed, args.nodes, args.groups)
            indexed.create_scalar_index("group")
            key = args.groups // 2
            baseline_ids = baseline.query(predicate={"group": key}).ids()
            indexed_ids = indexed.scalar_ids("group", key)
            if baseline_ids != indexed_ids:
                raise RuntimeError("indexed lookup does not match materialized query")
            baseline_samples: list[float] = []
            indexed_samples: list[float] = []
            for repeat in range(args.repeats):
                started = time.perf_counter()
                for i in range(args.reads):
                    baseline.query(predicate={"group": (i + repeat) % args.groups}).ids()
                baseline_samples.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                for i in range(args.reads):
                    indexed.scalar_ids("group", (i + repeat) % args.groups)
                indexed_samples.append((time.perf_counter() - started) * 1000)
            result = {
                "format_version": 1,
                "nodes": args.nodes,
                "groups": args.groups,
                "reads": args.reads,
                "repeats": args.repeats,
                "candidate_count": len(indexed_ids),
                "baseline_materialized_query_median_ms": _median(baseline_samples),
                "durable_scalar_index_median_ms": _median(indexed_samples),
                "speedup_baseline_over_index": round(
                    statistics.median(baseline_samples) / statistics.median(indexed_samples), 3
                ),
                "baseline_samples_ms": [round(value, 3) for value in baseline_samples],
                "indexed_samples_ms": [round(value, 3) for value in indexed_samples],
                "python": platform.python_version(),
            }
        finally:
            baseline.close()
            indexed.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

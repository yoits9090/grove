"""Compare SQLiteTreeStore read refresh before/after revision caching.

The baseline is the pre-optimization refresh algorithm, retained here only as
an experiment control. It is never used by the package.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from grove import SQLiteTreeStore


def _baseline_refresh(store: SQLiteTreeStore) -> None:
    """Materialize and validate the complete SQLite state on every read."""
    with store._lock:
        state, revision = store._read_snapshot()
        store._state = state
        store._version = revision


def _measure(store: SQLiteTreeStore, ids: list[str], reads: int, repeats: int) -> list[float]:
    samples = []
    for repeat in range(repeats):
        started = time.perf_counter()
        for i in range(reads):
            store.get(ids[(i + repeat) % len(ids)])
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=1_000)
    parser.add_argument("--reads", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if min(args.nodes, args.reads, args.repeats) <= 0:
        parser.error("--nodes, --reads and --repeats must be positive")

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "reads.db"
        store = SQLiteTreeStore(path)
        with store.transaction() as tx:
            ids = [tx.create(f"node-{i}").id for i in range(args.nodes)]
        # Keep setup and cache warmup out of measured samples.
        store.get(ids[0])
        optimized = _measure(store, ids, args.reads, args.repeats)
        store._refresh = lambda: _baseline_refresh(store)
        baseline = _measure(store, ids, args.reads, args.repeats)
        result = {
            "nodes": args.nodes,
            "reads": args.reads,
            "repeats": args.repeats,
            "optimized_median_ms": round(statistics.median(optimized), 3),
            "baseline_median_ms": round(statistics.median(baseline), 3),
            "speedup": round(statistics.median(baseline) / statistics.median(optimized), 1),
            "optimized_samples_ms": [round(x, 3) for x in optimized],
            "baseline_samples_ms": [round(x, 3) for x in baseline],
        }
        store.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

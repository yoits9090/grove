"""Bounded, reproducible GROVE workload benchmarks.

The benchmark deliberately lives outside the package so that adding an
observational workload cannot change production behavior.  The default
``random`` family is the original vertical-slice smoke workload.  Additional
families exercise one shape at a time:

* ``deep``: one long chain (path/deep traversal and recursive export);
* ``broad``: many ordered children under one parent (sibling scans/order);
* ``skew``: an intentionally hot set of early parents (uneven fan-out);
* ``reopen``: one durable commit followed by a reopen for every node.

Workload generation is deterministic for a given family, node/read count, and
seed.  Timings and machine metadata are observational, of course, but the
output is always one JSON document with a stable schema and sorted keys.

Examples::

    uv run python benchmarks/bench.py --nodes 200 --reads 100 --seed 7
    uv run python benchmarks/bench.py --family deep --nodes 500 --reads 100
    uv run python benchmarks/bench.py --family reopen --nodes 64 --reads 100
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from grove import PersistentTreeStore, TreeStore


FAMILIES = ("random", "deep", "broad", "skew", "reopen")
# These limits are intentionally conservative.  A snapshot commit copies and
# serializes the complete tree, while deep export/import is recursive.
MAX_NODES = {
    "random": 5_000,
    "deep": 800,
    "broad": 5_000,
    "skew": 5_000,
    "reopen": 200,
}
DEFAULT_NODES = {
    # Preserve the old no-argument behavior for the original smoke workload.
    "random": 1_000,
    "deep": 500,
    "broad": 1_000,
    "skew": 1_000,
    # Reopen intentionally has a lower default: recovery parses every prior
    # snapshot, so a large count quickly becomes an unbounded-looking test.
    "reopen": 64,
}
MAX_READS = 20_000

SHAPE_CONFIG: dict[str, dict[str, Any]] = {
    "random": {
        "parent_mode": "root_or_existing",
        "existing_parent_probability": 0.25,
    },
    "deep": {"parent_mode": "previous_node"},
    "broad": {"parent_mode": "root"},
    "skew": {
        "parent_mode": "early_existing_or_root",
        "root_probability": 0.10,
        "early_parent_exponent": 4.0,
    },
    "reopen": {
        "parent_mode": "skew",
        "commit_mode": "one_create_per_commit",
        "reopen_mode": "after_every_commit",
    },
}


def _parent_for(
    family: str,
    ids: list[str],
    rng: random.Random,
    previous: str = "/",
) -> str:
    """Return the deterministic parent for the next generated node."""
    if family == "deep":
        return previous
    if family == "broad":
        return "/"
    if family == "random":
        return rng.choice(ids) if ids and rng.random() < 0.25 else "/"
    # Skew uses an inexpensive geometric choice rather than ``choices`` with
    # O(n) weights.  Small indexes are selected much more often, producing a
    # few hot early parents while retaining some root-level branches.
    if not ids or rng.random() < 0.10:
        return "/"
    index = int((rng.random() ** 4.0) * len(ids))
    return ids[min(index, len(ids) - 1)]


def _build_memory(
    family: str, nodes: int, rng: random.Random
) -> tuple[TreeStore, list[str], float]:
    """Build a non-reopen workload in one atomic transaction."""
    db = TreeStore()
    ids: list[str] = []
    previous = "/"
    started = time.perf_counter()
    with db.transaction() as tx:
        for i in range(nodes):
            parent = _parent_for(family, ids, rng, previous)
            node = tx.create(f"n{i}", parent=parent)
            ids.append(node.id)
            previous = node.id
    return db, ids, (time.perf_counter() - started) * 1000


def _build_reopen(
    nodes: int, rng: random.Random, directory: Path
) -> tuple[PersistentTreeStore, list[str], float, dict[str, Any]]:
    """Build a durable workload, reopening after each committed create."""
    path = directory / "grove-reopen.log"
    db = PersistentTreeStore(path)
    ids: list[str] = []
    previous = "/"
    commit_times: list[float] = []
    reopen_times: list[float] = []
    started = time.perf_counter()
    for i in range(nodes):
        parent = _parent_for("skew", ids, rng, previous)
        commit_started = time.perf_counter()
        node = db.create(f"n{i}", parent=parent)
        commit_times.append((time.perf_counter() - commit_started) * 1000)
        ids.append(node.id)
        previous = node.id

        db.close()
        reopen_started = time.perf_counter()
        db = PersistentTreeStore(path)
        reopen_times.append((time.perf_counter() - reopen_started) * 1000)

    return db, ids, (time.perf_counter() - started) * 1000, {
        "history_bytes": path.stat().st_size,
        "reopen_count": len(reopen_times),
        "reopen_commit_times_ms": commit_times,
        "reopen_times_ms": reopen_times,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Retain the original benchmark's nearest-lower p95 convention.
    index = max(0, min(len(ordered) - 1, int(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def _measure_import(db: TreeStore) -> tuple[float, int]:
    """Measure the common one-shot durable import baseline."""
    exported_tree = db.export("/")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "grove.log"
        durable = PersistentTreeStore(path)
        started = time.perf_counter()
        with durable.transaction() as tx:
            tx.import_tree(exported_tree, preserve_ids=False)
        elapsed = (time.perf_counter() - started) * 1000
        durable_bytes = path.stat().st_size
        durable.close()
    return elapsed, durable_bytes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--family", "--workload", "--workload-family",
        dest="family", choices=FAMILIES, default="random",
        help="workload shape (default: random; reopen defaults to 64 nodes)",
    )
    parser.add_argument(
        "--nodes", type=int, default=None,
        help="number of created nodes (family-specific bounded default)",
    )
    parser.add_argument("--reads", type=int, default=1_000)
    parser.add_argument("--seed", type=int, default=7)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    nodes = DEFAULT_NODES[args.family] if args.nodes is None else args.nodes
    if nodes < 0:
        parser.error("--nodes must be non-negative")
    if nodes > MAX_NODES[args.family]:
        parser.error(
            f"--nodes is capped at {MAX_NODES[args.family]} for {args.family} "
            "to keep snapshot/recursive work bounded"
        )
    if args.reads < 0:
        parser.error("--reads must be non-negative")
    if args.reads > MAX_READS:
        parser.error(f"--reads is capped at {MAX_READS}")

    rng = random.Random(args.seed)
    reopen_meta: dict[str, Any] = {
        "history_bytes": None,
        "reopen_count": 0,
        "reopen_commit_times_ms": [],
        "reopen_times_ms": [],
    }
    if args.family == "reopen":
        with tempfile.TemporaryDirectory() as tmp:
            db, ids, build_ms, reopen_meta = _build_reopen(
                nodes, rng, Path(tmp)
            )
            result = _measure(db, ids, args, rng, build_ms, reopen_meta)
    else:
        db, ids, build_ms = _build_memory(args.family, nodes, rng)
        result = _measure(db, ids, args, rng, build_ms, reopen_meta)
    print(json.dumps(result, sort_keys=True, indent=2))


def _measure(
    db: TreeStore,
    ids: list[str],
    args: argparse.Namespace,
    rng: random.Random,
    build_ms: float,
    reopen_meta: dict[str, Any],
) -> dict[str, Any]:
    """Collect common read/export/durable metrics and return JSON data."""
    read_times: list[float] = []
    # Keep reads deterministic but independent of hash/randomized iteration.
    for _ in range(args.reads):
        target = ids[rng.randrange(len(ids))] if ids else "/"
        started = time.perf_counter()
        db.get(target)
        read_times.append((time.perf_counter() - started) * 1e6)

    started = time.perf_counter()
    exported = db.export_json()
    export_ms = (time.perf_counter() - started) * 1000
    durable_ms, durable_bytes = _measure_import(db)

    result_nodes = args.nodes if args.nodes is not None else len(ids)
    result: dict[str, Any] = {
        "format_version": 2,
        "family": args.family,
        "workload_family": args.family,
        "seed": args.seed,
        "nodes": result_nodes,
        "actual_nodes": len(ids),
        "reads": args.reads,
        "machine": platform.platform(),
        "python": sys.version.split()[0],
        "build_ms": round(build_ms, 3),
        "export_ms": round(export_ms, 3),
        "export_bytes": len(exported.encode()),
        "read_p50_us": _percentile(read_times, 0.50),
        "read_p95_us": _percentile(read_times, 0.95),
        "durable_import_commit_ms": round(durable_ms, 3),
        "durable_bytes": durable_bytes,
        "history_bytes": reopen_meta["history_bytes"],
        "reopen_count": reopen_meta["reopen_count"],
        "reopen_commit_p50_ms": _percentile(
            reopen_meta["reopen_commit_times_ms"], 0.50
        ),
        "reopen_commit_p95_ms": _percentile(
            reopen_meta["reopen_commit_times_ms"], 0.95
        ),
        "reopen_p50_ms": _percentile(reopen_meta["reopen_times_ms"], 0.50),
        "reopen_p95_ms": _percentile(reopen_meta["reopen_times_ms"], 0.95),
        # The config/shape fields make it possible to compare artifacts
        # without reverse-engineering command-line defaults.
        "config": {
            "family": args.family,
            "nodes": result_nodes,
            "reads": args.reads,
            "seed": args.seed,
            "max_nodes": MAX_NODES[args.family],
        },
        "shape": SHAPE_CONFIG[args.family],
        "operation_counts": {
            "create": len(ids),
            "read": args.reads,
            "export": 1,
            "durable_import": 1,
            "reopen": reopen_meta["reopen_count"],
        },
    }
    assert result["config"]["nodes"] == result_nodes
    return result


if __name__ == "__main__":
    main()

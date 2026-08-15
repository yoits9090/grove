# Evaluation catalog

Each benchmark/eval should record commit, machine, Python version, seed, config,
raw output, and workload family.

## Initial correctness gates

- Invariants after every generated operation: exactly one root, bidirectional
  parent/child edges, ordered unique children, unique sibling names, no cycle,
  all nodes reachable, IDs stable after rename/move.
- Model-based sequences against a tiny dict/list reference model.
- JSON export/import round trips including Unicode names and tagged values.
- Transaction rollback and conflict tests: failed operations change neither
  state nor subscription output.
- Recovery matrix: clean frame, truncated header/payload/CRC, arbitrary tail,
  and corrupt complete frame. Complete checksum failure must fail closed.

Zero tolerance: data loss, invariant violation, unexplained flaky test, and
partial transaction visibility.

## Workload families

Deep chains, broad sibling sets, skewed trees, tiny nodes, large nested
properties, Unicode/path components, random move/rename/delete races, and
reopen-after-each-commit durability. Baselines use deterministic seeds.


## First-cycle evidence

`tests/` contains 25 deterministic tests plus randomized operation
coverage, including typed round trips, model-like path checks, invalid operation
atomicity, subscription ancestry, and initial-file corruption. Run:

```bash
uv run pytest -q
```

The benchmark driver is `benchmarks/bench.py`; it emits machine, interpreter,
seed, node count, read latency, export size, and durable commit measurements.
Do not compare results without matching workload/configuration.


### Smoke result (2026-08-15)

On macOS arm64 with Python 3.14.5, seed 7, 200 nodes and 100 reads: build
33.505 ms, export 0.262 ms (68,900 bytes), read p50 0.917 us / p95 1.000 us,
and durable root import commit 2.365 ms (66,938-byte log). This is a smoke
measurement, not a production target; the snapshot backend's O(tree-size)
commit cost dominates as datasets grow.


## Second-cycle evidence

The SQLite adapter now has differential tests across `TreeStore`,
`PersistentTreeStore`, and `SQLiteTreeStore`, plus deterministic subprocess
SIGKILL recovery tests at pre-commit and post-commit boundaries. The suite
contains 34 tests and passes repeatedly. Workload families in
`benchmarks/bench.py` now include `random`, `deep`, `broad`, `skew`, and
`reopen`, with bounded deterministic JSON output including configuration,
shape, operation counts, and reopen statistics.


## Third-cycle evidence

Snapshot query tests now cover recursive and direct-child traversal, typed
criteria (`bool` versus `int`), detached query results, immutable query snapshots,
and lightweight property indexes. Extended fuzz coverage exercises Unicode,
deep/broad trees, invalid paths, hostile JSON/tags, malformed SQLite rows, and
model invariants. The suite currently passes 72 tests.

A SQLite read-cache experiment uses `PRAGMA data_version` plus durable revision
validation to avoid rematerializing unchanged snapshots. At 300 nodes/50 reads,
the local comparison measured 54.422 ms baseline versus 0.232 ms cached median
(234.5x); this is a machine-specific observational result.


## Fourth-cycle evidence

A disposable SQLite scalar-property index experiment stores typed scalar keys
in an attached sidecar and rebuilds them atomically with tree commits. It
preserves exact GROVE semantics by finalizing through the normal materialized
query. At 200 nodes/50 lookups, the baseline materialized path measured 51.519
ms median versus 67.136 ms for the indexed experiment (0.767x), demonstrating
that an index without an accelerated snapshot/query executor is not yet useful.
The experiment remains isolated pending a workload-driven design.

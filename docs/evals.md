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

Snapshot query tests cover recursive and direct-child traversal, typed criteria
(`bool` versus `int`), detached query results, immutable query snapshots, and
lightweight property indexes. Extended fuzz coverage exercises Unicode,
deep/broad trees, invalid paths, hostile JSON/tags, malformed SQLite rows, and
model invariants. The current suite collects 85 tests and passes with
`uv run pytest -q`.

A SQLite read-cache experiment uses `PRAGMA data_version` plus durable revision
validation to avoid rematerializing unchanged snapshots. The checked-in
1,000-node/100-read control run measured 340.975 ms baseline versus 0.450 ms
cached median (757.3x); this is a machine-specific observational result. See
`docs/experiment-002-sqlite-read-cache.md` for the reproduction command and
limits.


## Fourth-cycle evidence

A disposable SQLite scalar-property index experiment stores typed scalar keys
in an attached sidecar and rebuilds them atomically with tree commits. It now
has an isolated ordered SQL-CTE direct path that decodes matching rows without
materializing the complete tree. In a 200-node/20-group/50-read/5-repeat run,
the materialized baseline measured 52.336 ms median versus 12.095 ms for the
direct indexed path (4.327x). Earlier full-materialization runs were slower than
baseline; neither result is generalized into the public API. Broader predicate,
mutation, crash, and workload validation remains required.


## Fifth-cycle evidence

The suite now includes optional schema validation, immutable content-addressed
snapshot tests, incremental Merkle tests, bounded SQLite writer lifecycle
coverage, online backup tests, and malformed external-schema tests. The full
suite passes under `uv run python -m pytest -q`.

The structural-sharing and Merkle work is intentionally isolated under
`experiments/`; neither experiment is a production performance claim.

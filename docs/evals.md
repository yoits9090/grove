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

`tests/test_store.py` contains 20 deterministic tests plus randomized operation
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

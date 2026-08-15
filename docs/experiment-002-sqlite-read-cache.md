# Experiment 002: SQLite read revision cache

**Question.** Does `SQLiteTreeStore` spend most of repeated read latency
materializing an unchanged database snapshot?

**Bottleneck.** Before this experiment, every `get`, `exists`, `path`, and
export first called `_refresh`, which selected and decoded every node and edge,
validated properties/timestamps, and checked all invariants. A cProfile run at
1,000 nodes showed `_read_state_from_connection` dominating the read call path;
500 `get` calls took approximately 1.6 seconds on this machine.

**Change.** `_refresh` now checks SQLite's connection-local `PRAGMA data_version`
and the singleton metadata revision/root (two small queries). If neither has
changed since the last coherent snapshot, it keeps the detached in-memory state.
A changed, missing, or malformed validator follows the old full snapshot path;
there is no behavior change on refresh or corruption handling. `data_version`
is retained so a second connection's commit invalidates the cache even before
revision validation. Transaction and commit paths update the validator after
loading or publishing state.

**Reproduction.** From the repository root:

```bash
uv run python benchmarks/bench_sqlite_reads.py --nodes 1000 --reads 100 --repeats 5
```

The benchmark compares the optimized path with an in-process copy of the prior
full-materialization refresh algorithm. It is a control, not production code.
On macOS arm64, Python 3.14.5, this run produced:

```json
{
  "baseline_median_ms": 360.745,
  "optimized_median_ms": 0.414,
  "speedup": 871.4,
  "nodes": 1000,
  "reads": 100,
  "repeats": 5
}
```

The absolute timings are machine-dependent; the large gap measures avoided
JSON decoding and invariant validation, not a semantic shortcut. The focused
test `test_sqlite_reads_skip_snapshot_materialization_when_revision_is_unchanged`
asserts no materialization for stable repeated reads, while
`test_sqlite_fast_read_path_still_refreshes_other_instance` covers visibility
of another handle's commit. Full suite result: `72 passed` (`uv run pytest -q`).

**Reversibility and limits.** The change is isolated to the SQLite adapter and
can be reverted as one `_refresh`/validator patch. Every operation still checks
metadata and `data_version`; concurrent or out-of-band changes take the
original snapshot-and-validation path. This is a read optimization only:
transactions still materialize detached snapshots and commits still rewrite
all relational rows.

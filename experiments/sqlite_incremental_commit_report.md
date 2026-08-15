# SQLite incremental commit experiment

## Scope

`grove.sqlite_incremental_experiment.SQLiteIncrementalTreeStore` is an opt-in,
separate subclass of `SQLiteTreeStore`. It does **not** replace or modify the
core commit path. The experiment computes a dirty-row diff while the existing
`BEGIN IMMEDIATE` transaction is open, then updates only changed `nodes` rows
and only changed ordered child lists in `children`. Metadata revision updates
remain the existing optimistic commit boundary.

The class exposes `last_commit_stats` for measurement and intentionally is not
exported from `grove.__init__` as production API. Databases use the existing
three-table schema and can be opened by either adapter.

## Correctness evidence

`tests/test_sqlite_incremental_experiment.py` provides:

* differential operations against a core `SQLiteTreeStore` (create, move,
  reorder, rename, update, delete-subtree, and create-at-index), comparing the
  complete ordered tree after every operation and after reopen;
* a write-count assertion for an isolated leaf update; and
* separate-interpreter `SIGKILL` tests at edge deletion, node update, metadata
  update, and immediately after SQL `COMMIT`.

The crash tests reopen with the core adapter, checking that a kill before
`COMMIT` recovers the previous complete snapshot and a kill after `COMMIT`
recovers the complete new snapshot. Foreign-key constraints, dense ordered
positions, parent/child consistency, schema validation, optimistic revision
checking, and subscriptions remain supplied by the existing core path.

The diff itself first reads and validates the durable old state under the write
lock. This is deliberately conservative: the update set is derived from the
rows actually present in SQLite rather than assuming that the transaction's
in-memory base is still durable. (As with the core adapter, metadata revision
is the optimistic conflict token, so an out-of-band edit with no revision
bump is outside the conflict contract.) It also means this prototype still
materializes the old state in Python, so the observed
benefit is primarily fewer SQLite writes, not yet a fully constant-memory
commit.

## Measurement

Command (repository root):

```text
uv run python benchmarks/bench_sqlite_incremental.py --nodes 2000 --updates 20
```

The benchmark creates 2,000 root children, then performs 20 isolated updates
to one leaf at a time. `sqlite3.Connection.total_changes` measures logical row
changes (including the one metadata row); `last_commit_stats.rows_written`
measures tree rows only.

Observed run (Python 3.14.5, macOS 26.5.1 arm64):

| adapter | median commit | p95 commit | median SQLite row changes | total row changes |
| --- | ---: | ---: | ---: | ---: |
| core full rewrite | 124.946 ms | 144.157 ms | 6,002 | 120,040 |
| incremental dirty rows | 38.652 ms | 42.830 ms | 2 | 40 |

The core rewrite touches 2,000 node updates + 2,000 edge deletes + 2,000
edge inserts + metadata for each update. The incremental path touches one node
row + metadata for each update. Thus this run reduced tree-row writes from
6,001 to 1 per update (a 6,001x logical tree-row reduction), and total SQLite
row changes from 6,002 to 2 (3,001x). Median wall-clock commit speedup was
3.233x. At 200 nodes / 8 updates, the corresponding speedup was 1.327x and
row-change reduction 301x.

The speedup is lower than the write reduction because the prototype validates
and materializes the old relational snapshot for every commit, and both paths
pay transaction/fsync costs. Results are workload- and machine-dependent; the
benchmark is intended for repeatable comparison rather than a performance
promise.

## Recommendation

Do not replace `SQLiteTreeStore` yet. The experiment demonstrates a substantial
reduction in dirty SQLite rows while retaining atomicity and order, but should
first gain a larger-workload benchmark, a conflict/out-of-band mutation matrix,
and a design for avoiding full old-state materialization (for example, a
transaction change journal or row-level digest). Keep the class isolated until
those measurements show that the added diff complexity is worthwhile.

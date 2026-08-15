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


## Bounded workload matrix (follow-up)

The isolated leaf result above is not representative of structural edits, so a
small workload matrix was run before deciding whether to continue the
prototype.  The protocol used Python 3.14.5 on macOS 26.5.1 arm64, 256
non-root nodes, three repetitions per case, and fresh temporary databases for
each adapter.  Each adapter received the same deterministic tree and mutation
sequence.  `sqlite3.Connection.total_changes` is the row-write measure and
includes the one metadata-row revision update; the dirty adapter's
`last_commit_stats.rows_written` is tree rows only.  The reported row counts
below are therefore deliberately conservative (they include metadata).

* **deep:** one chain;
* **broad:** all nodes are ordered children of root;
* **skew:** deterministic early-parent selection with exponent 4 and a 10%
  root probability;
* **reopen-like:** the same sequence, but close/reopen after every commit;
* mutation sequence: isolated leaf update, rename and rename-back, move to root
  and back (or root reorder), nested-property update, leaf delete/create, and a
  no-op update.  This is 8--10 commits depending on shape.

The latest bounded run produced these medians (milliseconds and logical SQLite
row changes):

| shape | mode | full commit p50 / p95 ms | dirty commit p50 / p95 ms | full rows p50 (total) | dirty rows p50 (total) | p50 row reduction | p50 time speedup |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| deep | mutations | 10.523 / 13.219 | 9.217 / 13.040 | 770 (23,124) | 3 (114) | 256.7x | 1.142x |
| broad | mutations | 5.952 / 8.942 | 4.762 / 6.770 | 770 (18,477) | 3 (4,665) | 256.7x | 1.250x |
| skew | mutations | 6.034 / 6.454 | 4.081 / 4.258 | 770 (20,787) | 3 (756) | 256.7x | 1.479x |
| deep | reopen-like | 10.650 / 13.263 | 9.800 / 11.401 | 770 (23,124) | 3 (114) | 256.7x | 1.087x |
| broad | reopen-like | 5.729 / 6.216 | 4.541 / 6.188 | 770 (18,477) | 3 (4,665) | 256.7x | 1.262x |
| skew | reopen-like | 5.829 / 6.090 | 3.995 / 4.234 | 770 (20,787) | 3 (756) | 256.7x | 1.459x |

For the reopen-like cases, including close/open in each cycle, the measured
full/dirty p50 cycle times were 16.303/15.953 ms (deep, 1.022x),
8.128/7.063 ms (broad, 1.151x), and 8.035/6.173 ms (skew, 1.302x).  These
small wall-time gains are much less dramatic than the row-write reduction,
consistent with the prototype still materializing and validating the complete
old state and paying SQLite transaction/reopen costs.

A separate bounded differential stream used 64-node trees, seeds 10--19, and
48 operations per seed per shape (create, update, rename, move, and recursive
leaf delete).  All 1,440 operations matched the full adapter's ordered tree;
reopening at operations 16, 32, and 48 also matched.  Dirty-row maxima were 14
(deep), 132 (broad), and 76 (skew).  The committed regression test
`tests/test_sqlite_incremental_workloads.py` retains the shape and
mutation/reopen coverage; run it with:

```text
uv run pytest -q tests/test_sqlite_incremental_workloads.py
```

The focused workload tests pass and the full repository suite remains green.
No production module or public API was changed.

## Termination decision

**Terminate this as an isolated experiment; do not promote it.**  The matrix
confirms the useful claim: isolated edits usually reduce SQLite logical row
writes by roughly two orders of magnitude or more.  It does **not** establish
a sufficiently consistent wall-clock win: reopen-like speedups were only
1.022--1.302x, and broad edge rewrites raised dirty p95 writes to 515 in the
larger 256-node sequence.  The implementation also parses/materializes the
entire old state under the write lock, so its asymptotic read/diff cost remains
whole-tree.  Further work would require a separately justified design (change
journal or row digests, edge-range update policy, and larger contention/crash
matrices) rather than core promotion of this subclass.

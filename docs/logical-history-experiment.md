# Logical snapshot/history prototype (isolated experiment)

## Question

GROVE currently offers detached read/query snapshots, but no durable logical
history API. This note compares three ways to expose history and records an
isolated prototype without changing the core package.

## Options

### 1. SQLite online backup (recommended first durable experiment)

At a capture point, open a read transaction on the live SQLite connection,
copy it to a temporary database with Python's `sqlite3.Connection.backup`,
validate the copied `metadata.revision` and root, then atomically rename it to
`snapshot-<revision>.db`. A history catalog can be added later; the prototype
uses filenames and enumerates them. Opening a snapshot validates it through
`SQLiteTreeStore`, then deep-copies its state into a standalone `TreeStore` so
callers cannot mutate the artifact.

This is a *logical revision artifact*: a full database copy, not an API that
keeps a transaction open indefinitely. Capture is idempotent by revision and
publishes only after validation. A failed capture removes its temporary file.
The prototype lives in `experiments/sqlite_history.py` and is intentionally
private/unsupported.

### 2. Long-lived SQLite read transaction

`BEGIN` in WAL mode gives snapshot isolation: all reads through that connection
see one historic committed view until COMMIT/ROLLBACK. This is useful for a
short, coherent query/export, but is a poor durable history object. It ties up a
connection and can retain WAL frames, has no stable handle after process exit,
and must not be confused with a revision identifier. Expose this only as a
scoped read context if needed, never as the primary durable history API.

### 3. Version table + structural sharing

A schema such as `revisions(revision, root_id, ...)` plus versioned node/edge
rows can retain logical roots and deduplicate unchanged records. A persistent
in-memory tree can do the same through immutable path-copying (structural
sharing). This can reduce storage/write cost for small edits, but requires
version-aware reads, garbage collection/retention, indexes, migration, and
careful reference/property semantics. GROVE's current state is mutable nested
Python dictionaries and each transaction deep-copies the complete state, so
introducing structural sharing would be a broad core rewrite—not justified by
this experiment.

## Recommendation

Adopt the online-backup artifact as the next *isolated* history proof of
concept. Define a future public API around explicit immutable handles, e.g.
`capture() -> Snapshot`, `snapshot(revision)`, `Snapshot.open()` and retention,
with no accidental writes to history. Use scoped read transactions only for
short-lived consistent reads. Revisit a version table/structural sharing only
when measured full-copy cost or retention requirements demand it; then design
it as a separate storage format with migration and GC, not as an optimization
inside `TreeStore`.

## Evidence and authoritative sources

* SQLite, “Online Backup API”: https://sqlite.org/backup.html — completing a
  backup makes the destination bit-wise identical to the source as it was when
  copying commenced (a snapshot); source locking is brief during reads.
* SQLite C API reference, `sqlite3_backup_*`:
  https://sqlite.org/c3ref/backup_finish.html — documents source/destination
  locking, retry/error behavior, and that backup may run against a live source.
* SQLite, “Isolation In SQLite”: https://sqlite.org/isolation.html — WAL
  readers see an unchanging database snapshot for the duration of a read
  transaction; SQLite serializes writes.
* SQLite, “Transactions”: https://sqlite.org/lang_transaction.html — read
  transactions continue seeing the historic snapshot until they end; only one
  writer at a time.
* SQLite, “Write-Ahead Logging”: https://sqlite.org/wal.html — WAL reader end
  marks provide point-in-time views and WAL has checkpoint/concurrency tradeoffs.
* Clojure, “Data Structures”: https://clojure.org/reference/data_structures
  — immutable persistent collections create modified versions using structural
  sharing and are inherently thread-safe (a concise authoritative description
  of the tradeoff, though not a GROVE implementation requirement).

## Reproducible smoke test

```
$ .venv/bin/python experiments/sqlite_history.py
1 True False
2 True True
```

The prototype has no retention deletion, encryption, cross-filesystem rename,
or multi-database atomicity; these are deliberate non-goals.

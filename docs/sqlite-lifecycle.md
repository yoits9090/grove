# SQLite lifecycle and multi-process boundaries

`SQLiteTreeStore` supports multiple processes sharing one file, within SQLite's
single-writer model. WAL is enabled for file-backed databases and each commit
is an atomic SQL transaction. Readers can continue while another process
writes; writers are serialized by SQLite.

## Bounded writer waits

Constructors accept `timeout`, `write_retries`, and `retry_delay`:

```python
db = SQLiteTreeStore(
    "grove.db", timeout=0.25, write_retries=3, retry_delay=0.01
)
```

`timeout` bounds each SQLite lock wait. On a transient `BUSY`/`LOCKED` error,
the store makes at most `write_retries` additional attempts with exponential
backoff. Exhaustion raises `InvalidOperationError`; it is never an unbounded
retry loop. A stale optimistic transaction still raises the normal transaction
conflict error and should be rebuilt from a fresh transaction.

The retry policy is local coordination only. It is not distributed locking,
leader election, or a guarantee that concurrent application-level operations
will both commit.

## Online backups

Call `backup(destination)` to make a consistent physical SQLite copy:

```python
with SQLiteTreeStore("grove.db") as db:
    db.backup("grove-copy.db")
```

The source read snapshot is pinned while Python's SQLite Online Backup API
copies it. Path destinations are written to a temporary sibling and atomically
renamed only after the copied revision and root are checked. Existing targets
are replaced. A caller-provided `sqlite3.Connection` may also be supplied; it
remains open after the call. Backups reject closed stores and source/destination
aliases explicitly.

A backup is a physical copy of one committed revision, not a history catalog
or rollback mechanism. Retention, encryption, and remote replication remain
outside this adapter.

## Crash and close behavior

SQLite rolls back an uncommitted transaction if a process is killed during a
commit. Reopening therefore observes either the previous complete tree or the
new complete tree, never partially rewritten relational rows. `close()` is
idempotent; public store operations after close raise `InvalidOperationError`
with `store is closed`. Detached transactions remain readable, but cannot
publish after their owning store closes.

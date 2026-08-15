# Initial architecture and decision record

## D-001: adjacency-list domain model (2025-02-14)

The primary hierarchy is an adjacency list: each node stores one `parent_id`
and its parent stores an ordered child-ID list. IDs are opaque and stable;
paths are derived indexes and are not identity. Names are unique among siblings,
with `/`, NUL, empty, `.`, and `..` rejected. Root is a sentinel with an empty
name and no parent. A move validates destination ancestry before changing either
edge, so the operation is all-or-nothing in a transaction.

This is intentionally simple and reversible. Child lists make insertion order
explicit and deterministic, while path lookup is O(depth + sibling scan) in the
reference implementation. A future SQLite adapter can use `(parent_id, ordinal)`
rows and a unique normalized-name constraint.

## D-002: whole-snapshot checksummed log (2025-02-14)

The first durable implementation appends a complete canonical JSON snapshot in a
length + CRC32 frame, flushes, and fsyncs before publishing state. On recovery,
truncated final frames are ignored, garbage is truncated only at the final
suffix, bytes between recognizable committed frames fail closed, and checksum
failures in complete frames fail closed. This makes crash behavior inspectable and is intentionally not a scale
solution. It supports zero-data-loss testing before optimizing the format.

Alternatives considered: SQLite (leading next experiment for local embedded ACID,
foreign-key/unique constraints, WAL readers, and recursive queries), embedded KV
(RocksDB/LMDB; less structural constraint support), and event-only logs (harder
read-model rebuild). Primary references: SQLite WAL and transaction docs
(https://sqlite.org/wal.html, https://sqlite.org/lang_transaction.html),
RocksDB basic operations (https://github.com/facebook/rocksdb/wiki/Basic-Operations),
and LMDB design material (https://www.symas.com/lmdb).

## D-003: typed property envelope

The Python API accepts null, booleans, finite numbers, strings, bytes,
timezone-aware datetimes, arrays, maps with string keys, and `Reference`. Values
are detached and cyclic containers are rejected. JSON uses tagged `$grove`
objects. References are non-owning and can dangle; they never affect primary
cycle checks.

## Known limitations

- Single-process thread safety only; transactions conflict optimistically and
  readers receive detached snapshots.
- No schema/index/query/history/snapshot API yet.
- Every durable commit rewrites the complete state; no streaming import/export.
- Unicode names are preserved but not case/normalization-folded.
- Subscriptions are best-effort synchronous callbacks and currently report only
  changed roots, not a compact subtree summary.


## D-004: scope and event semantics (2025-02-14)

The snapshot backend is single-owner (one process/instance per log path) in
this milestone. Its lock is thread-local; opening the same log from multiple
writers is not a supported concurrency mode and is a planned SQLite adapter
requirement. Recovery truncates only an incomplete or garbage suffix after a
valid frame; a non-empty file with no valid frame fails closed.

Subscriptions are synchronous, best-effort callbacks after a successful commit.
A transaction produces a small event batch (changed roots/parents, not every
removed descendant); recursive matching checks both pre- and post-commit
ancestry so move-out and deletion notifications are not lost. Callback errors
are isolated and do not roll back committed data.

Subtree exports are self-contained: their exported root has `parent_id: null`.
A complete root export can replace an empty destination database atomically;
non-empty roots reject replacement. With
`preserve_ids=False`, references to IDs inside the imported subtree are remapped,
while external references remain opaque and may dangle.


## D-005: SQLite WAL experiment (2025-02-14)

The SQLite adapter is experimental and intentionally uses three relational
structures: `metadata` (singleton revision/root), `nodes` (identity, parent,
properties, timestamps), and ordered `children` edges (parent, child, position).
The adapter enables WAL, foreign keys, and `synchronous=FULL` for file-backed
stores. It loads and validates the complete graph, stages mutations with the
existing detached transaction model, then rebuilds relational rows in one
`BEGIN IMMEDIATE` transaction. Durable revision comparison prevents stale
cross-instance commits; SQLite serializes writers and allows WAL readers.

This is a correctness-first adapter, not yet a production query engine: commits
rewrite relational rows for the complete state, reads materialize the tree, and
busy/lock errors are surfaced as operation errors rather than hidden retries.
The snapshot backend remains the reference oracle. See SQLite's authoritative
WAL, transaction, foreign-key, and synchronous documentation:
https://sqlite.org/wal.html, https://sqlite.org/lang_transaction.html,
https://sqlite.org/foreignkeys.html, and
https://sqlite.org/pragma.html#pragma_synchronous.

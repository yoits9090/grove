# Roadmap and priority queue

GROVE's mission for this repository is deliberately bounded: establish a small,
typed object-tree model with explicit invariants, atomic mutations, and
inspectable local durability; then measure conservative scale options without
turning an experiment into an API by implication. Correctness, crash behavior,
and reproducible evidence are release gates. Large-scale distributed storage,
remote replication, and a broad query language are not current goals.

## Delivered: trust and API foundation

1. **Complete:** the in-memory model has stable opaque IDs, one root, ordered
   children, unique sibling names, cycle-safe moves, detached node views,
   transactions, optimistic conflict detection, typed property envelopes, and
   atomic export/import.
2. **Complete:** checksummed whole-snapshot persistence provides fsync-before-
   publish commits and fail-closed recovery for corrupt complete frames,
   malformed initial files, and bytes between committed frames. It remains the
   reference oracle, not a scale solution.
3. **Complete:** query snapshots, typed predicates (including `bool` versus
   `int`), and lightweight in-memory `PropertyIndex` preserve primary child
   order and detached-result semantics.
4. **Complete:** deterministic model/differential tests, randomized
   deep/broad/skew/reopen workloads, typed round trips, transaction atomicity,
   and subprocess SQLite crash fixtures are checked in. The current suite
   collects 109 tests and passes with `uv run pytest -q`.

## Delivered: SQLite correctness and lifecycle slice

5. **Complete for correctness comparison:** `SQLiteTreeStore` uses WAL,
   foreign keys, relational ordered child edges, durable revisions, and
   cross-instance optimistic conflict detection. It still materializes reads
   when its revision changes and rewrites the complete relational tree at each
   commit; do not call it production-ready on this evidence alone.
6. **Complete:** the read-revision cache avoids rematerializing unchanged
   snapshots while retaining `data_version` and full validation on change.
   Online backup copies one committed revision and publishes path destinations
   atomically. Writer lock waits are bounded (`timeout`, finite retries, and
   backoff), `close()` is explicit/idempotent, and malformed out-of-band schema
   changes fail closed. These are lifecycle guarantees, not distributed
   coordination or a history catalog. See `docs/sqlite-lifecycle.md` and
   `docs/experiment-002-sqlite-read-cache.md`.
7. **Complete API slice:** optional `Schema` declarations support type,
   required-property, extra-property, enum, and callable constraints. Schema
   validation is atomic across create, update, import, transactions, initial
   state, and durable backends. Schemas are in-memory configuration; there is
   no schema migration/catalog format.

## Delivered: isolated experiments (not public API)

8. **Complete, terminate as prototype:** the durable scalar-property index has
   an ordered direct SQL path that decodes matching rows without full-tree
   materialization. It is still an attached sidecar experiment; predicate
   breadth, mutation/crash coverage, and workload evidence are insufficient to
   promote it to the core API.
9. **Complete, terminate as prototype:** content-addressed immutable node blobs
   and root manifests demonstrate unchanged-subtree sharing, canonical
   validation, corruption rejection, and GC accounting. Incremental Merkle
   hashing demonstrates digest reuse after local edits. Neither is a storage
   replacement or a wall-clock performance claim.
10. **Complete proof of concept:** SQLite online-backup history artifacts can be
    captured and reopened as detached stores. The prototype has no retention,
    encryption, remote replication, or public history handle; the core package
    intentionally exposes no durable history API.

## Next priorities (in order)

1. **SQLite readiness evidence before optimization:** expand deterministic
   multi-process contention, crash-boundary, reopen, malformed-database, and
   backup tests; run the documented workload families on supported Python
   versions; report write amplification, p50/p95 latency, WAL growth, and
   dataset-size limits. Keep the snapshot backend as the differential oracle.
   Exit only with reproducible results and explicit limits; do not hide lock
   failures or weaken fail-closed recovery.
2. **Choose the first scale investment from measurements:** if complete-tree
   commit cost is the limiting factor, prototype an incremental relational
   write path in a disposable branch and compare it against the current
   rewrite path. If query latency is the limiting factor, broaden the scalar
   sidecar experiment to the required predicate/scope semantics first. Promote
   neither without crash, mutation, ordering, typed-equality, and workload
   evidence.
3. **Define history requirements before designing a public API:** measure
   retention, diff/rollback, and snapshot-open use cases. If demand is real,
   harden the backup artifact with retention and explicit immutable handles;
   otherwise keep it private. Do not introduce version tables or structural
   sharing into `TreeStore` merely to satisfy an unmeasured possibility.
4. **Keep the public contract narrow and documented:** preserve detached
   snapshots, typed values, schema atomicity, bounded lifecycle behavior, and
   compatibility tests while any backend experiment evolves. Multi-process
   shared-file SQLite is supported within SQLite's single-writer model; a
   networked multi-writer service remains out of scope.

Every next item is evidence-first. New production behavior requires a focused
invariant/crash test and a documentation update; experiments must state a
termination criterion and remain isolated until their evidence justifies API
surface.

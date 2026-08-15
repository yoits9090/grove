# Roadmap and priority queue

## Now: establish trust (vertical slice delivered)

1. **Complete:** model-like randomized tests, typed round trips, transaction
   atomicity, and fault-oriented frame recovery fixtures.
2. **Complete:** API edge cases and compatibility contracts documented.
3. **Complete for this cycle:** reproducible random/deep/broad/skew/reopen
   workload families, differential backend tests, and SQLite subprocess crash
   recovery fixtures.

## Next: conservative scale experiment

4. **Started:** SQLite adapter using WAL, FK enforcement, relational ordered
   child edges, and durable commit revisions. Expand differential and crash
   tests before treating it as production-ready. Current adapter is complete
   enough for correctness comparison but still rewrites the logical tree per
   commit.
5. **Complete reference phase:** snapshot queries, typed predicate filtering,
   and lightweight in-memory property indexes. The public query/index APIs are
   intentionally detached-snapshot based and preserve primary child order.
6. **Experiment only:** a disposable durable scalar-index prototype exists. Its
   current end-to-end path is slower because final semantics still materialize
   the full tree, so it is not promoted to core. Logical-history artifacts are
   likewise isolated; no durable history API is part of the package.

## Later: ambitious experiments (disposable branches)

7. Persistent structural sharing/content-addressed snapshots; success means
   lower write amplification at unchanged-subtree workloads without weaker crash
   semantics. Terminate after a fixed prototype budget if recovery or complexity
   regresses.
8. Incremental Merkle hashes and lazy traversal for out-of-memory trees.
9. Historical revisions/diffs/rollback, schema validation, and multi-process API.


## Fifth-cycle decisions

7. **Completed experiment:** content-addressed immutable node blobs and atomic
   root manifests demonstrated unchanged-subtree sharing, strict canonical
   validation, corruption rejection, and GC accounting. It remains isolated;
   success does not justify replacing SQLite without larger write-amplification
   and crash evidence.
8. **Completed experiment:** incremental Merkle hashing measures reusable
   subtree digests. Hashes intentionally exclude mutable modification metadata
   for location-independent subtree content; full-store callers can include
   ancestry through the exported structure.
9. **Completed API slice:** optional schema validation is atomic across create,
   update, import, transactions, and durable backends.
10. **Completed lifecycle slice:** bounded SQLite writer retries, online backup,
   closed-handle errors, and malformed out-of-band schema fail-closed behavior.

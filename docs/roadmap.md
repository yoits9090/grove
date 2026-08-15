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
5. Query API and secondary indexes, designed against a reference evaluator.
6. Better event batches/subtree change summaries and subscription backpressure.

## Later: ambitious experiments (disposable branches)

7. Persistent structural sharing/content-addressed snapshots; success means
   lower write amplification at unchanged-subtree workloads without weaker crash
   semantics. Terminate after a fixed prototype budget if recovery or complexity
   regresses.
8. Incremental Merkle hashes and lazy traversal for out-of-memory trees.
9. Historical revisions/diffs/rollback, schema validation, and multi-process API.

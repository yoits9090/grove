# Roadmap and priority queue

## Now: establish trust (vertical slice delivered)

1. **Complete:** model-like randomized tests, typed round trips, transaction
   atomicity, and fault-oriented frame recovery fixtures.
2. **Complete:** API edge cases and compatibility contracts documented.
3. **Started:** reproducible smoke benchmark; broad/deep/property-heavy suites
   remain the next measurement task.

## Next: conservative scale experiment

4. SQLite adapter using WAL, FK enforcement, unique sibling names, and explicit
   commit revisions; compare correctness and write/read throughput against the
   snapshot log.
5. Query API and secondary indexes, designed against a reference evaluator.
6. Better event batches/subtree change summaries and subscription backpressure.

## Later: ambitious experiments (disposable branches)

7. Persistent structural sharing/content-addressed snapshots; success means
   lower write amplification at unchanged-subtree workloads without weaker crash
   semantics. Terminate after a fixed prototype budget if recovery or complexity
   regresses.
8. Incremental Merkle hashes and lazy traversal for out-of-memory trees.
9. Historical revisions/diffs/rollback, schema validation, and multi-process API.

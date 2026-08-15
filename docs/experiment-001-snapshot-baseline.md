# Experiment 001: checksummed whole-snapshot baseline

**Question.** Can a deliberately simple append-only snapshot log provide a
trustworthy persistence baseline before optimizing storage?

**Approach.** Each successful transaction serializes the complete logical state
to canonical UTF-8 JSON, frames it with a six-byte magic, payload length, and
CRC32, then writes, flushes, and fsyncs before publishing in-memory state.
Recovery accepts the last complete valid frame, truncates only an incomplete or
garbage suffix, rejects a non-empty file with no valid frame, and rejects bytes
between recognizable committed frames.

**Evidence.** 20 tests pass, including typed JSON round trips, failed-operation
rollback, model-like randomized operations (100 seeds in an interactive smoke
run), corrupt initial files, valid-prefix/torn-tail recovery, and corruption
between commits. Smoke benchmark (macOS arm64, Python 3.14.5, seed 7, 200 nodes,
100 reads): 33.505 ms build, 0.262 ms export, 0.917 us read p50, 1.000 us read
p95, 2.365 ms durable root import commit.

**Conclusion.** Keep this as the conservative reference implementation. It is
clear and crash-testable, but writes and recovery are O(total historical frame
bytes) and each commit is O(current tree size). It is single-owner per path;
its lock is process-local. Do not present it as the scalable foundation.

**Next experiment.** Implement a storage-interface-compatible SQLite WAL
adapter with adjacency rows, explicit sibling order, unique sibling names,
foreign keys, and a commit revision/event table. Compare correctness first,
then workload families and write amplification. SQLite's documented single
writer/WAL behavior and backup API make this the leading conservative scale
candidate; use Postgres only if multi-writer/network service requirements emerge.

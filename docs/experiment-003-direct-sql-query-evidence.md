# Experiment 003: bounded direct-SQL scalar query evidence

**Status: isolated experiment; not promoted to the public query API.**

This report records a bounded direct-SQL scalar-index workload and differential
mutation check. The direct path is implemented by
`SQLiteScalarPropertyIndexExperiment` and is intentionally separate from the
public `SQLiteTreeStore.query` API.

## Workload and environment

Command (run from repository root):

```bash
uv run python benchmarks/bench_sqlite_property_index.py \
  --nodes 5000 --groups 100 --reads 100 --repeats 3
```

The run used Python 3.14.5 on the local macOS arm64 development machine. The
benchmark builds equivalent 5,000-node trees, indexes the scalar `group`
property, and compares 100 exact lookups per repeat (three repeats) against a
materialized `Query` implementation. Candidate count is 50 for the selected
group. Results are wall-clock observations, not service-level targets:

| nodes | groups | baseline median (ms) | direct median (ms) | ratio |
|---:|---:|---:|---:|---:|
| 5,000 | 100 | 3,369.784 | 475.114 | 7.093x |
| 5,000 | 10 | 3,623.597 | 838.999 | 4.319x |
| 5,000 | 1,000 | 3,237.507 | 431.280 | 7.507x |

The three-run samples for the first row were baseline 3,335.601 / 3,369.784 /
3,907.191 ms and direct 475.114 / 451.257 / 547.058 ms. The ratio decreases
when the matching candidate set grows, as expected. This is not evidence of a
general query speedup: only exact scalar equality on a registered sidecar index
is measured, and index build/write cost is excluded from the read loop.

## Differential mutation validation

Twenty deterministic streams (seeds 100 through 119) built a mixed broad/deep
30-node tree, then replayed 45 random create/update/rename/move/recursive-delete
mutations per stream. After each mutation, scoped direct lookups were compared
to materialized queries across:

- root and randomly selected non-root targets;
- recursive and direct-child traversal;
- `include_root` true and false;
- typed scalar values (`None`, booleans, integers, float, and strings).

All **900 mutation steps** and their scoped lookup comparisons matched. The
full test suite passed (`uv run pytest -q`, 124 tests at the time of this
report), and the focused scalar-index tests passed independently.

## Semantic bug found and fixed

The differential scope check found that a direct lookup rooted at a non-root
node failed with `StorageCorruptionError` when a matching record was decoded:
the CTE traversal anchor had been passed as the `root_id` to node-name
validation. This incorrectly treated the non-root target as the singleton
root, whose empty-name sentinel it did not have. Decoding now validates against
the metadata singleton root while retaining the target as the traversal
anchor. A regression test covers recursive/direct and include-root combinations
for non-root targets.

## Promotion decision

Do **not** promote the direct path based on these results. Remaining evidence
needs include larger mutation/property-shape matrices, sidecar corruption and
rebuild checks, contention and crash boundaries, write amplification/index
maintenance cost, and p50/p95 measurements with index-build costs included.

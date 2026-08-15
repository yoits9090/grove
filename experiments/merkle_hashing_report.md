# Incremental Merkle hashing experiment

## Question

Can a Merkle digest over GROVE exported subtrees avoid cryptographic work for
unchanged subtrees after a small update, while remaining sensitive to move,
rename, and property changes?

## Method

`experiments/merkle_hashing.py` consumes the existing `TreeStore.export()`
representation; no `grove/` code or public API is changed. Node payloads are
canonical UTF-8 JSON (`ensure_ascii=False`, recursively sorted object keys,
compact separators, and `allow_nan=False`). Each node digest is SHA-256 over a
length-delimited node payload followed by the ordered child digests. A domain
separator distinguishes this experimental digest from unrelated hashes.

`IncrementalMerkle.update()` parses a fresh export and compares recursively
built structural signatures. If a node payload and all ordered descendants are
unchanged, its prior digest (and all cached descendant digests) is reused as a
unit. `UpdateStats.nodes_hashed` counts actual SHA-256 node computations;
`nodes_reused` counts cached nodes covered by reuse. Export parsing and
canonicalization still visit the fresh export, so this is a hashing-cost
experiment, not an incremental-export or incremental-storage implementation.

The cache relies on GROVE's stable opaque IDs. `store.export("/")` should be
used for a complete hierarchy: a move changes the moved node's `parent_id`,
the source/destination child ordering, and touched timestamps. A detached
subtree export has `parent_id=None` for its exported root by GROVE contract;
its digest represents that exported content and still includes `modified_at`.

## Reproduction

```sh
uv run pytest -q tests/test_merkle_hashing_experiment.py
uv run python experiments/merkle_hashing.py
```

The script creates 8 branches with 128 leaves each (1,033 nodes), updates one
leaf property, and prints JSON statistics. A representative run (timings vary
by machine) produced:

```json
{
  "nodes": 1033,
  "incremental": {
    "nodes_hashed": 3,
    "nodes_reused": 1030,
    "nodes_visited": 137
  },
  "full": {"nodes_hashed": 1033}
}
```

The three hashes are the changed leaf, its branch, and the root. The untouched
branches (and their leaves) remain cached. The changed leaf's digest and root
digest differ from the previous revision, while a complete fresh hash equals
the incremental result.

## Correctness coverage

Tests verify deterministic canonical encoding, reuse of an unchanged sibling
subtree, full-tree hash equality against a fresh computation, and distinct
hashes after each of rename, move, and property update. A detached subtree
export is also checked for `parent_id=None`; move-related `modified_at` remains
part of the payload, making the mutation observable without relying on a
private store path.

## Limitations and decision

This is intentionally isolated and observational. Parsing every export,
building recursive signatures, timestamp churn, collision considerations, and
lack of persistent cache serialization mean it is not a production API or a
claim of wall-clock speedup. The evidence supports further investigation only
for workloads where stable IDs and exported subtrees are already available;
any production design would need an explicit timestamp policy, cache lifecycle,
concurrency model, and benchmark against a real storage/update path.

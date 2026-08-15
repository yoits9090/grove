# Content-addressed structural-sharing snapshot experiment

## Hypothesis

Immutable canonical node blobs addressed by SHA-256, plus a small root
manifest, can publish snapshots atomically while reusing unchanged subtrees.
A move should rewrite only the ancestor blobs whose child-hash edge changed;
the moved subtree itself should retain its hashes.

## Protocol

`content_addressed_snapshots.py` captures a detached GROVE state into
`nodes/<sha256>.blob`, where each canonical blob contains node metadata and
ordered child hashes but deliberately omits `parent_id`. It then writes a
content-addressed manifest and atomically replaces `CURRENT`. `open()` verifies
hashes, reconstructs parent IDs, checks GROVE invariants, and returns a detached
`TreeStore`. `report()` accounts for blobs/manifests and reachability;
`gc(keep=N)` removes old manifests and unreachable blobs, with `dry_run=True`
for a no-change plan.

## Success criteria

* Re-capturing identical content produces the same root hash and does not
  rewrite any immutable blob.
* Moving a subtree changes its ancestors' blobs but preserves every blob below
  the moved root; both old and new manifests open to their original trees.
* A reader either sees the previous `CURRENT` manifest or the complete new one;
  temporary files are never accepted as blobs/manifests.
* Corrupt or mismatched blob bytes fail closed with `StorageCorruptionError`.
* GC marks from all retained roots, does not delete shared blobs, and a dry run
  leaves report/accounting unchanged.
* No production module or public GROVE API is modified by this experiment.

The accompanying `tests/test_content_addressed_snapshots.py` exercises move
sharing, detached round-trips, GC/dry-run, and corruption detection. Run it
from a checkout with `PYTHONPATH=. uv run pytest -q
 tests/test_content_addressed_snapshots.py` (the package is intentionally not
included in the production setuptools package).

## Termination criteria and non-goals

Stop this prototype when the criteria above pass on local filesystems and the
small test suite is green. It is not a benchmark or production implementation:
there is no concurrent writer coordination, quota policy, encryption, remote
object store, crash-injection harness, or incremental source-state traversal.
Those are follow-up experiments requiring workload and durability measurements.

"""Tests for the isolated structural-sharing snapshot experiment."""
from pathlib import Path

import pytest

from grove import TreeStore
from grove.errors import StorageCorruptionError
from experiments.content_addressed_snapshots import ContentAddressedSnapshotStore


def test_move_reuses_unchanged_subtree_and_round_trips(tmp_path: Path):
    source = TreeStore()
    left = source.create("left")
    leaf = source.create("leaf", parent=left.id, properties={"kind": "shared"})
    right = source.create("right")
    repo = ContentAddressedSnapshotStore(tmp_path)

    first = repo.capture(source)
    first_blobs = {path.stem for path in (tmp_path / "nodes").glob("*.blob")}
    source.move(left.id, right.id)
    second = repo.capture(source)
    second_blobs = {path.stem for path in (tmp_path / "nodes").glob("*.blob")}

    # The moved subtree is represented by exactly the same immutable blobs;
    # only the ancestors whose child-hash edge changed need new blobs.
    assert first.root_hash != second.root_hash
    assert first_blobs <= second_blobs
    assert second.open().path(leaf.id) == "/right/left/leaf"
    assert first.open().path(leaf.id) == "/left/leaf"
    assert repo.current().snapshot_id == second.snapshot_id

    detached = second.open()
    detached.rename(leaf.id, "changed-only-in-detached-store")
    assert second.open().get(leaf.id).name == "leaf"


def test_gc_marks_retained_roots_and_supports_dry_run(tmp_path: Path):
    source = TreeStore()
    source.create("before")
    repo = ContentAddressedSnapshotStore(tmp_path)
    first = repo.capture(source)
    source.create("after")
    second = repo.capture(source)

    before = repo.report()
    plan = repo.gc(keep=1, dry_run=True)
    assert plan.kept_snapshots == (second.snapshot_id,)
    assert first.snapshot_id in plan.removed_snapshots
    assert plan.reclaimed_bytes > 0
    assert repo.report() == before

    done = repo.gc(keep=1)
    assert done.deleted_blob_count == len(done.deleted_blobs)
    assert repo.snapshots() == (second,)
    assert repo.report().unreachable_blob_count == 0


def test_blob_corruption_is_detected_on_open(tmp_path: Path):
    source = TreeStore()
    source.create("node")
    repo = ContentAddressedSnapshotStore(tmp_path)
    snapshot = repo.capture(source)
    blob = next((tmp_path / "nodes").glob("*.blob"))
    blob.write_bytes(blob.read_bytes() + b"corruption")
    with pytest.raises(StorageCorruptionError):
        snapshot.open()

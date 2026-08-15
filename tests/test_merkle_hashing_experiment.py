"""Tests for the isolated incremental Merkle experiment."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))

from experiments.merkle_hashing import (
    IncrementalMerkle,
    canonical_encode,
    merkle_hash,
)
from grove import TreeStore


def _tree() -> tuple[TreeStore, object, object, object, object, object]:
    store = TreeStore()
    left = store.create("left", properties={"side": "stable"})
    left_a = store.create("a", parent=left.id, properties={"value": 1})
    left_b = store.create("b", parent=left.id, properties={"value": 2})
    right = store.create("right")
    right_a = store.create("a", parent=right.id, properties={"value": 3})
    return store, left, left_a, left_b, right, right_a


def test_canonical_encoding_is_deterministic_and_typed():
    first = {"z": [1, True], "a": {"b": "é", "a": None}}
    second = {"a": {"a": None, "b": "é"}, "z": [1, True]}
    assert canonical_encode(first) == canonical_encode(second)
    assert canonical_encode(first).decode() == '{"a":{"a":null,"b":"é"},"z":[1,true]}'


def test_incremental_update_reuses_unchanged_sibling_subtrees():
    store, left, left_a, left_b, right, right_a = _tree()
    merkle = IncrementalMerkle.from_store(store)
    old_hashes = merkle.node_hashes

    store.update(left_a.id, properties={"value": 10})
    stats = merkle.update(store.export("/"))

    assert merkle.root_hash == merkle_hash(store.export("/"))
    assert old_hashes[right.id] == merkle.node_hash(right.id)
    assert old_hashes[right_a.id] == merkle.node_hash(right_a.id)
    assert right.id in stats.reused_node_ids
    # The changed path is rehashed, while the complete right subtree is one
    # reusable unit and its descendants need not be cryptographically hashed.
    assert left_a.id in stats.hashed_node_ids
    assert stats.nodes_hashed < len(old_hashes)
    assert stats.nodes_reused >= 2
    assert left_a.id in stats.changed_node_ids
    assert merkle.node_hash(left_a.id) != old_hashes[left_a.id]


def test_move_rename_and_property_changes_change_full_tree_hashes():
    store, left, left_a, left_b, right, right_a = _tree()
    merkle = IncrementalMerkle.from_store(store)
    hashes = [merkle.root_hash]
    left_a_hash = merkle.node_hash(left_a.id)

    store.rename(left_a.id, "renamed")
    stats = merkle.update(store.export("/"))
    hashes.append(merkle.root_hash)
    assert stats.nodes_hashed >= 3  # renamed node, parent, and root
    assert merkle.node_hash(left_a.id) != left_a_hash

    renamed_hash = merkle.node_hash(left_a.id)
    store.move(left_a.id, right.id)
    stats = merkle.update(store.export("/"))
    hashes.append(merkle.root_hash)
    assert stats.nodes_hashed >= 3  # moved node and both affected ancestors
    assert merkle.node_hash(left_a.id) != renamed_hash

    moved_hash = merkle.node_hash(left_a.id)
    store.update(left_a.id, properties={"value": 99})
    stats = merkle.update(store.export("/"))
    hashes.append(merkle.root_hash)
    assert stats.nodes_hashed >= 3
    assert merkle.node_hash(left_a.id) != moved_hash
    assert len(set(hashes)) == 4
    assert merkle.root_hash == merkle_hash(store.export("/"))


def test_subtree_export_is_self_contained_and_hashes_stably_across_move():
    store, left, left_a, left_b, right, right_a = _tree()
    before = store.export(left.id)
    before_hash = merkle_hash(before)
    store.move(left.id, right.id)
    after = store.export(left.id)
    # GROVE export detaches the exported root's parent_id; the content hash is
    # location-independent for a subtree and excludes mutable modification
    # metadata.
    assert before["parent_id"] is None and after["parent_id"] is None
    assert before_hash == merkle_hash(after)

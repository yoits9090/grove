"""Differential tests for the in-memory and durable tree backends.

The operation stream deliberately addresses nodes by path rather than by ID.
That lets the stream include ``copy`` (which allocates IDs) while still being
identical for all three backends.  Canonical snapshots omit implementation
specific IDs and wall-clock timestamps, but preserve hierarchy, ordering,
types, and all property values (including references resolved to paths).
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

import pytest

from grove import (
    PersistentTreeStore,
    Reference,
    SQLiteTreeStore,
    TreeStore,
)


# A fixed instant keeps typed timestamp properties byte-for-byte comparable;
# node creation/modification timestamps are intentionally not compared because
# three stores are committed one after another and therefore have different
# wall-clock values.
_FIXED_TIME = dt.datetime(2020, 1, 2, 3, 4, 5, 678901, tzinfo=dt.timezone.utc)


def _canonical(store: TreeStore) -> tuple[Any, ...]:
    """Return an ID-independent representation of the complete tree.

    IDs and timestamps are backend-specific (and copies allocate fresh UUIDs),
    whereas paths, child order, types, and properties are the observable model
    we want to compare.  References are converted to paths where possible so
    internal references in copied subtrees compare correctly as well.
    """

    id_to_path: dict[str, str] = {}
    nodes: dict[str, Any] = {}

    def discover(node_id: str, path: str) -> None:
        node = store.get(node_id)
        id_to_path[node.id] = path
        nodes[path] = node
        for child_id in node.children:
            child = store.get(child_id)
            child_path = path.rstrip("/") + "/" + child.name
            discover(child_id, child_path)

    discover(store.root.id, "/")

    def value(item: Any) -> tuple[Any, ...]:
        if item is None:
            return ("none",)
        if isinstance(item, bool):
            return ("bool", item)
        if isinstance(item, int):
            return ("int", item)
        if isinstance(item, float):
            return ("float", item)
        if isinstance(item, str):
            return ("str", item)
        if isinstance(item, bytes):
            return ("bytes", item)
        if isinstance(item, dt.datetime):
            return ("datetime", item.isoformat())
        if isinstance(item, Reference):
            target = id_to_path.get(item.node_id)
            # Copy allocates UUIDs independently in each backend.  Once a
            # referenced copied node is deleted, its opaque ID is no longer
            # observable through the tree, so compare all such dangling links
            # by that observable fact rather than by backend-local UUID.
            return ("reference-path", target) if target is not None else (
                "reference-dangling",
            )
        if isinstance(item, list):
            return ("list", tuple(value(x) for x in item))
        if isinstance(item, dict):
            return (
                "map",
                tuple((key, value(item[key])) for key in sorted(item)),
            )
        raise AssertionError(f"unexpected property value: {item!r}")

    def node_record(path: str) -> tuple[Any, ...]:
        node = nodes[path]
        return (
            node.name,
            node.type,
            value(node.properties),
            tuple(
                node_record(path.rstrip("/") + "/" + nodes[child_path].name)
                for child_path in (
                    path.rstrip("/") + "/" + store.get(child_id).name
                    for child_id in node.children
                )
            ),
        )

    return node_record("/")


def _entries(store: TreeStore) -> list[tuple[str, Any]]:
    """List current nodes as (path, Node), in tree order."""

    result: list[tuple[str, Any]] = []

    def visit(node_id: str, path: str) -> None:
        node = store.get(node_id)
        result.append((path, node))
        for child_id in node.children:
            child = store.get(child_id)
            visit(child_id, path.rstrip("/") + "/" + child.name)

    visit(store.root.id, "/")
    return result


def _descendants(store: TreeStore, node_id: str) -> set[str]:
    result: set[str] = set()
    for child_id in store.get(node_id).children:
        result.add(child_id)
        result.update(_descendants(store, child_id))
    return result


def _apply(store: TreeStore, operation: tuple[Any, ...]) -> None:
    kind, args = operation[0], operation[1:]
    if kind == "create":
        name, parent, node_id, typ, properties = args
        store.create(
            name,
            parent=parent,
            node_id=node_id,
            type=typ,
            properties=properties,
        )
    elif kind == "update":
        target, properties, typ, merge = args
        store.update(target, properties=properties, type=typ, merge=merge)
    elif kind == "rename":
        target, name = args
        store.rename(target, name)
    elif kind == "move":
        target, parent, index = args
        store.move(target, parent, index=index)
    elif kind == "copy":
        target, parent, name, index = args
        store.copy(target, parent, name=name, index=index)
    elif kind == "delete":
        (target,) = args
        store.delete(target, recursive=True)
    else:  # pragma: no cover - guards accidental stream typos
        raise AssertionError(f"unknown operation {kind!r}")


def _next_operation(
    store: TreeStore, rng: random.Random, seed: int, step: int, known_ids: list[str]
) -> tuple[Any, ...]:
    """Choose one valid operation from the current oracle state."""

    entries = _entries(store)
    all_paths = [path for path, _ in entries]
    nonroot = [(path, node) for path, node in entries if path != "/"]

    # Bootstrap enough structure to guarantee that every operation family is
    # exercised in every deterministic stream.  Later choices are randomized.
    if step < 5:
        kind = "create"
    elif step == 5:
        kind = "update"
    elif step == 6:
        kind = "rename"
    elif step == 7:
        kind = "move"
    elif step == 8:
        kind = "copy"
    elif step == 9:
        kind = "delete"
    elif len(entries) == 1:
        kind = "create"
    else:
        kind = rng.choices(
            ["create", "update", "rename", "move", "copy", "delete"],
            weights=[38, 20, 11, 14, 9, 8],
            k=1,
        )[0]

    if kind == "create":
        serial = len(known_ids)
        node_id = f"differential-{seed}-{serial}"
        name = f"n{seed}_{step}_{serial}"
        parent = rng.choice(all_paths)
        properties: dict[str, Any] = {
            "created-by": "differential",
            "ordinal": serial,
            "nested": [seed, {"step": step, "enabled": bool(step % 2)}],
        }
        if known_ids:
            properties["link"] = Reference(rng.choice(known_ids))
        known_ids.append(node_id)
        return ("create", name, parent, node_id, f"kind-{step % 4}", properties)

    if kind == "update":
        target, _ = rng.choice(entries)
        properties: dict[str, Any] = {
            "step": step,
            "seed": seed,
            "blob": bytes((seed % 256, step % 256, (seed + step) % 256)),
            "when": _FIXED_TIME,
            "nested": {"flag": bool(rng.getrandbits(1)), "items": [1, 2, 3]},
        }
        if known_ids:
            properties["link"] = Reference(rng.choice(known_ids))
        return (
            "update",
            target,
            properties,
            f"updated-{(seed + step) % 5}",
            bool(rng.getrandbits(1)),
        )

    if kind == "rename":
        target, node = rng.choice(nonroot)
        parent = store.get(node.parent_id)
        name = f"r{seed}_{step}"
        sibling_names = {store.get(child_id).name for child_id in parent.children}
        suffix = 0
        while name in sibling_names and name != node.name:
            suffix += 1
            name = f"r{seed}_{step}_{suffix}"
        return ("rename", target, name)

    if kind == "move":
        target, node = rng.choice(nonroot)
        blocked = {node.id} | _descendants(store, node.id)
        candidates: list[tuple[str, Any]] = []
        for parent_path, parent in entries:
            if parent.id in blocked:
                continue
            sibling_names = {
                store.get(child_id).name
                for child_id in parent.children
                if child_id != node.id
            }
            if node.name not in sibling_names:
                candidates.append((parent_path, parent))
        # The current parent is always a candidate, so this cannot be empty.
        parent_path, parent = rng.choice(candidates)
        same_parent = parent.id == node.parent_id
        available = len(parent.children) - (1 if same_parent else 0)
        index = rng.randrange(available + 1)
        return ("move", target, parent_path, index)

    if kind == "copy":
        target, source = rng.choice(entries)
        name = f"c{seed}_{step}"
        candidates: list[tuple[str, Any]] = []
        for parent_path, parent in entries:
            if name not in {store.get(child_id).name for child_id in parent.children}:
                candidates.append((parent_path, parent))
        parent_path, parent = rng.choice(candidates)
        index = rng.randrange(len(parent.children) + 1)
        return ("copy", target, parent_path, name, index)

    # Recursive deletion is valid for every non-root node, including leaves.
    target, _ = rng.choice(nonroot)
    return ("delete", target)


@pytest.mark.parametrize("seed", (7, 31, 113))
def test_randomized_backends_are_differentially_equivalent_after_reopen(
    tmp_path, seed: int
) -> None:
    """Replay deterministic valid streams and compare all observable state."""

    rng = random.Random(seed)
    persistent_path = tmp_path / f"differential-{seed}.log"
    sqlite_path = tmp_path / f"differential-{seed}.sqlite"
    oracle = TreeStore()
    persistent = PersistentTreeStore(persistent_path)
    sqlite = SQLiteTreeStore(sqlite_path)
    stores = [oracle, persistent, sqlite]
    known_ids: list[str] = []

    try:
        for step in range(80):
            operation = _next_operation(oracle, rng, seed, step, known_ids)
            for store in stores:
                _apply(store, operation)

            expected = _canonical(oracle)
            assert _canonical(persistent) == expected, (seed, step, operation)
            assert _canonical(sqlite) == expected, (seed, step, operation)

        expected = _canonical(oracle)
    finally:
        # SQLite has a real close operation; PersistentTreeStore.close is a
        # no-op but calling both makes the reopen boundary explicit.
        persistent.close()
        sqlite.close()

    with PersistentTreeStore(persistent_path) as reopened:
        assert _canonical(reopened) == expected
    with SQLiteTreeStore(sqlite_path) as reopened:
        assert _canonical(reopened) == expected

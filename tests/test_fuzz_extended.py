"""Bounded randomized and hostile-input coverage for GROVE.

These tests intentionally use ``random.Random`` rather than an optional
third-party property-testing package.  Fixed seeds make failures reproducible,
while the generated operation streams still exercise many combinations of
ordering, unicode, paths, and hierarchy mutations in a short test run.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import random
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from grove import (
    InvalidOperationError,
    NotFoundError,
    Reference,
    SQLiteTreeStore,
    StorageCorruptionError,
    TreeStore,
)


_UNICODE_NAMES = ("café", "子", "узел", "مرحبا", "👩‍💻", "e\u0301", "नोड")
_UNICODE_TEXT = "東京・café・مرحبا・👩‍💻・e\u0301"


def _descendants(model: dict[str, dict[str, Any]], node_id: str) -> set[str]:
    found: set[str] = set()
    for child_id in model[node_id]["children"]:
        found.add(child_id)
        found.update(_descendants(model, child_id))
    return found


def _assert_model(
    store: TreeStore, model: dict[str, dict[str, Any]], root_id: str
) -> None:
    """Check all public hierarchy observations against a tiny model oracle."""
    assert store.root.id == root_id
    assert store.root.name == ""
    visited: set[str] = set()

    def visit(node_id: str, expected_path: str) -> None:
        assert node_id not in visited, "the public tree must be acyclic"
        visited.add(node_id)
        expected = model[node_id]
        node = store.get(node_id)
        assert node.id == node_id
        assert node.name == expected["name"]
        assert node.type == expected["type"]
        assert node.properties == expected["properties"]
        assert node.parent_id == expected["parent"]
        assert list(node.children) == expected["children"]
        assert store.path(node_id) == expected_path
        assert store.get(expected_path).id == node_id
        assert len(node.children) == len(set(node.children))
        for child_id in node.children:
            assert child_id in model
            assert model[child_id]["parent"] == node_id
            child_path = expected_path.rstrip("/") + "/" + model[child_id]["name"]
            visit(child_id, child_path)

    visit(root_id, "/")
    assert visited == set(model)
    # Every model parent pointer must be represented by exactly one edge.
    for node_id, expected in model.items():
        if node_id == root_id:
            assert expected["parent"] is None
        else:
            assert expected["parent"] in model
            assert node_id in model[expected["parent"]]["children"]


def _seed_model(root_id: str) -> dict[str, dict[str, Any]]:
    return {
        root_id: {
            "name": "",
            "parent": None,
            "children": [],
            "type": "root",
            "properties": {},
        }
    }


@pytest.mark.parametrize("seed", (3, 29))
def test_seeded_random_operations_preserve_model_invariants_and_sqlite_reopen(
    tmp_path: Path, seed: int
) -> None:
    """Replay bounded valid streams against memory and SQLite backends.

    The model updates are deliberately independent of the store's child lists;
    this catches parent/child and ordering mistakes, including same-parent
    moves.  Reopening midway also checks that randomized states remain valid in
    the relational representation.
    """
    rng = random.Random(seed)
    path = tmp_path / f"model-{seed}.sqlite"
    memory = TreeStore()
    durable: SQLiteTreeStore = SQLiteTreeStore(path)
    root_id = memory.root.id
    durable_root_id = durable.root.id
    # IDs are explicit so the two backends can share the same model shape.
    model = _seed_model(root_id)
    durable_model = _seed_model(durable_root_id)

    def apply_create(store: TreeStore, expected: dict[str, dict[str, Any]], step: int) -> None:
        parent = rng.choice(list(expected))
        node_id = f"random-{seed}-{step}"
        name = f"{_UNICODE_NAMES[step % len(_UNICODE_NAMES)]}-{seed}-{step}"
        props = {
            "text": _UNICODE_TEXT,
            "step": step,
            "nested": [step % 2 == 0, {"seed": seed}],
        }
        node = store.create(
            name,
            parent=parent,
            node_id=node_id,
            type=f"kind-{step % 4}",
            properties=props,
        )
        assert node.id == node_id
        expected[node_id] = {
            "name": name,
            "parent": parent,
            "children": [],
            "type": f"kind-{step % 4}",
            "properties": copy.deepcopy(props),
        }
        expected[parent]["children"].append(node_id)

    def apply_update(store: TreeStore, expected: dict[str, dict[str, Any]], step: int) -> None:
        node_id = rng.choice(list(expected))
        props = {"step": step, "text": _UNICODE_TEXT, "bytes": bytes([step, seed])}
        merge = bool(rng.getrandbits(1))
        typ = f"updated-{step % 3}"
        store.update(node_id, properties=props, type=typ, merge=merge)
        expected[node_id]["properties"] = (
            {**expected[node_id]["properties"], **props} if merge else copy.deepcopy(props)
        )
        expected[node_id]["type"] = typ

    def apply_rename(store: TreeStore, expected: dict[str, dict[str, Any]], step: int) -> None:
        node_id = rng.choice([nid for nid, record in expected.items() if record["parent"] is not None])
        parent = expected[node_id]["parent"]
        name = f"改名-{_UNICODE_NAMES[(step + 2) % len(_UNICODE_NAMES)]}-{step}"
        # The step suffix makes this unique among siblings and across the run.
        store.rename(node_id, name)
        expected[node_id]["name"] = name

    def apply_move(store: TreeStore, expected: dict[str, dict[str, Any]], step: int) -> None:
        node_id = rng.choice([nid for nid, record in expected.items() if record["parent"] is not None])
        blocked = {node_id} | _descendants(expected, node_id)
        parent = rng.choice([nid for nid in expected if nid not in blocked])
        old_parent = expected[node_id]["parent"]
        destination = expected[parent]["children"]
        available = len(destination) - (1 if parent == old_parent else 0)
        index = rng.randrange(available + 1)
        store.move(node_id, parent, index=index)
        expected[old_parent]["children"].remove(node_id)
        expected[parent]["children"].insert(index, node_id)
        expected[node_id]["parent"] = parent

    def apply_delete(store: TreeStore, expected: dict[str, dict[str, Any]]) -> None:
        node_id = rng.choice([nid for nid, record in expected.items() if record["parent"] is not None])
        parent = expected[node_id]["parent"]
        doomed = {node_id} | _descendants(expected, node_id)
        store.delete(node_id, recursive=True)
        expected[parent]["children"].remove(node_id)
        for doomed_id in doomed:
            del expected[doomed_id]

    try:
        for step in range(72):
            # Bootstrap a broad tree before allowing destructive operations.
            if len(model) < 8 or step < 16:
                operation = "create"
            else:
                operation = rng.choices(
                    ("create", "update", "rename", "move", "delete"),
                    weights=(35, 24, 13, 18, 10),
                    k=1,
                )[0]

            # Draw all random choices once, then replay the same operation by
            # resetting the RNG state is unnecessarily error-prone.  Applying
            # the operation to both models independently is equivalent because
            # their node sets and ordering are kept identical.
            state_before = rng.getstate()
            if operation == "create":
                apply_create(memory, model, step)
                rng.setstate(state_before)
                apply_create(durable, durable_model, step)
            elif operation == "update":
                apply_update(memory, model, step)
                rng.setstate(state_before)
                apply_update(durable, durable_model, step)
            elif operation == "rename":
                apply_rename(memory, model, step)
                rng.setstate(state_before)
                apply_rename(durable, durable_model, step)
            elif operation == "move":
                apply_move(memory, model, step)
                rng.setstate(state_before)
                apply_move(durable, durable_model, step)
            else:
                apply_delete(memory, model)
                rng.setstate(state_before)
                apply_delete(durable, durable_model)

            if step % 8 == 0 or step == 71:
                _assert_model(memory, model, root_id)
                _assert_model(durable, durable_model, durable_root_id)
            if step == 35:
                durable.close()
                durable = SQLiteTreeStore(path)
                _assert_model(durable, durable_model, durable_root_id)
    finally:
        durable.close()

    with SQLiteTreeStore(path) as reopened:
        _assert_model(reopened, durable_model, durable_root_id)



def test_unicode_deep_and_broad_tree_roundtrip(tmp_path: Path) -> None:
    """Exercise unicode path components plus both hierarchy extremes."""
    path = tmp_path / "unicode-shape.sqlite"
    depth = 54
    breadth = 128
    deep_names: list[str] = []
    deep_ids: list[str] = []
    broad_ids: list[str] = []
    with SQLiteTreeStore(path) as db:
        parent: str = "/"
        for index in range(depth):
            name = f"{_UNICODE_NAMES[index % len(_UNICODE_NAMES)]}-{index}"
            node = db.create(
                name,
                parent=parent,
                node_id=f"deep-{index}",
                properties={
                    "label": _UNICODE_TEXT,
                    "combining": "e\u0301",
                    "reference": Reference("external-※"),
                },
            )
            deep_names.append(name)
            deep_ids.append(node.id)
            parent = node.id
        for index in range(breadth):
            name = f"{_UNICODE_NAMES[(index + 3) % len(_UNICODE_NAMES)]}-{index}"
            node = db.create(
                name,
                node_id=f"wide-{index}",
                properties={"index": index, "text": _UNICODE_TEXT},
            )
            broad_ids.append(node.id)

        deep_path = "/" + "/".join(deep_names)
        assert db.get(deep_path).id == deep_ids[-1]
        assert db.path(deep_ids[-1]) == deep_path
        assert db.get(deep_ids[-1]).properties["label"] == _UNICODE_TEXT
        assert db.root.children == (deep_ids[0], *broad_ids)

    with SQLiteTreeStore(path) as reopened:
        assert reopened.get(deep_path).id == deep_ids[-1]
        assert reopened.get(deep_ids[depth // 2]).properties["combining"] == "e\u0301"
        assert reopened.root.children == (deep_ids[0], *broad_ids)
        assert reopened.get("/" + deep_names[0]).children == (deep_ids[1],)


@pytest.mark.parametrize(
    "bad_path",
    (
        "/a/",
        "/a//b",
        "/a/./b",
        "/a/../b",
        "/a\x00b",
        "/a/b/",
        "/a//",
    ),
)
def test_invalid_paths_are_rejected_without_state_changes(tmp_path: Path, bad_path: str) -> None:
    """Reject malformed absolute paths, including NUL and empty components."""
    stores: list[TreeStore] = [TreeStore(), SQLiteTreeStore(tmp_path / "paths.sqlite")]
    try:
        for store in stores:
            node = store.create("a")
            store.create("b", parent=node.id)
            before = store.export_json()
            with pytest.raises(InvalidOperationError):
                store.get(bad_path)
            with pytest.raises(InvalidOperationError):
                store.path(bad_path)
            assert not store.exists(bad_path)
            assert store.export_json() == before
    finally:
        stores[1].close()


def test_non_absolute_and_missing_paths_never_alias_nodes() -> None:
    db = TreeStore()
    node = db.create("relative")
    # Names are paths only when rooted; bare strings are opaque node IDs.
    with pytest.raises(NotFoundError):
        db.get("relative")
    with pytest.raises(NotFoundError):
        db.get("relative/child")
    with pytest.raises(NotFoundError):
        db.get("/does-not-exist")
    assert db.get("/").children == (node.id,)


def _seed_sqlite_corruption_case(path: Path) -> None:
    with SQLiteTreeStore(path) as db:
        parent = db.create("parent")
        db.create("left", parent=parent.id)
        db.create("right", parent=parent.id)


def _mutate_node_type(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE nodes SET type = '' WHERE name = 'left'")


def _mutate_node_name(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE nodes SET name = 'bad/name' WHERE name = 'left'")


def _mutate_missing_parent(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE nodes SET parent_id = 'missing-parent' WHERE name = 'left'")


def _mutate_bad_timestamp(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE nodes SET modified_at = 'not-a-timestamp' WHERE name = 'left'")


def _mutate_empty_id(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE nodes SET id = '' WHERE name = 'left'")


def _mutate_duplicate_sibling_name(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE nodes SET name = 'right' WHERE name = 'left'")


def _mutate_detached_edge(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM children WHERE child_id = (SELECT id FROM nodes WHERE name = 'left')"
    )


def _mutate_orphan_edge(conn: sqlite3.Connection) -> None:
    # child_id is unique, so replace the valid edge with one whose parent is absent.
    conn.execute("DELETE FROM children WHERE child_id = (SELECT id FROM nodes WHERE name = 'left')")
    conn.execute(
        "INSERT INTO children(parent_id, child_id, position) "
        "VALUES ('missing-parent', (SELECT id FROM nodes WHERE name = 'left'), 0)"
    )


def _mutate_position_gap(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE children SET position = 3 "
        "WHERE child_id = (SELECT id FROM nodes WHERE name = 'right')"
    )


def _mutate_position_type(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE children SET position = 'not-an-integer' "
        "WHERE child_id = (SELECT id FROM nodes WHERE name = 'right')"
    )


def _mutate_bad_root_metadata(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE metadata SET root_id = 'missing-root' WHERE id = 1")


def _mutate_negative_revision(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE metadata SET revision = -1 WHERE id = 1")


def _mutate_missing_metadata(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM metadata")


_SCHEMA_MUTATORS: tuple[tuple[str, Callable[[sqlite3.Connection], None]], ...] = (
    ("empty-type", _mutate_node_type),
    ("slash-name", _mutate_node_name),
    ("missing-parent", _mutate_missing_parent),
    ("bad-timestamp", _mutate_bad_timestamp),
    ("empty-id", _mutate_empty_id),
    ("duplicate-sibling-name", _mutate_duplicate_sibling_name),
    ("detached-edge", _mutate_detached_edge),
    ("orphan-edge", _mutate_orphan_edge),
    ("position-gap", _mutate_position_gap),
    ("position-type", _mutate_position_type),
    ("bad-root-metadata", _mutate_bad_root_metadata),
    ("negative-revision", _mutate_negative_revision),
    ("missing-metadata", _mutate_missing_metadata),
)


@pytest.mark.parametrize("case,mutator", _SCHEMA_MUTATORS, ids=[x[0] for x in _SCHEMA_MUTATORS])
def test_malformed_sqlite_rows_fail_closed(
    tmp_path: Path, case: str, mutator: Callable[[sqlite3.Connection], None]
) -> None:
    path = tmp_path / f"malformed-{case}.sqlite"
    _seed_sqlite_corruption_case(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        mutator(conn)
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(path)


@pytest.mark.parametrize(
    "tag_value",
    (
        {"$grove": "unknown", "value": 1},
        {"$grove": "bytes", "base64": "not base64!"},
        {"$grove": "bytes", "base64": 123},
        {"$grove": "bytes", "base64": "YQ==", "extra": True},
        {"$grove": "timestamp", "value": "2020-01-01T00:00:00"},
        {"$grove": "timestamp", "value": 3},
        {"$grove": "reference", "id": "bad/id"},
        {"$grove": "reference", "id": ""},
        {"$grove": None, "id": "x"},
    ),
)
def test_malformed_property_tags_and_json_fail_closed(
    tmp_path: Path, tag_value: dict[str, Any]
) -> None:
    path = tmp_path / "malformed-tags.sqlite"
    with SQLiteTreeStore(path) as db:
        db.create("payload")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE nodes SET properties = ? WHERE name = 'payload'",
            (json.dumps({"payload": tag_value}, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(path)


@pytest.mark.parametrize("properties", ("not json", "[]", '{"payload": [1, {"$grove": "wat"}]}'))
def test_malformed_property_documents_fail_closed(tmp_path: Path, properties: str) -> None:
    path = tmp_path / "malformed-document.sqlite"
    with SQLiteTreeStore(path) as db:
        db.create("payload")
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE nodes SET properties = ? WHERE name = 'payload'", (properties,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(path)


def test_sqlite_rejects_partial_and_unrecognized_schema(tmp_path: Path) -> None:
    partial = tmp_path / "partial.sqlite"
    conn = sqlite3.connect(partial)
    conn.execute("CREATE TABLE nodes (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(partial)

    extra = tmp_path / "extra.sqlite"
    with SQLiteTreeStore(extra):
        pass
    conn = sqlite3.connect(extra)
    conn.execute("CREATE TABLE application_data (value TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(extra)

    wrong_columns = tmp_path / "wrong-columns.sqlite"
    conn = sqlite3.connect(wrong_columns)
    conn.executescript(
        """
        CREATE TABLE metadata (id INTEGER PRIMARY KEY, revision TEXT, root_id TEXT);
        CREATE TABLE nodes (id TEXT PRIMARY KEY);
        CREATE TABLE children (parent_id TEXT, child_id TEXT, position INTEGER);
        """
    )
    conn.commit()
    conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(wrong_columns)

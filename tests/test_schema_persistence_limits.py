"""Characterize schema persistence limits without changing the public API.

These tests are deliberately about the *current* contract: schemas are
process-local.  They should be revised alongside a future schema catalog or
migration implementation, rather than silently becoming compatibility claims.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove import (
    PersistentTreeStore,
    Schema,
    SchemaValidationError,
    SQLiteTreeStore,
    TreeStore,
)


def _person_schema() -> Schema:
    return Schema(
        {
            "person": {
                "properties": {"name": {"type": str, "required": True}},
                "allow_extra": False,
            }
        }
    )


@pytest.mark.parametrize("backend", ("log", "sqlite"))
def test_durable_reopen_does_not_recover_process_local_schema(
    tmp_path: Path, backend: str
) -> None:
    """Current files carry state, not the Schema passed to their writer."""
    path = tmp_path / ("tree.log" if backend == "log" else "tree.sqlite")
    factory = PersistentTreeStore if backend == "log" else SQLiteTreeStore

    with factory(path, schema=_person_schema()) as db:
        db.create("alice", type="person", properties={"name": "Alice"})

    # No schema is implicitly reconstructed at reopen.  This is intentional
    # current behavior, and is the limit a future durable catalog must change
    # explicitly rather than by interpreting legacy files.
    with factory(path) as reopened:
        reopened.update("/alice", type="unconstrained", properties={"other": 1})
        assert reopened.get("/alice").type == "unconstrained"


@pytest.mark.parametrize("backend", ("log", "sqlite"))
def test_matching_schema_must_be_supplied_again_on_reopen(
    tmp_path: Path, backend: str
) -> None:
    path = tmp_path / ("tree.log" if backend == "log" else "tree.sqlite")
    factory = PersistentTreeStore if backend == "log" else SQLiteTreeStore
    with factory(path) as db:
        db.create("alice", type="person", properties={"name": "Alice"})

    # Passing the schema validates all loaded records before the handle is
    # exposed, preserving strict compatibility when applications opt back in.
    with factory(path, schema=_person_schema()) as reopened:
        with pytest.raises(SchemaValidationError):
            reopened.update("/alice", properties={"name": 1})
        assert reopened.get("/alice").properties == {"name": "Alice"}


def test_schema_free_exports_have_no_schema_contract() -> None:
    source = TreeStore(schema=_person_schema())
    source.create("alice", type="person", properties={"name": "Alice"})
    exported = source.export("/")
    assert "schema" not in exported
    assert "schema_version" not in exported
    encoded = source.export_json("/")
    assert "schema_version" not in encoded
    assert json.loads(encoded) == exported


def test_schema_rejection_on_import_remains_atomic() -> None:
    source = TreeStore()
    node = source.create("alice", type="person", properties={"name": "Alice"})
    payload = source.export(node.id)
    payload["properties"] = {"name": 3}

    target = TreeStore(schema=_person_schema())
    before = target.export_json()
    with pytest.raises(SchemaValidationError):
        target.import_tree(payload)
    assert target.export_json() == before

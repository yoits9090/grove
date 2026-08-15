"""Opt-in schema validation tests."""
from pathlib import Path

import pytest

from grove import (Schema, SchemaValidationError, SQLiteTreeStore,
                   PersistentTreeStore, TreeStore)


def _schema():
    return Schema({
        "person": {
            "properties": {
                "name": {"type": str, "required": True},
                "age": int,
                "role": {"enum": ("admin", "user")},
            },
            "allow_extra": False,
        }
    })


def test_schema_validates_types_required_and_extra_properties():
    db = TreeStore(schema=_schema())
    person = db.create("alice", type="person", properties={"name": "Alice", "age": 4})
    assert person.type == "person"
    with pytest.raises(SchemaValidationError):
        db.create("wrong", type="person", properties={"name": 2})
    with pytest.raises(SchemaValidationError):
        db.create("missing", type="person", properties={"age": 2})
    with pytest.raises(SchemaValidationError):
        db.create("extra", type="person", properties={"name": "x", "unknown": 1})
    with pytest.raises(SchemaValidationError):
        db.update(person.id, properties={"name": "Alice", "role": "invalid"})
    assert db.get(person.id).properties == {"name": "Alice", "age": 4}


def test_schema_unknown_types_and_atomic_import():
    db = TreeStore(schema=_schema())
    with pytest.raises(SchemaValidationError):
        db.create("unknown", type="thing")
    source = TreeStore()
    exported = source.create("n", type="person", properties={"name": "x"})
    data = source.export(exported.id)
    data["properties"] = {"name": 10}
    before = db.export_json()
    with pytest.raises(SchemaValidationError):
        db.import_tree(data)
    assert db.export_json() == before


@pytest.mark.parametrize("store_factory", [
    lambda _: PersistentTreeStore(_),
    lambda _: SQLiteTreeStore(_),
])
def test_schema_applies_to_durable_backends(tmp_path: Path, store_factory):
    path = tmp_path / ("tree.log" if "Persistent" in repr(store_factory) else "tree.db")
    with store_factory(path) as db:
        db.set_schema(_schema())
        node = db.create("ok", type="person", properties={"name": "x"})
        with pytest.raises(SchemaValidationError):
            db.update(node.id, type="not-declared")
        assert db.get(node.id).type == "person"

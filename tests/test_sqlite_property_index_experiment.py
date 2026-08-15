"""Tests for the disposable durable SQLite scalar-index experiment."""
from pathlib import Path

import pytest

from grove import SQLiteTreeStore
from grove.sqlite_property_index_experiment import SQLiteScalarPropertyIndexExperiment


def test_scalar_index_matches_materialized_query_and_preserves_types(tmp_path: Path):
    db = SQLiteScalarPropertyIndexExperiment(tmp_path / "source.db")
    try:
        root = db.create("objects")
        one = db.create("one", parent=root.id, properties={"score": 1})
        truth = db.create("truth", parent=root.id, properties={"score": True})
        text = db.create("text", parent=root.id, properties={"score": "1"})
        nested = db.create("nested", parent=root.id, properties={"meta": {"score": 1}})
        db.create_scalar_index("score").create_scalar_index("meta.score")

        for value in (1, True, "1"):
            expected = db.query(root.id).where(score=value).ids()
            assert db.scalar_ids("score", value, root.id) == expected
        assert db.scalar_ids("score", 1, root.id) == (one.id,)
        assert db.scalar_ids("score", True, root.id) == (truth.id,)
        assert db.scalar_ids("score", "1", root.id) == (text.id,)
        assert db.scalar_ids("meta.score", 1, root.id) == (nested.id,)
        assert db.scalar_ids("score", None, root.id) == ()
    finally:
        db.close()


def test_index_is_durable_and_tracks_atomic_updates(tmp_path: Path):
    source = tmp_path / "source.db"
    db = SQLiteScalarPropertyIndexExperiment(source)
    one = db.create("one", properties={"value": "old"})
    db.create_scalar_index("value")
    assert db.scalar_ids("value", "old") == (one.id,)
    db.update(one.id, properties={"value": "new"})
    assert db.scalar_ids("value", "old") == ()
    assert db.scalar_ids("value", "new") == (one.id,)
    db.close()

    reopened = SQLiteScalarPropertyIndexExperiment(source)
    try:
        assert reopened.indexed_properties == ("value",)
        assert reopened.scalar_ids("value", "new") == (one.id,)
    finally:
        reopened.close()


def test_sidecar_rebuilds_after_another_source_handle_commits(tmp_path: Path):
    source = tmp_path / "source.db"
    indexed = SQLiteScalarPropertyIndexExperiment(source)
    try:
        node = indexed.create("node", properties={"value": 1})
        indexed.create_scalar_index("value")
        other = SQLiteTreeStore(source)
        try:
            other.update(node.id, properties={"value": 2})
        finally:
            other.close()
        assert indexed.scalar_ids("value", 1) == ()
        assert indexed.scalar_ids("value", 2) == (node.id,)
    finally:
        indexed.close()


def test_tree_and_index_rollback_together_on_index_failure(tmp_path: Path, monkeypatch):
    db = SQLiteScalarPropertyIndexExperiment(tmp_path / "source.db")
    try:
        node = db.create("node", properties={"value": "before"})
        db.create_scalar_index("value")
        original = db._rebuild_rows
        def fail(state):
            original(state)
            raise RuntimeError("injected index failure")
        monkeypatch.setattr(db, "_rebuild_rows", fail)
        with pytest.raises(RuntimeError):
            db.update(node.id, properties={"value": "after"})
        assert db.get(node.id).properties["value"] == "before"
        assert db.scalar_ids("value", "before") == (node.id,)
        assert db.scalar_ids("value", "after") == ()
    finally:
        db.close()


def test_non_scalar_values_are_left_to_materialized_queries(tmp_path: Path):
    db = SQLiteScalarPropertyIndexExperiment(tmp_path / "source.db")
    try:
        node = db.create("node", properties={"value": [1, 2]})
        db.create_scalar_index("value")
        assert db.query().where(value=[1, 2]).ids() == (node.id,)
        with pytest.raises(TypeError):
            db.scalar_ids("value", [1, 2])
    finally:
        db.close()


def test_direct_scalar_query_avoids_full_state_and_preserves_tree_order(tmp_path: Path, monkeypatch):
    db = SQLiteScalarPropertyIndexExperiment(tmp_path / "direct.db")
    try:
        root = db.create("root")
        first = db.create("first", parent=root.id, properties={"score": 7})
        nested = db.create("nested", parent=first.id, properties={"score": 7})
        second = db.create("second", parent=root.id, properties={"score": 7})
        db.create("miss", parent=root.id, properties={"score": 8})
        db.create_scalar_index("score")
        def refuse_materialization(*args, **kwargs):
            raise AssertionError("direct lookup materialized the complete tree")
        monkeypatch.setattr(db, "_read_state_from_connection", refuse_materialization)
        query = db.lookup_scalar("score", 7, root.id)
        assert query.ids() == (first.id, nested.id, second.id)
        assert not hasattr(query, "_state")
        assert query.first().properties == {"score": 7}
    finally:
        db.close()


def test_direct_scalar_query_scope_and_predicate_match_query(tmp_path: Path):
    db = SQLiteScalarPropertyIndexExperiment(tmp_path / "scope.db")
    try:
        root = db.create("root")
        parent = db.create("parent", parent=root.id, properties={"score": 1, "kind": "yes"})
        child = db.create("child", parent=parent.id, properties={"score": 1, "kind": "no"})
        db.create_scalar_index("score")
        for options in ({}, {"recursive": False}, {"include_root": True},
                        {"recursive": False, "include_root": True}):
            expected = db.query(root.id, **options).where(score=1).ids()
            actual = db.lookup_scalar("score", 1, root.id, **options).where(kind="yes").ids()
            expected_extra = db.query(root.id, **options).where(score=1).where(kind="yes").ids()
            assert actual == expected_extra
            if options.get("recursive", True):
                assert expected == (parent.id, child.id)
            else:
                assert expected == (parent.id,)
        assert db.lookup_scalar("score", 1, root.id, recursive=False).ids() == (parent.id,)
    finally:
        db.close()


def test_direct_scalar_query_nonroot_target_include_root_matches_query(tmp_path: Path):
    """A scoped SQL traversal must validate records against the database root.

    This catches the distinction between the CTE traversal anchor and the
    singleton root: passing the former to root-name validation rejects every
    non-root scoped lookup (including a matching target with ``include_root``).
    """
    db = SQLiteScalarPropertyIndexExperiment(tmp_path / "nonroot-scope.db")
    try:
        outer = db.create("outer", properties={"score": 1})
        child = db.create("child", parent=outer.id, properties={"score": 1})
        db.create("miss", parent=outer.id, properties={"score": 2})
        db.create_scalar_index("score")
        for options in ({}, {"recursive": False}, {"include_root": True},
                        {"recursive": False, "include_root": True}):
            expected = db.query(outer.id, **options).where(score=1).ids()
            actual = db.lookup_scalar("score", 1, outer.id, **options).ids()
            assert actual == expected
        assert db.lookup_scalar("score", 1, child.id, include_root=True).ids() == (child.id,)
    finally:
        db.close()

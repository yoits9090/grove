"""Adversarial characterization tests for the disposable direct scalar index.

These tests intentionally exercise the private experiment rather than GROVE's
public index API.  In particular, sidecar metadata is treated as a revision
validator, not as a cryptographic integrity proof: same-revision raw tampering
is documented as a known limitation below.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from grove import Reference, SQLiteTreeStore, StorageCorruptionError
from grove.sqlite_property_index_experiment import (
    SQLiteScalarPropertyIndexExperiment,
)


_ALIASED_VALUES = "property_values"


def _sidecar(path: Path) -> Path:
    return Path(str(path) + ".property-index.sqlite3")


def _indexed(path: Path) -> tuple[SQLiteScalarPropertyIndexExperiment, str]:
    db = SQLiteScalarPropertyIndexExperiment(path)
    node = db.create("node", properties={"value": "old"})
    db.create_scalar_index("value")
    return db, node.id


def test_sidecar_malformed_file_fails_closed(tmp_path: Path):
    path = tmp_path / "malformed.db"
    db, _ = _indexed(path)
    db.close()
    _sidecar(path).write_bytes(b"this is not a SQLite database")

    with pytest.raises(StorageCorruptionError, match="property-index"):
        SQLiteScalarPropertyIndexExperiment(path)


def test_sidecar_extra_table_fails_closed(tmp_path: Path):
    path = tmp_path / "extra-table.db"
    db, _ = _indexed(path)
    db.close()
    sidecar = _sidecar(path)
    with sqlite3.connect(sidecar) as raw:
        raw.execute("CREATE TABLE unrelated (payload TEXT)")
        raw.commit()

    with pytest.raises(StorageCorruptionError, match="schema"):
        SQLiteScalarPropertyIndexExperiment(path)


@pytest.mark.parametrize("mutation", ("identity", "revision_text"))
def test_sidecar_metadata_invalid_values_fail_closed(tmp_path: Path, mutation: str):
    path = tmp_path / f"bad-metadata-{mutation}.db"
    db, _ = _indexed(path)
    db.close()
    with sqlite3.connect(_sidecar(path)) as raw:
        if mutation == "identity":
            raw.execute(
                "UPDATE index_metadata SET source_identity = 'another-source' WHERE id = 1"
            )
        else:
            raw.execute(
                "UPDATE index_metadata SET source_revision = 'not-an-integer' WHERE id = 1"
            )
        raw.commit()

    with pytest.raises(StorageCorruptionError, match="property-index"):
        SQLiteScalarPropertyIndexExperiment(path)


def test_dangling_sidecar_candidate_is_reported_on_lookup(tmp_path: Path):
    path = tmp_path / "dangling.db"
    db, node_id = _indexed(path)
    db.close()
    with sqlite3.connect(_sidecar(path)) as raw:
        raw.execute(
            f"INSERT INTO {_ALIASED_VALUES} "
            "(property_name, value_type, value, node_id) VALUES (?, ?, ?, ?)",
            ("value", "str", b"ghost", "missing-node"),
        )
        raw.commit()

    with SQLiteScalarPropertyIndexExperiment(path) as reopened:
        # The valid candidate remains usable; asking for the dangling key must
        # not silently return a partial result.
        assert reopened.scalar_ids("value", "old") == (node_id,)
        with pytest.raises(StorageCorruptionError, match="missing source node"):
            reopened.scalar_ids("value", "ghost")


def test_stale_sidecar_revision_rebuilds_after_create_and_delete(tmp_path: Path):
    path = tmp_path / "stale-rebuild.db"
    indexed, old_id = _indexed(path)
    try:
        other = SQLiteTreeStore(path)
        try:
            other.delete(old_id)
            new = other.create("new", properties={"value": "new"})
            new_id = new.id
        finally:
            other.close()

        assert indexed.scalar_ids("value", "old") == ()
        assert indexed.scalar_ids("value", "new") == (new_id,)
        assert indexed.indexed_properties == ("value",)
    finally:
        indexed.close()


def test_stale_sidecar_metadata_is_rebuilt_on_reopen(tmp_path: Path):
    path = tmp_path / "stale-metadata.db"
    db, node_id = _indexed(path)
    db.close()
    with sqlite3.connect(_sidecar(path)) as raw:
        # Deliberately claim an older source revision while retaining the
        # registration.  Reopen must rebuild rows from the source snapshot.
        raw.execute("UPDATE index_metadata SET source_revision = 0 WHERE id = 1")
        raw.execute(f"DELETE FROM {_ALIASED_VALUES}")
        raw.commit()

    with SQLiteScalarPropertyIndexExperiment(path) as reopened:
        assert reopened.scalar_ids("value", "old") == (node_id,)


def test_same_revision_sidecar_tamper_is_a_known_integrity_limit(tmp_path: Path):
    """A matching revision cannot detect raw sidecar deletion (characterize it).

    This is intentionally a characterization rather than a desired guarantee:
    the experiment stores no sidecar checksum.  The report records this as a
    reason not to promote the direct path to the public API yet.
    """
    path = tmp_path / "same-revision-tamper.db"
    db, node_id = _indexed(path)
    try:
        with sqlite3.connect(_sidecar(path)) as raw:
            raw.execute(f"DELETE FROM {_ALIASED_VALUES} WHERE node_id = ?", (node_id,))
            raw.commit()
        # Source revision and sidecar metadata still agree, so no rebuild is
        # attempted and this raw corruption manifests as a false negative.
        assert db.scalar_ids("value", "old") == ()
    finally:
        db.close()


_TYPED_VALUES = (
    ("none", None),
    ("false", False),
    ("true", True),
    ("integer", 1),
    ("large-integer", 2**100),
    ("float", 1.5),
    ("negative-zero", -0.0),
    ("text", "1"),
    ("bytes", b"1\\x00"),
    ("datetime", dt.datetime(2020, 1, 2, 3, 4, tzinfo=dt.timezone.utc)),
    ("reference", Reference("external")),
)


def test_direct_lookup_preserves_scalar_types_and_exact_values(tmp_path: Path):
    path = tmp_path / "typed.db"
    db = SQLiteScalarPropertyIndexExperiment(path)
    try:
        root = db.create("objects")
        nodes = {
            label: db.create(label, parent=root.id, properties={"value": value})
            for label, value in _TYPED_VALUES
        }
        # A missing property and a nested non-scalar value must never become
        # candidates for an exact scalar lookup.
        db.create("missing", parent=root.id)
        db.create("list", parent=root.id, properties={"value": [1, 2]})
        db.create_scalar_index("value")

        for label, value in _TYPED_VALUES:
            expected = db.query(root.id).where(value=value).ids()
            assert expected == (nodes[label].id,)
            assert db.scalar_ids("value", value, root.id) == expected

        # A datetime with a different offset denotes the same instant and is
        # normalized to the UTC sidecar key.
        equivalent = dt.datetime(
            2020, 1, 2, 5, 4, tzinfo=dt.timezone(dt.timedelta(hours=2))
        )
        assert db.scalar_ids("value", equivalent, root.id) == (
            nodes["datetime"].id,
        )
        assert db.scalar_ids("value", 1, root.id) != db.scalar_ids(
            "value", True, root.id
        )
        assert db.scalar_ids("value", 1, root.id) != db.scalar_ids(
            "value", 1.0, root.id
        )
        with pytest.raises(TypeError):
            db.scalar_ids("value", [1, 2], root.id)
        with pytest.raises(TypeError):
            db.scalar_ids("value", float("nan"), root.id)
    finally:
        db.close()


def test_direct_lookup_scope_path_node_and_predicate_match_materialized_query(
    tmp_path: Path,
):
    path = tmp_path / "scopes.db"
    db = SQLiteScalarPropertyIndexExperiment(path)
    try:
        outer = db.create("outer")
        first = db.create(
            "first", parent=outer.id, type="match", properties={"value": 7, "kind": "yes"}
        )
        nested = db.create(
            "nested", parent=first.id, type="match", properties={"value": 7, "kind": "no"}
        )
        second = db.create(
            "second", parent=outer.id, type="other", properties={"value": 7, "kind": "yes"}
        )
        db.create("outside", properties={"value": 7, "kind": "yes"})
        db.create_scalar_index("value")

        for target in ("/outer", outer, outer.id):
            for options in (
                {},
                {"recursive": False},
                {"include_root": True},
                {"recursive": False, "include_root": True},
            ):
                expected = db.query(target, **options).where(value=7).where(kind="yes").ids()
                actual = (
                    db.lookup_scalar("value", 7, target, **options)
                    .where(kind="yes")
                    .ids()
                )
                assert actual == expected

        assert db.lookup_scalar("value", 7, "/outer").by_type("match").ids() == (
            first.id,
            nested.id,
        )
        assert db.lookup_scalar("value", 7, outer.id).where(
            lambda node: node.name.endswith("d")
        ).ids() == (nested.id, second.id)
        assert db.lookup_scalar("value", 7, "/outer", recursive=False).ids() == (
            first.id,
            second.id,
        )
    finally:
        db.close()


_WORKER = r"""
import os
import signal
import sys

from grove.sqlite_property_index_experiment import SQLiteScalarPropertyIndexExperiment


def kill_now():
    os.kill(os.getpid(), signal.SIGKILL)


class CrashConnection:
    def __init__(self, connection, checkpoint):
        self.connection = connection
        self.checkpoint = checkpoint
        self.commit_armed = False

    def execute(self, sql, *parameters):
        result = self.connection.execute(sql, *parameters)
        normalized = " ".join(sql.split()).upper()
        if (
            self.checkpoint == "after_sidecar_delete"
            and normalized.startswith(
                "DELETE FROM GROVE_PROPERTY_INDEX_EXPERIMENT.PROPERTY_VALUES"
            )
        ):
            kill_now()
        if normalized.startswith("UPDATE METADATA SET REVISION ="):
            if self.checkpoint == "after_source_revision":
                kill_now()
            if self.checkpoint == "after_commit":
                self.commit_armed = True
        return result

    def commit(self):
        result = self.connection.commit()
        if self.checkpoint == "after_commit" and self.commit_armed:
            kill_now()
        return result

    def __getattr__(self, name):
        return getattr(self.connection, name)


path, checkpoint = sys.argv[1:]
db = SQLiteScalarPropertyIndexExperiment(path)
db._conn = CrashConnection(db._conn, checkpoint)
db.update("/node", properties={"value": "new"})
"""


def _run_killed_worker(path: Path, checkpoint: str) -> None:
    repository = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repository) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    result = subprocess.run(
        [sys.executable, "-c", _WORKER, str(path), checkpoint],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == -signal.SIGKILL, (
        f"worker did not receive SIGKILL (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _assert_scalar_state(path: Path, expected: str, node_id: str) -> None:
    with SQLiteScalarPropertyIndexExperiment(path) as db:
        assert db.scalar_ids("value", expected) == (node_id,)
        other = "new" if expected == "old" else "old"
        assert db.scalar_ids("value", other) == ()


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash tests require POSIX")
@pytest.mark.parametrize("checkpoint", ("after_sidecar_delete", "after_source_revision"))
def test_killed_direct_index_update_reopens_previous_coherent_snapshot(
    tmp_path: Path, checkpoint: str
):
    path = tmp_path / f"before-{checkpoint}.db"
    db, node_id = _indexed(path)
    db.close()
    _run_killed_worker(path, checkpoint)
    _assert_scalar_state(path, "old", node_id)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash tests require POSIX")
def test_killed_direct_index_update_after_commit_reopens_new_snapshot(tmp_path: Path):
    path = tmp_path / "after-commit.db"
    db, node_id = _indexed(path)
    db.close()
    _run_killed_worker(path, "after_commit")
    _assert_scalar_state(path, "new", node_id)

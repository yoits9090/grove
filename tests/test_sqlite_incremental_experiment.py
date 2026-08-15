"""Differential and crash coverage for the opt-in dirty-row SQLite experiment."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from grove import SQLiteTreeStore
from grove.sqlite_incremental_experiment import SQLiteIncrementalTreeStore


def _shape(store):
    def walk(node):
        return {
            "name": node.name,
            "type": node.type,
            "properties": node.properties,
            "children": [walk(store.get(child)) for child in node.children],
        }
    return walk(store.root)


def test_incremental_matches_full_rewrite_and_preserves_order(tmp_path):
    full = SQLiteTreeStore(tmp_path / "full.db")
    dirty = SQLiteIncrementalTreeStore(tmp_path / "dirty.db")
    try:
        def create(store, name, parent="/", **kw):
            return store.create(name, parent=parent, **kw)

        for store in (full, dirty):
            create(store, "a", node_id="a", properties={"v": 1})
            create(store, "b", node_id="b", properties={"v": 2})
            create(store, "c", node_id="c", properties={"v": 3})
            create(store, "leaf", parent="a", node_id="leaf")
        assert _shape(full) == _shape(dirty)

        operations = [
            lambda s: s.move("c", "/", index=0),
            lambda s: s.rename("a", "renamed"),
            lambda s: s.update("leaf", properties={"v": 99, "tags": ["x"]}),
            lambda s: s.move("leaf", "b", name="moved", index=0),
            lambda s: s.delete("/renamed", recursive=True),
            lambda s: s.create("tail", node_id="tail", index=0),
        ]
        for operation in operations:
            operation(full)
            operation(dirty)
            assert _shape(full) == _shape(dirty)
        dirty.close()
        dirty = SQLiteIncrementalTreeStore(tmp_path / "dirty.db")
        assert _shape(full) == _shape(dirty)
    finally:
        full.close()
        dirty.close()


def test_incremental_leaf_update_touches_only_dirty_node_and_metadata(tmp_path):
    full = SQLiteTreeStore(tmp_path / "full.db")
    dirty = SQLiteIncrementalTreeStore(tmp_path / "dirty.db")
    try:
        for store in (full, dirty):
            with store.transaction() as tx:
                for i in range(100):
                    tx.create(f"node-{i}", node_id=f"node-{i}")
        full_before = full._conn.total_changes
        dirty_before = dirty._conn.total_changes
        full.update("node-50", properties={"changed": True})
        dirty.update("node-50", properties={"changed": True})
        full_writes = full._conn.total_changes - full_before
        dirty_writes = dirty._conn.total_changes - dirty_before
        assert dirty_writes == 2  # one node row + metadata row
        assert full_writes > dirty_writes
        assert dirty.last_commit_stats == {
            "node_inserts": 0, "node_updates": 1, "node_deletes": 0,
            "node_detaches": 0, "edge_inserts": 0, "edge_deletes": 0, "rows_written": 1,
            "old_nodes": 101, "new_nodes": 101,
            "old_edges": 100, "new_edges": 100,
        }
    finally:
        full.close()
        dirty.close()


_WORKER = r'''
import os
import signal
import sys
from grove.sqlite_incremental_experiment import SQLiteIncrementalTreeStore

def kill_now():
    os.kill(os.getpid(), signal.SIGKILL)

class CrashConnection:
    def __init__(self, connection, checkpoint):
        self.connection = connection
        self.checkpoint = checkpoint
        self.kill_after_commit = False
    def execute(self, sql, *parameters):
        result = self.connection.execute(sql, *parameters)
        normalized = " ".join(sql.split()).upper()
        if self.checkpoint == "after_edge_delete" and normalized.startswith("DELETE FROM CHILDREN WHERE"):
            kill_now()
        if self.checkpoint == "after_node_update" and normalized.startswith("UPDATE NODES SET"):
            kill_now()
        if normalized.startswith("UPDATE METADATA SET REVISION ="):
            if self.checkpoint == "after_metadata_update":
                kill_now()
            if self.checkpoint == "after_sql_commit":
                self.kill_after_commit = True
        return result
    def commit(self):
        result = self.connection.commit()
        if self.checkpoint == "after_sql_commit" and self.kill_after_commit:
            kill_now()
        return result
    def __getattr__(self, name):
        return getattr(self.connection, name)

path, checkpoint = sys.argv[1:]
db = SQLiteIncrementalTreeStore(path)
db._conn = CrashConnection(db._conn, checkpoint)
tx = db.transaction()
tx.update("/stable", properties={"value": "new"})
tx.delete("/stable/existing")
tx.create("pending", properties={"value": "new"})
tx.commit()
'''


def _make_baseline(path: Path):
    with SQLiteIncrementalTreeStore(path) as db:
        stable = db.create("stable", properties={"value": "old"})
        db.create("existing", parent=stable.id, properties={"value": "old"})


def _run_worker(path: Path, checkpoint: str):
    env = os.environ.copy()
    repository = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(repository) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run(
        [sys.executable, "-c", _WORKER, str(path), checkpoint],
        cwd=repository, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == -signal.SIGKILL, (result.stdout, result.stderr)


def _assert_old(path):
    with SQLiteTreeStore(path) as db:
        assert db.get("/stable").properties == {"value": "old"}
        assert db.get("/stable/existing").properties == {"value": "old"}
        assert not db.exists("/pending")


def _assert_new(path):
    with SQLiteTreeStore(path) as db:
        assert db.get("/stable").properties == {"value": "new"}
        assert not db.exists("/stable/existing")
        assert db.get("/pending").properties == {"value": "new"}


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash tests require POSIX")
@pytest.mark.parametrize("checkpoint", ("after_edge_delete", "after_node_update", "after_metadata_update"))
def test_incremental_kill_before_commit_reopens_old_snapshot(tmp_path, checkpoint):
    path = tmp_path / f"before-{checkpoint}.db"
    _make_baseline(path)
    _run_worker(path, checkpoint)
    _assert_old(path)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash tests require POSIX")
def test_incremental_kill_after_sql_commit_reopens_new_snapshot(tmp_path):
    path = tmp_path / "after-commit.db"
    _make_baseline(path)
    _run_worker(path, "after_sql_commit")
    _assert_new(path)

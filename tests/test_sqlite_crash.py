"""Crash/reopen coverage for SQLiteTreeStore's all-or-nothing commits.

The worker is deliberately a separate interpreter.  SIGKILL is sent by the
worker to itself at SQL commit checkpoints, so a test failure cannot terminate
pytest (or leave a Python finally block to accidentally turn a torn commit into
an orderly rollback).  Every checkpoint is reached by a direct call/return,
not by a sleep or a race with another process.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from grove import SQLiteTreeStore


# A process cannot run Python cleanup after SIGKILL.  That is the property this
# test needs: SQLite must recover the open transaction itself on the next open.
_WORKER = r"""
import os
import signal
import sys

from grove import SQLiteTreeStore


def kill_now():
    # The child owns this PID; the pytest process is never signalled.
    os.kill(os.getpid(), signal.SIGKILL)


class CrashConnection:
    # Delegate to sqlite while killing after selected durable-boundary calls.

    def __init__(self, connection, checkpoint):
        self.connection = connection
        self.checkpoint = checkpoint
        self.kill_after_commit = False

    def execute(self, sql, *parameters):
        result = self.connection.execute(sql, *parameters)
        normalized = " ".join(sql.split()).upper()
        if (
            self.checkpoint == "after_children_delete"
            and normalized == "DELETE FROM CHILDREN"
        ):
            kill_now()
        if normalized.startswith("UPDATE METADATA SET REVISION ="):
            if self.checkpoint == "after_metadata_update":
                kill_now()
            if self.checkpoint == "after_sql_commit":
                self.kill_after_commit = True
        return result

    def commit(self):
        # This is after sqlite has returned from COMMIT.  With synchronous=FULL,
        # reopening must therefore observe this transaction as committed.
        result = self.connection.commit()
        if self.checkpoint == "after_sql_commit" and self.kill_after_commit:
            kill_now()
        return result

    def __getattr__(self, name):
        return getattr(self.connection, name)


path = sys.argv[1]
checkpoint = sys.argv[2]
db = SQLiteTreeStore(path)
db._conn = CrashConnection(db._conn, checkpoint)

if checkpoint == "after_write_state":
    original_write_state = db._write_state

    def write_state_then_kill(state):
        original_write_state(state)
        kill_now()

    db._write_state = write_state_then_kill

transaction = db.transaction()
transaction.update("/stable", properties={"value": "new"})
transaction.delete("/stable/existing")
transaction.create("pending", properties={"value": "new"})
transaction.commit()
"""


def _run_killed_worker(path: Path, checkpoint: str) -> None:
    # -c runs exactly the same installed interpreter as pytest.  Explicitly
    # prepend the repository so this remains reliable without an editable
    # install when tests are run from another working directory.
    repository = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    old_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repository) + (
        os.pathsep + old_pythonpath if old_pythonpath else ""
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
        f"crash worker did not receive SIGKILL (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _make_baseline(path: Path) -> None:
    with SQLiteTreeStore(path) as db:
        stable = db.create("stable", properties={"value": "old"})
        db.create("existing", parent=stable.id, properties={"value": "old"})


def _assert_baseline(path: Path) -> None:
    with SQLiteTreeStore(path) as db:
        assert db.get("/stable").properties == {"value": "old"}
        assert db.get("/stable/existing").properties == {"value": "old"}
        assert not db.exists("/pending")
        assert db.get("/").children == (db.get("/stable").id,)


def _assert_committed(path: Path) -> None:
    with SQLiteTreeStore(path) as db:
        assert db.get("/stable").properties == {"value": "new"}
        assert not db.exists("/stable/existing")
        assert db.get("/pending").properties == {"value": "new"}
        assert db.get("/").children == (db.get("/stable").id, db.get("/pending").id)


@pytest.mark.parametrize(
    "checkpoint",
    ("after_children_delete", "after_write_state", "after_metadata_update"),
)
@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash tests require POSIX")
def test_sqlite_killed_before_commit_reopens_previous_snapshot(tmp_path, checkpoint):
    """A kill during any uncommitted rewrite must not expose partial rows."""
    path = tmp_path / f"before-{checkpoint}.db"
    _make_baseline(path)

    _run_killed_worker(path, checkpoint)
    _assert_baseline(path)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash tests require POSIX")
def test_sqlite_killed_after_sql_commit_reopens_committed_snapshot(tmp_path):
    """A kill after COMMIT returns must preserve the complete new snapshot."""
    path = tmp_path / "after-commit.db"
    _make_baseline(path)

    _run_killed_worker(path, "after_sql_commit")
    _assert_committed(path)

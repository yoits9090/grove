"""Isolated SQLite logical-history prototype (not part of GROVE's public API).

The prototype deliberately treats each SQLite online-backup result as an
immutable artifact.  It does not alter SQLiteTreeStore's commit path or schema.
Run ``python experiments/sqlite_history.py`` for a tiny demonstration.
"""
from __future__ import annotations

import copy
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grove import SQLiteTreeStore, TreeStore


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A named, immutable-on-disk logical revision.

    ``open()`` returns a detached in-memory ``TreeStore``.  The returned store
    is intentionally mutable, but mutating it cannot alter this artifact.
    """

    revision: int
    path: Path

    def open(self) -> TreeStore:
        # Loading through the existing validating SQLite adapter makes this
        # experiment reuse all format/invariant checks.  Return a detached
        # in-memory store so consumers cannot accidentally write the artifact.
        source = SQLiteTreeStore(self.path)
        try:
            state: dict[str, Any] = copy.deepcopy(source._state)
        finally:
            source.close()
        if source._version != self.revision:
            raise RuntimeError("snapshot filename/revision mismatch")
        return TreeStore(state=state)


class SQLiteHistory:
    """Capture and enumerate SQLiteTreeStore revisions without core changes.

    This uses ``sqlite3.Connection.backup`` under a source read transaction.
    The source transaction pins one WAL snapshot while ``backup`` copies it;
    the destination is published by rename only after its metadata revision is
    checked.  It is a prototype: retention, encryption, and quota management
    are intentionally out of scope.
    """

    def __init__(self, store: SQLiteTreeStore, directory: str | os.PathLike[str]):
        self.store = store
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def capture(self) -> Snapshot:
        store = self.store
        # SQLiteTreeStore's lock protects its single connection.  The private
        # connection access is intentional here: this file is an experiment,
        # not a proposed public API implementation.
        with store._lock:
            store._ensure_open()
            source = store._conn
            source.execute("BEGIN")
            temporary: Path | None = None
            try:
                row = source.execute(
                    "SELECT revision, root_id FROM metadata WHERE id = 1"
                ).fetchone()
                if row is None or not isinstance(row[0], int) or row[0] < 0:
                    raise RuntimeError("GROVE metadata revision is unavailable")
                revision, root_id = row
                final = self.directory / f"snapshot-{revision:020d}.db"
                if final.exists():
                    # Capture is idempotent for a revision.  Validate the
                    # existing artifact before returning it.
                    check = SQLiteTreeStore(final)
                    try:
                        if check._version != revision or check.root.id != root_id:
                            raise RuntimeError("existing snapshot has wrong revision/root")
                    finally:
                        check.close()
                    source.commit()
                    return Snapshot(revision, final)

                temporary = self.directory / (
                    f".{final.name}.{uuid.uuid4().hex}.tmp"
                )
                destination = sqlite3.connect(temporary, isolation_level=None)
                try:
                    destination.execute("PRAGMA synchronous = FULL")
                    # The active source read transaction pins the copied view.
                    source.backup(destination, name="main")
                    copied = destination.execute(
                        "SELECT revision, root_id FROM metadata WHERE id = 1"
                    ).fetchone()
                    if copied != (revision, root_id):
                        raise RuntimeError("backup changed revision during capture")
                    destination.commit()
                finally:
                    destination.close()
                os.replace(temporary, final)
                temporary = None
                # Persist the directory entry where supported.  This is best
                # effort because Windows and some virtual filesystems do not
                # expose fsync-able directory handles.
                try:
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
                source.commit()
                return Snapshot(revision, final)
            except Exception:
                try:
                    source.rollback()
                except sqlite3.Error:
                    pass
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                raise

    def revisions(self) -> tuple[int, ...]:
        revisions = []
        for path in self.directory.glob("snapshot-*.db"):
            try:
                revision = int(path.stem.removeprefix("snapshot-"))
            except ValueError:
                continue
            revisions.append(revision)
        return tuple(sorted(set(revisions)))

    def snapshot(self, revision: int) -> Snapshot:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        path = self.directory / f"snapshot-{revision:020d}.db"
        if not path.exists():
            raise KeyError(revision)
        # Constructor validation catches a stale/corrupt artifact and confirms
        # that the filename really denotes the requested logical revision.
        check = SQLiteTreeStore(path)
        try:
            if check._version != revision:
                raise RuntimeError("snapshot filename/revision mismatch")
        finally:
            check.close()
        return Snapshot(revision, path)


def demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        db = SQLiteTreeStore(Path(directory) / "live.db")
        history = SQLiteHistory(db, Path(directory) / "history")
        db.create("before")
        first = history.capture()
        db.create("after")
        second = history.capture()
        print(first.revision, first.open().exists("/before"), first.open().exists("/after"))
        print(second.revision, second.open().exists("/before"), second.open().exists("/after"))
        db.close()


if __name__ == "__main__":
    demo()

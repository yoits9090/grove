"""Small durable logical-history API for :class:`SQLiteTreeStore`.

History artifacts are complete SQLite online-backup copies.  They are named by
GROVE's durable metadata revision and published with a same-directory rename
only after the copy has been validated.  This module deliberately leaves the
live SQLite schema and commit path unchanged.

The API is intentionally conservative: snapshots are full copies (not
incremental versions), retention and encryption are caller responsibilities,
and an artifact is immutable by convention rather than protected from an
external process opening the SQLite file directly.
"""
from __future__ import annotations

import copy
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import StorageCorruptionError
from .sqlite_store import SQLiteTreeStore
from .store import TreeStore


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A named logical revision stored as an immutable-on-disk artifact.

    ``open()`` returns a detached in-memory :class:`~grove.TreeStore`.  The
    returned store may be changed freely; such changes never write this
    artifact.
    """

    revision: int
    path: Path

    def open(self) -> TreeStore:
        """Validate and open this revision as a detached ``TreeStore``.

        Loading through ``SQLiteTreeStore`` reuses the adapter's schema and
        invariant checks.  The public export/import API then detaches the
        resulting state, avoiding a writable handle to the artifact.
        """
        source = SQLiteTreeStore(self.path)
        try:
            revision, root_id = _metadata(self.path)
            if revision != self.revision:
                raise RuntimeError("snapshot filename/revision mismatch")
            exported = source.export("/")
            if exported.get("id") != root_id:
                raise StorageCorruptionError(
                    "snapshot metadata root does not match exported tree"
                )
        finally:
            source.close()

        detached = TreeStore()
        # A complete root export is accepted by the public import API and
        # preserves IDs/timestamps when preserve_ids=True.
        detached.import_tree(copy.deepcopy(exported), preserve_ids=True)
        return detached


def _metadata(path: Path) -> tuple[int, str]:
    """Read and validate the singleton metadata row from an artifact."""
    try:
        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                "SELECT revision, root_id FROM metadata WHERE id = 1"
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise StorageCorruptionError(
            f"invalid SQLite history artifact: {exc}"
        ) from exc
    if len(rows) != 1:
        raise StorageCorruptionError("SQLite history metadata row is missing")
    revision, root_id = rows[0]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(root_id, str)
        or not root_id
    ):
        raise StorageCorruptionError("invalid SQLite history metadata")
    return revision, root_id


class SQLiteHistory:
    """Capture and enumerate ``SQLiteTreeStore`` logical revisions.

    ``capture()`` uses the store's public online-backup operation.  A backup is
    first written to a uniquely named sibling, validated by reopening it, and
    then published as ``snapshot-<revision>.db`` with ``os.replace``.  Existing
    artifacts for a revision are validated and returned unchanged, making
    capture idempotent.

    This API does not alter the source database or its schema.  It is local to
    one history directory and does not provide retention, encryption, remote
    replication, rollback, or multi-database atomicity.
    """

    def __init__(
        self,
        store: SQLiteTreeStore,
        directory: str | os.PathLike[str],
    ) -> None:
        if not isinstance(store, SQLiteTreeStore):
            raise TypeError("SQLiteHistory requires a SQLiteTreeStore")
        self.store = store
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _name(revision: int) -> str:
        return f"snapshot-{revision:020d}.db"

    def _validate_artifact(self, path: Path, expected: int | None = None) -> Snapshot:
        # Constructor validation checks all SQLite rows and tree invariants;
        # metadata additionally confirms the artifact's logical revision/root.
        check = SQLiteTreeStore(path)
        try:
            revision, root_id = _metadata(path)
            if check.root.id != root_id:
                raise StorageCorruptionError(
                    "snapshot metadata root does not match tree root"
                )
        finally:
            check.close()
        if expected is not None and revision != expected:
            raise RuntimeError("snapshot filename/revision mismatch")
        return Snapshot(revision, path)

    def capture(self) -> Snapshot:
        """Capture the source's current committed revision atomically."""
        # ``backup`` performs source locking, pins one read snapshot, validates
        # it, and publishes the supplied path atomically.  We add a second
        # publication step because the revision is part of the destination
        # filename and cannot be known before the copy.
        temporary = self.directory / f".{uuid.uuid4().hex}.capture.db"
        try:
            self.store.backup(temporary)
            captured = self._validate_artifact(temporary)
            revision = captured.revision
            final = self.directory / self._name(revision)
            if final.exists():
                # Preserve the first artifact for a revision.  This also
                # prevents silently accepting an externally damaged history.
                existing = self._validate_artifact(final, expected=revision)
                temporary.unlink()
                return existing
            os.replace(temporary, final)
            temporary = None
            _fsync_directory(self.directory)
            return Snapshot(revision, final)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def revisions(self) -> tuple[int, ...]:
        """Return sorted logical revisions represented by snapshot filenames."""
        revisions: set[int] = set()
        for path in self.directory.glob("snapshot-*.db"):
            if not path.is_file():
                continue
            try:
                revisions.add(int(path.stem.removeprefix("snapshot-")))
            except ValueError:
                continue
        return tuple(sorted(revisions))

    def snapshot(self, revision: int) -> Snapshot:
        """Return and validate a stored revision, or raise ``KeyError``."""
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("revision must be a non-negative integer")
        path = self.directory / self._name(revision)
        if not path.exists():
            raise KeyError(revision)
        return self._validate_artifact(path, expected=revision)


def _fsync_directory(directory: Path) -> None:
    """Best-effort durability for the directory rename."""
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Windows and some virtual filesystems do not expose fsync-able
        # directory handles.  The SQLite backup itself remains durable.
        pass


__all__ = ["Snapshot", "SQLiteHistory"]

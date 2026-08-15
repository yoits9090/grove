"""SQLite-backed GROVE tree store.

The public tree and transaction operations are implemented by :class:`TreeStore`;
this module supplies the durable state and commit layer using SQLite.  A complete
state is represented relationally by ``nodes`` and ``children``.  The latter is
an ordered edge table rather than a JSON column, so SQLite's foreign-key checks
also protect the durable hierarchy.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import time
import math
from pathlib import Path
from typing import Any

from .errors import InvalidOperationError, InvalidPropertyError, StorageCorruptionError
from .store import (
    TreeStore,
    Transaction,
    _check_invariants,
    _clone_properties,
    _decode_value,
    _encode_value,
    _new_state,
    _validate_id,
    _validate_name,
    _validate_timestamp,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL,
    root_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    properties TEXT NOT NULL,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES nodes(id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS children (
    parent_id TEXT NOT NULL,
    child_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    UNIQUE (parent_id, position),
    FOREIGN KEY (parent_id) REFERENCES nodes(id) ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (child_id) REFERENCES nodes(id) ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED
);
"""


class SQLiteTreeStore(TreeStore):
    """A :class:`TreeStore` persisted in a SQLite database.

    Transactions retain the same detached, optimistic API as ``TreeStore``.
    Every transaction starts from a fresh SQLite snapshot and commits by taking
    ``BEGIN IMMEDIATE`` and checking the durable metadata revision.  Thus two
    store instances sharing a database cannot silently overwrite one another.

    ``:memory:`` is supported for convenience.  File-backed databases use WAL,
    ``foreign_keys=ON``, and ``synchronous=FULL`` on this store's connection.
    """

    def __init__(self, path: str | os.PathLike[str], *, schema=None,
                 timeout: float = 30.0, write_retries: int = 2,
                 retry_delay: float = 0.01):
        """Open a GROVE SQLite database.

        ``timeout`` is the per-attempt SQLite lock timeout in seconds.  A
        writer may make at most ``write_retries`` additional attempts after a
        busy/locked failure, with exponential backoff beginning at
        ``retry_delay`` seconds.  The policy is deliberately bounded: callers
        receive :class:`InvalidOperationError` rather than waiting forever for
        another process.  Reads are not retried because WAL readers do not
        need a writer lock.
        """
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str):
            raise TypeError("database path must be a string or path-like object")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite non-negative number")
        if isinstance(write_retries, bool) or not isinstance(write_retries, int) or write_retries < 0:
            raise ValueError("write_retries must be a non-negative integer")
        if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)) or not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError("retry_delay must be a finite non-negative number")

        self.path_on_disk = Path(raw_path) if raw_path != ":memory:" else None
        self._memory = raw_path == ":memory:"
        self._closed = False
        self._db_path = raw_path
        self._db_uri = raw_path.startswith("file:")
        self._timeout = float(timeout)
        self._write_retries = write_retries
        self._retry_delay = float(retry_delay)
        if not self._memory and not self._db_uri:
            self.path_on_disk.parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None keeps transaction boundaries explicit.  A single
        # connection is guarded by _lock; separate store instances use SQLite's
        # normal file locking and WAL reader/writer concurrency.
        self._conn = sqlite3.connect(
            raw_path,
            timeout=self._timeout,
            isolation_level=None,
            check_same_thread=False,
            uri=self._db_uri,
        )
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                f"PRAGMA busy_timeout = {int(self._timeout * 1000)}"
            )
            if not self._memory:
                # journal_mode is persistent for a file database.  Setting it
                # during construction (before any transaction) is important:
                # a torn process does not leave a partially applied snapshot.
                try:
                    self._conn.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError as exc:
                    if self._is_busy(exc):
                        raise InvalidOperationError(
                            "SQLite writer lock unavailable while enabling WAL"
                        ) from exc
                    raise
            self._conn.execute("PRAGMA synchronous = FULL")
            existing_tables = {row[0] for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            required = {"metadata", "nodes", "children"}
            # A path is owned by this adapter. Do not turn an arbitrary empty
            # SQLite database (or a partially initialized one) into a GROVE
            # database, which could silently hide unrelated durable data.
            user_tables = {name for name in existing_tables if not name.startswith("sqlite_")}
            if user_tables and user_tables != required:
                raise StorageCorruptionError("unrecognized or incomplete GROVE SQLite schema")
            self._conn.executescript(_SCHEMA)
            state, revision = self._initialize_or_load()
        except sqlite3.DatabaseError as exc:
            self._conn.close()
            raise StorageCorruptionError(f"invalid SQLite database: {exc}") from exc
        except Exception:
            self._conn.close()
            raise

        # super() validates and deep-copies the state, and creates the normal
        # subscriptions and re-entrant lock used by TreeStore.
        super().__init__(state=state, schema=schema)
        self._version = revision
        # SQLite's data_version changes when another connection commits.  Keep
        # it alongside the durable revision so the read fast path still
        # notices out-of-band changes (including changes that do not update
        # GROVE metadata) made by another handle.
        self._data_version = self._conn.execute(
            "PRAGMA data_version"
        ).fetchone()[0]

    # -- SQLite setup and state decoding ---------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise InvalidOperationError("store is closed")

    @staticmethod
    def _is_busy(exc: sqlite3.OperationalError) -> bool:
        """Return whether an operational error is a transient writer lock."""
        code = getattr(exc, "sqlite_errorcode", None)
        # SQLITE_BUSY and SQLITE_LOCKED (including extended variants).
        if isinstance(code, int) and (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        message = str(exc).lower()
        return "database is locked" in message or "database table is locked" in message

    def _begin_immediate(self) -> None:
        """Acquire SQLite's writer lock under a finite, explicit retry policy."""
        for attempt in range(self._write_retries + 1):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if not self._is_busy(exc):
                    raise
                if attempt >= self._write_retries:
                    raise InvalidOperationError(
                        "SQLite writer lock unavailable after "
                        f"{attempt + 1} attempt(s)"
                    ) from exc
                delay = self._retry_delay * (2 ** attempt)
                if delay:
                    time.sleep(delay)

    def _initialize_or_load(self) -> tuple[dict[str, Any], int]:
        """Create the initial root, or atomically load an existing database."""
        self._begin_immediate()
        try:
            metadata = self._conn.execute(
                "SELECT revision, root_id FROM metadata WHERE id = 1"
            ).fetchone()
            metadata_count = self._conn.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
            node_count = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            child_count = self._conn.execute("SELECT COUNT(*) FROM children").fetchone()[0]
            if metadata_count > 1:
                raise StorageCorruptionError("duplicate SQLite metadata rows")
            if metadata is None:
                # A truly new DB has no rows. Refuse any partial population;
                # this avoids silently replacing acknowledged/corrupt data.
                if metadata_count or node_count or child_count:
                    raise StorageCorruptionError(
                        "metadata is missing from a non-empty SQLite database"
                    )
                state = _new_state()
                root = state["nodes"][state["root_id"]]
                self._conn.execute(
                    """INSERT INTO nodes
                       (id, name, type, properties, parent_id, created_at, modified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        root["id"],
                        root["name"],
                        root["type"],
                        self._properties_json(root["properties"]),
                        root["parent_id"],
                        root["created_at"],
                        root["modified_at"],
                    ),
                )
                self._conn.execute(
                    "INSERT INTO metadata (id, revision, root_id) VALUES (1, 0, ?)",
                    (state["root_id"],),
                )
                self._conn.commit()
                return state, 0

            state, revision = self._read_state_from_connection(metadata)
            if state["root_id"] != metadata[1]:
                raise StorageCorruptionError("metadata root does not match tree root")
            # Extra rows are not part of this format. The singleton metadata
            # constraint handles metadata, while state validation handles all
            # node/edge consistency.
            self._conn.commit()
            return state, revision
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _properties_json(properties: dict[str, Any]) -> str:
        # All values have already been validated by the TreeStore operation;
        # encoding through the common codec keeps the SQLite format compatible
        # with export/import JSON.
        return json.dumps(
            _encode_value(properties),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _read_state_from_connection(
        self, metadata: tuple[Any, Any] | None = None
    ) -> tuple[dict[str, Any], int]:
        """Read and validate state while the caller owns a SQLite snapshot."""
        if metadata is None:
            metadata = self._conn.execute(
                "SELECT revision, root_id FROM metadata WHERE id = 1"
            ).fetchone()
        if metadata is None:
            raise StorageCorruptionError("SQLite metadata row is missing")

        revision, root_id = metadata
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise StorageCorruptionError("invalid SQLite metadata revision")
        try:
            _validate_id(root_id)
        except InvalidOperationError as exc:
            raise StorageCorruptionError("invalid SQLite root ID") from exc

        rows = self._conn.execute(
            """SELECT id, name, type, properties, parent_id, created_at, modified_at
               FROM nodes"""
        ).fetchall()
        nodes: dict[str, dict[str, Any]] = {}
        try:
            for node_id, name, typ, properties, parent_id, created_at, modified_at in rows:
                _validate_id(node_id)
                _validate_name(name, root=node_id == root_id)
                if not isinstance(typ, str) or not typ:
                    raise InvalidOperationError("invalid node type")
                if not isinstance(properties, str):
                    raise InvalidOperationError("invalid node properties encoding")
                decoded = _decode_value(json.loads(properties))
                if not isinstance(decoded, dict):
                    raise InvalidPropertyError("node properties must be a mapping")
                props = _clone_properties(decoded)
                if parent_id is not None:
                    _validate_id(parent_id)
                _validate_timestamp(created_at)
                _validate_timestamp(modified_at)
                if node_id in nodes:
                    raise InvalidOperationError("duplicate node ID")
                nodes[node_id] = {
                    "id": node_id,
                    "name": name,
                    "type": typ,
                    "properties": props,
                    "parent_id": parent_id,
                    "children": [],
                    "created_at": created_at,
                    "modified_at": modified_at,
                }

            child_rows = self._conn.execute(
                "SELECT parent_id, child_id, position FROM children ORDER BY parent_id, position"
            ).fetchall()
            seen_positions: set[tuple[str, int]] = set()
            seen_children: set[str] = set()
            for parent_id, child_id, position in child_rows:
                if parent_id not in nodes or child_id not in nodes:
                    raise InvalidOperationError("child edge references missing node")
                if not isinstance(position, int) or position < 0:
                    raise InvalidOperationError("invalid child position")
                key = (parent_id, position)
                if key in seen_positions or child_id in seen_children:
                    raise InvalidOperationError("duplicate child edge")
                # Explicit ordinals must form a dense zero-based list.  Gaps
                # would otherwise be silently normalized on reopen.
                if position != len(nodes[parent_id]["children"]):
                    raise InvalidOperationError("non-contiguous child position")
                seen_positions.add(key)
                seen_children.add(child_id)
                nodes[parent_id]["children"].append(child_id)

            state = {"root_id": root_id, "nodes": nodes}
            _check_invariants(state)
            return state, revision
        except StorageCorruptionError:
            raise
        except (TypeError, ValueError, KeyError, json.JSONDecodeError,
                InvalidOperationError, InvalidPropertyError) as exc:
            raise StorageCorruptionError(f"invalid SQLite tree state: {exc}") from exc

    def _read_snapshot(self) -> tuple[dict[str, Any], int]:
        """Read a coherent state/revision pair in one SQLite read transaction."""
        self._ensure_open()
        try:
            self._conn.execute("BEGIN")
            result = self._read_state_from_connection()
            self._conn.commit()
            return result
        except sqlite3.DatabaseError as exc:
            try: self._conn.rollback()
            except sqlite3.DatabaseError: pass
            raise StorageCorruptionError(f"invalid SQLite database during read: {exc}") from exc
        except Exception:
            try: self._conn.rollback()
            except sqlite3.DatabaseError: pass
            raise

    def _refresh(self) -> None:
        # Reading every public operation makes a long-lived instance observe a
        # commit made by another instance, rather than serving stale state.
        #
        # Most reads happen without a concurrent writer.  In that common case
        # the metadata revision is a cheap cache validator: avoid materializing
        # and validating every node when this instance already has that
        # revision.  A changed (or malformed) revision falls through to the
        # original coherent snapshot path, preserving cross-instance refresh
        # and corruption checks.
        with self._lock:
            self._ensure_open()
            data_version = self._conn.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            metadata = self._conn.execute(
                "SELECT revision, root_id FROM metadata WHERE id = 1"
            ).fetchone()
            # Close the small validation window: a different connection may
            # commit between the first data_version read and metadata query.
            data_version_after = self._conn.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            if (
                data_version == self._data_version
                and data_version_after == data_version
                and metadata is not None
            ):
                revision, root_id = metadata
                if (
                    isinstance(revision, int)
                    and not isinstance(revision, bool)
                    and revision >= 0
                    and root_id == self._state["root_id"]
                    and revision == self._version
                ):
                    return
            state, revision = self._read_snapshot()
            self._state = state
            self._version = revision
            self._data_version = self._conn.execute(
                "PRAGMA data_version"
            ).fetchone()[0]

    # -- TreeStore API with durable snapshots ----------------------------

    def transaction(self) -> Transaction:
        with self._lock:
            state, revision = self._read_snapshot()
            self._state = state
            self._version = revision
            self._data_version = self._conn.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            return Transaction(self, revision, copy.deepcopy(state), self._schema)

    def get(self, target):
        self._refresh()
        return super().get(target)

    read = get

    def exists(self, target) -> bool:
        self._refresh()
        return super().exists(target)

    def path(self, target) -> str:
        self._refresh()
        return super().path(target)

    def export(self, target="/"):
        self._refresh()
        return super().export(target)

    def backup(self, destination: str | os.PathLike[str] | sqlite3.Connection) -> None:
        """Create a consistent SQLite online backup.

        ``destination`` may be a filesystem path or an open
        :class:`sqlite3.Connection`.  Path backups are published with an
        atomic rename after SQLite has completed the copy; an existing target
        is replaced.  The source is held in a read transaction for the copy,
        so concurrent commits are excluded from the backup's view.  A backup
        never closes a caller-provided connection and fails explicitly when
        this store is closed or the destination aliases the source database.
        """
        with self._lock:
            self._ensure_open()
            destination_path: Path | None = None
            temporary: Path | None = None
            destination_conn: sqlite3.Connection | None = None
            owns_connection = False
            if isinstance(destination, sqlite3.Connection):
                if destination is self._conn:
                    raise InvalidOperationError("backup destination cannot be the source connection")
                if destination.in_transaction:
                    raise InvalidOperationError(
                        "backup destination connection has an active transaction"
                    )
                destination_conn = destination
            else:
                try:
                    raw_destination = os.fspath(destination)
                except TypeError as exc:
                    raise TypeError("backup destination must be a path or sqlite connection") from exc
                if not isinstance(raw_destination, str) or raw_destination == ":memory:":
                    raise ValueError("backup destination must be a filesystem path")
                destination_path = Path(raw_destination)
                source_path = self.path_on_disk
                try:
                    if source_path is not None and destination_path.resolve() == source_path.resolve():
                        raise InvalidOperationError("backup destination cannot be the source database")
                except OSError:
                    # If canonicalization fails, SQLite will still report a
                    # useful error; do not silently overwrite the source.
                    if source_path is not None and os.path.abspath(destination_path) == os.path.abspath(source_path):
                        raise InvalidOperationError("backup destination cannot be the source database")
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination_path.name}.", suffix=".tmp",
                    dir=destination_path.parent,
                )
                os.close(fd)
                temporary = Path(temporary_name)
                destination_conn = sqlite3.connect(temporary, isolation_level=None)
                owns_connection = True
            try:
                # Pin a coherent source snapshot.  Validate it before and
                # after copying so malformed/out-of-band rows never become a
                # backup artifact that appears successful.
                self._conn.execute("BEGIN")
                _state, revision = self._read_state_from_connection()
                self._conn.backup(destination_conn, name="main")
                copied = destination_conn.execute(
                    "SELECT revision, root_id FROM metadata WHERE id = 1"
                ).fetchone()
                if copied != (revision, _state["root_id"]):
                    raise StorageCorruptionError("SQLite backup changed revision during capture")
                destination_conn.commit()
                self._conn.commit()
                if destination_path is not None:
                    os.replace(temporary, destination_path)
                    temporary = None
                    try:
                        directory_fd = os.open(destination_path.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                    except OSError:
                        pass
            except sqlite3.DatabaseError as exc:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                try:
                    destination_conn.rollback()
                except sqlite3.Error:
                    pass
                raise InvalidOperationError(f"SQLite backup failed: {exc}") from exc
            except Exception:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                try:
                    destination_conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                if owns_connection and destination_conn is not None:
                    destination_conn.close()
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

    def export_json(self, target="/", *, indent=2):
        self._refresh()
        return super().export_json(target, indent=indent)

    def subscribe(self, callback, node=None, *, recursive=True):
        self._refresh()
        return super().subscribe(callback, node, recursive=recursive)

    def _query_state_snapshot(self):
        self._refresh()
        return super()._query_state_snapshot()

    # -- Atomic optimistic commit ----------------------------------------

    def _write_state(self, state: dict[str, Any]) -> None:
        """Replace relational rows inside the caller's open SQL transaction."""
        old_ids = {
            row[0] for row in self._conn.execute("SELECT id FROM nodes").fetchall()
        }
        new_ids = set(state["nodes"])

        # Edges are derived from the node records, so rebuilding this small
        # ordered table first avoids transient duplicate positions.  The outer
        # SQL transaction means readers never observe the intermediate state.
        self._conn.execute("DELETE FROM children")

        # Null parent pointers of removed rows before deleting them.  Parent
        # references are deferred, but this also keeps this routine valid if a
        # database was created with an older immediate-FK schema.
        removed_ids = old_ids - new_ids
        for node_id in removed_ids:
            self._conn.execute("UPDATE nodes SET parent_id = NULL WHERE id = ?", (node_id,))
        for node_id in removed_ids:
            self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

        for node_id, record in state["nodes"].items():
            values = (
                record["id"],
                record["name"],
                record["type"],
                self._properties_json(record["properties"]),
                record["parent_id"],
                record["created_at"],
                record["modified_at"],
            )
            if node_id in old_ids:
                self._conn.execute(
                    """UPDATE nodes SET id = ?, name = ?, type = ?, properties = ?,
                       parent_id = ?, created_at = ?, modified_at = ? WHERE id = ?""",
                    values + (node_id,),
                )
            else:
                self._conn.execute(
                    """INSERT INTO nodes
                       (id, name, type, properties, parent_id, created_at, modified_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )

        for parent_id, record in state["nodes"].items():
            for position, child_id in enumerate(record["children"]):
                self._conn.execute(
                    "INSERT INTO children (parent_id, child_id, position) VALUES (?, ?, ?)",
                    (parent_id, child_id, position),
                )

    def _commit(self, tx: Transaction) -> None:
        self._ensure_open()
        with self._lock:
            self._begin_immediate()
            try:
                metadata = self._conn.execute(
                    "SELECT revision, root_id FROM metadata WHERE id = 1"
                ).fetchone()
                if metadata is None:
                    raise StorageCorruptionError("SQLite metadata row is missing")
                current_revision, current_root = metadata
                if (
                    isinstance(current_revision, bool)
                    or not isinstance(current_revision, int)
                    or current_revision < 0
                ):
                    raise StorageCorruptionError("invalid SQLite metadata revision")
                if current_root != tx._state["root_id"]:
                    raise StorageCorruptionError("SQLite metadata root does not match transaction")
                if current_revision != tx._base_version:
                    self._conn.rollback()
                    # Keep this instance useful after a conflict.  The failed
                    # transaction itself remains closed by the caller's normal
                    # context-manager flow, matching TreeStore semantics.
                    state, revision = self._read_snapshot()
                    self._state, self._version = state, revision
                    raise InvalidOperationError(
                        "transaction conflict: store changed since transaction began"
                    )

                _check_invariants(tx._state)
                for _record in tx._state["nodes"].values():
                    self._schema.validate(_record["type"], _record["properties"],
                                          node_name=_record["name"])
                old_state = self._state
                self._write_state(tx._state)
                new_revision = current_revision + 1
                self._conn.execute(
                    "UPDATE metadata SET revision = ?, root_id = ? WHERE id = 1",
                    (new_revision, tx._state["root_id"]),
                )
                self._conn.commit()
                self._state = tx._state
                self._version = new_revision
                self._data_version = self._conn.execute(
                    "PRAGMA data_version"
                ).fetchone()[0]
                subscriptions = tuple(self._subscriptions)
            except sqlite3.IntegrityError as exc:
                try: self._conn.rollback()
                except sqlite3.Error: pass
                raise InvalidOperationError(f"SQLite constraint failure: {exc}") from exc
            except sqlite3.OperationalError as exc:
                try: self._conn.rollback()
                except sqlite3.Error: pass
                raise InvalidOperationError(f"SQLite transaction failure: {exc}") from exc
            except Exception:
                # The conflict branch has already rolled back; rollback is
                # harmless and makes all other failures fail closed.
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        for change in tx._changes:
            for sub in subscriptions:
                if sub._matches(change, old_state, self._state):
                    try:
                        sub._callback(change)
                    except Exception:
                        pass

    def close(self) -> None:
        if not self._closed:
            with self._lock:
                if not self._closed:
                    self._conn.close()
                    self._closed = True

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *args):
        self.close()
        return False

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

    def __init__(self, path: str | os.PathLike[str]):
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str):
            raise TypeError("database path must be a string or path-like object")

        self.path_on_disk = Path(raw_path) if raw_path != ":memory:" else None
        self._memory = raw_path == ":memory:"
        self._closed = False
        self._db_path = raw_path
        self._db_uri = raw_path.startswith("file:")
        if not self._memory and not self._db_uri:
            self.path_on_disk.parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None keeps transaction boundaries explicit.  A single
        # connection is guarded by _lock; separate store instances use SQLite's
        # normal file locking and WAL reader/writer concurrency.
        self._conn = sqlite3.connect(
            raw_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
            uri=self._db_uri,
        )
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 30000")
            if not self._memory:
                # journal_mode is persistent for a file database.  Setting it
                # during construction (before any transaction) is important:
                # a torn process does not leave a partially applied snapshot.
                self._conn.execute("PRAGMA journal_mode = WAL")
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
        super().__init__(state=state)
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

    def _initialize_or_load(self) -> tuple[dict[str, Any], int]:
        """Create the initial root, or atomically load an existing database."""
        self._conn.execute("BEGIN IMMEDIATE")
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
        self._conn.execute("BEGIN")
        try:
            result = self._read_state_from_connection()
            self._conn.commit()
            return result
        except Exception:
            self._conn.rollback()
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
            return Transaction(self, revision, copy.deepcopy(state))

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
            self._conn.execute("BEGIN IMMEDIATE")
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

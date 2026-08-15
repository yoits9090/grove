"""Disposable experiment: a durable SQLite scalar property index.

This module is deliberately *not* part of GROVE's public API.  It subclasses
:class:`~grove.sqlite_store.SQLiteTreeStore` only to attach a sidecar SQLite
database to the same SQLite transaction.  Registered scalar property indexes
are rebuilt inside the source store's commit transaction; a failure therefore
rolls back both the tree and the sidecar index.

The adapter is an experiment, not an optimized replacement for ``PropertyIndex``.
Lookup reads one coherent source/sidecar snapshot, narrows candidates through a
SQLite B-tree, and uses an ordered SQL CTE to decode only matching node rows.
The private direct query facade preserves detached Node views, typed equality,
and primary-tree order without full-tree materialization.  It is intentionally
kept out of the public API until broader predicate semantics and workloads
justify the added storage/query complexity.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import InvalidOperationError, InvalidPropertyError, NotFoundError, StorageCorruptionError
from .model import Node
from .query import Query, _MISSING, _index_key, _predicate_from, _property_value
from .sqlite_store import SQLiteTreeStore
from .store import (_clone_properties, _decode_value, _record_to_node,
                    _validate_id, _validate_name, _validate_timestamp,
                    _path_parts)
from .types import Reference

# Keep SQL identifiers static and separate from the source database schema.
_ALIAS = "grove_property_index_experiment"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_ALIAS}.index_metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    source_identity TEXT NOT NULL,
    source_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS {_ALIAS}.indexed_properties (
    property_name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS {_ALIAS}.property_values (
    property_name TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value BLOB NOT NULL,
    node_id TEXT NOT NULL,
    PRIMARY KEY (property_name, value_type, value, node_id)
);
CREATE INDEX IF NOT EXISTS {_ALIAS}.property_values_lookup
    ON property_values(property_name, value_type, value);
"""
_REQUIRED_TABLES = {"index_metadata", "indexed_properties", "property_values"}


def _scalar_key(value: Any) -> tuple[str, bytes] | None:
    """Encode a GROVE scalar as a type-tagged SQLite key.

    The type tag is essential: SQLite's JSON extraction and Python equality
    both alias values such as ``True`` and ``1``.  Integer text avoids SQLite's
    signed-64-bit limit, while float ``hex`` is exact and deterministic.
    """
    if value is None:
        return "none", b""
    if isinstance(value, bool):
        return "bool", (b"1" if value else b"0")
    if isinstance(value, int):
        return "int", str(value).encode("ascii")
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value == 0.0:
            value = 0.0  # GROVE equality treats -0.0 and 0.0 alike.
        return "float", value.hex().encode("ascii")
    if isinstance(value, str):
        return "str", value.encode("utf-8")
    if isinstance(value, bytes):
        return "bytes", value
    if isinstance(value, _dt.datetime):
        # Stored properties are timezone-aware.  Naive lookup values simply
        # produce no candidate, matching the absence of any valid stored value.
        if value.tzinfo is None:
            return "datetime", value.isoformat().encode("utf-8")
        normalized = value.astimezone(_dt.timezone.utc)
        return "datetime", normalized.isoformat().encode("utf-8")
    if isinstance(value, Reference):
        return "reference", value.node_id.encode("utf-8")
    # Lists and maps are intentionally not scalar-indexed by this experiment.
    return None


def _property_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("indexed property name must be a non-empty string")
    if "\x00" in name:
        raise ValueError("indexed property name cannot contain NUL")
    return name


class _DirectScalarQuery(Iterable[Node]):
    """Small query facade over candidate records decoded by the direct path.

    Unlike :class:`Query`, this object deliberately has no ``_state`` map:
    records are loaded only for matching scalar candidates.  It mirrors the
    read-only result methods used by the experiment and keeps scope changes
    and additional predicates deterministic.
    """

    def __init__(self, records: list[dict[str, Any]], *, recursive: bool,
                 include_root: bool, predicate: Callable[[Node], bool] | None):
        self._records = tuple(records)
        self._recursive = recursive
        self._include_root = include_root
        self._predicate = predicate

    def _copy(self, **changes: Any) -> "_DirectScalarQuery":
        options = {
            "recursive": self._recursive,
            "include_root": self._include_root,
            "predicate": self._predicate,
        }
        options.update(changes)
        return _DirectScalarQuery(list(self._records), **options)

    def descendants(self, *, recursive: bool = True,
                    include_root: bool = False) -> "_DirectScalarQuery":
        if not isinstance(recursive, bool) or not isinstance(include_root, bool):
            raise TypeError("recursive and include_root must be bools")
        return self._copy(recursive=recursive, include_root=include_root)

    def where(self, predicate: Callable[[Node], bool] | Mapping[str, Any] | None = None,
              **criteria: Any) -> "_DirectScalarQuery":
        if predicate is None and not criteria:
            return self
        new = _predicate_from(predicate) if predicate is not None else None
        if criteria:
            criterion = _predicate_from(criteria)
            assert criterion is not None
            if new is None:
                new = criterion
            else:
                old = new
                new = lambda node: bool(old(node)) and bool(criterion(node))
        assert new is not None
        if self._predicate is None:
            combined = new
        else:
            old = self._predicate
            combined = lambda node: bool(old(node)) and bool(new(node))
        return self._copy(predicate=combined)

    filter = where

    def by_type(self, node_type: str) -> "_DirectScalarQuery":
        return self.where(type=node_type)

    def __iter__(self) -> Iterator[Node]:
        for record in self._records:
            depth = record["_depth"]
            if not self._include_root and depth == 0:
                continue
            if not self._recursive and ((self._include_root and depth > 1) or
                                        (not self._include_root and depth > 1)):
                continue
            node = _record_to_node(record)
            if self._predicate is None or bool(self._predicate(node)):
                yield node

    def all(self) -> list[Node]:
        return list(self)

    def first(self, default: Node | None = None) -> Node | None:
        return next(iter(self), default)

    def count(self) -> int:
        return sum(1 for _ in self)

    def ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self)

    def __len__(self) -> int:
        return self.count()

    def __bool__(self) -> bool:
        return self.first() is not None

    def __repr__(self) -> str:
        return (f"DirectScalarQuery(recursive={self._recursive!r}, "
                f"include_root={self._include_root!r})")


class SQLiteScalarPropertyIndexExperiment(SQLiteTreeStore):
    """Opt-in SQLite sidecar index experiment.

    ``index_path`` defaults to ``<source>.property-index.sqlite3`` for a
    file-backed source and to an in-memory sidecar for ``:memory:``.  The
    sidecar is not accepted by or exposed through the core ``SQLiteTreeStore``
    schema.  Do not use this class as a migration; it exists to gather evidence
    before considering a production design.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        index_path: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(path)
        self.index_path_on_disk: Path | None = None
        if index_path is None:
            if self.path_on_disk is None:
                raw_index_path = ":memory:"
            else:
                self.index_path_on_disk = Path(
                    str(self.path_on_disk) + ".property-index.sqlite3"
                )
                self.index_path_on_disk.parent.mkdir(parents=True, exist_ok=True)
                raw_index_path = os.fspath(self.index_path_on_disk)
        else:
            raw_index_path = os.fspath(index_path)
            if raw_index_path != ":memory:":
                self.index_path_on_disk = Path(raw_index_path)
                self.index_path_on_disk.parent.mkdir(parents=True, exist_ok=True)

        if self.path_on_disk is not None and raw_index_path != ":memory:":
            try:
                if Path(raw_index_path).resolve() == self.path_on_disk.resolve():
                    raise ValueError("property index sidecar must differ from source database")
            except OSError:
                pass

        self._index_ready = False
        try:
            self._conn.execute("ATTACH DATABASE ? AS " + _ALIAS, (raw_index_path,))
            self._validate_and_create_schema()
            self._index_ready = True
            self._synchronize_index()
        except sqlite3.DatabaseError as exc:
            self.close()
            raise StorageCorruptionError(f"invalid property-index SQLite database: {exc}") from exc
        except Exception:
            self.close()
            raise

    def _validate_and_create_schema(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute(
                f"SELECT name FROM {_ALIAS}.sqlite_master WHERE type='table'"
            ).fetchall()
            if not row[0].startswith("sqlite_")
        }
        if tables and tables != _REQUIRED_TABLES:
            raise StorageCorruptionError("unrecognized or incomplete property-index schema")
        self._conn.executescript(_SCHEMA)

    @property
    def indexed_properties(self) -> tuple[str, ...]:
        self._ensure_index_current()
        with self._lock:
            return tuple(
                row[0]
                for row in self._conn.execute(
                    f"SELECT property_name FROM {_ALIAS}.indexed_properties ORDER BY property_name"
                )
            )

    def _source_identity(self) -> str:
        if self.path_on_disk is not None:
            return str(self.path_on_disk.resolve())
        return self._db_path

    def _metadata(self) -> tuple[str, int] | None:
        row = self._conn.execute(
            f"SELECT source_identity, source_revision FROM {_ALIAS}.index_metadata WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        identity, revision = row
        if not isinstance(identity, str) or not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise StorageCorruptionError("invalid property-index metadata")
        return identity, revision

    def _indexed_names(self) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                f"SELECT property_name FROM {_ALIAS}.indexed_properties ORDER BY property_name"
            ).fetchall()
        ]

    def _rebuild_rows(self, state: dict[str, Any]) -> None:
        """Rebuild all registered values in the caller's open transaction."""
        self._conn.execute(f"DELETE FROM {_ALIAS}.property_values")
        rows: list[tuple[str, str, bytes, str]] = []
        for name in self._indexed_names():
            for node_id, record in state["nodes"].items():
                value = _property_value(record["properties"], name)
                if value is _MISSING:
                    continue
                key = _scalar_key(value)
                if key is not None:
                    rows.append((name, key[0], key[1], node_id))
        if rows:
            self._conn.executemany(
                f"""INSERT INTO {_ALIAS}.property_values
                    (property_name, value_type, value, node_id)
                    VALUES (?, ?, ?, ?)""",
                rows,
            )

    def _set_metadata(self, revision: int) -> None:
        identity = self._source_identity()
        self._conn.execute(
            f"""INSERT INTO {_ALIAS}.index_metadata
                 (id, source_identity, source_revision) VALUES (1, ?, ?)
                 ON CONFLICT(id) DO UPDATE SET source_identity=excluded.source_identity,
                 source_revision=excluded.source_revision""",
            (identity, revision),
        )

    def _synchronize_index(self) -> None:
        """Initialize or catch up the sidecar to the durable source revision."""
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                source_row = self._conn.execute(
                    "SELECT revision, root_id FROM metadata WHERE id=1"
                ).fetchone()
                if source_row is None or not isinstance(source_row[0], int) or isinstance(source_row[0], bool) or source_row[0] < 0:
                    raise StorageCorruptionError("invalid source SQLite metadata")
                source_revision = source_row[0]
                metadata = self._metadata()
                if metadata is not None and metadata[0] != self._source_identity():
                    raise StorageCorruptionError("property-index belongs to another source database")
                state = None
                if metadata is None:
                    # A partially populated sidecar is never silently adopted.
                    values = self._conn.execute(f"SELECT COUNT(*) FROM {_ALIAS}.property_values").fetchone()[0]
                    names = self._conn.execute(f"SELECT COUNT(*) FROM {_ALIAS}.indexed_properties").fetchone()[0]
                    if values or names:
                        raise StorageCorruptionError("property-index metadata is missing from a non-empty sidecar")
                    self._set_metadata(source_revision)
                elif metadata[1] != source_revision:
                    state, revision = self._read_state_from_connection()
                    if revision != source_revision:
                        raise StorageCorruptionError("source revision changed during property-index rebuild")
                    self._rebuild_rows(state)
                    self._set_metadata(source_revision)
                self._conn.commit()
                if state is not None:
                    self._state, self._version = state, source_revision
            except Exception:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _ensure_index_current(self) -> None:
        """Refresh an index left stale by a separate source-store handle."""
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN")
            try:
                source = self._conn.execute("SELECT revision FROM metadata WHERE id=1").fetchone()
                metadata = self._metadata()
                stale = (
                    source is None
                    or not isinstance(source[0], int)
                    or isinstance(source[0], bool)
                    or metadata is None
                    or metadata[0] != self._source_identity()
                    or metadata[1] != source[0]
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if stale:
            self._synchronize_index()

    def _write_state(self, state: dict[str, Any]) -> None:
        # Called by SQLiteTreeStore while BEGIN IMMEDIATE is already open.  The
        # sidecar work is therefore in exactly the same SQLite transaction.
        super()._write_state(state)
        if self._index_ready:
            self._rebuild_rows(state)
            self._set_metadata(self._version + 1)

    def create_scalar_index(self, property_name: str) -> "SQLiteScalarPropertyIndexExperiment":
        """Register and durably build an index for one scalar property."""
        property_name = _property_name(property_name)
        self._ensure_index_current()
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    f"INSERT OR IGNORE INTO {_ALIAS}.indexed_properties(property_name) VALUES (?)",
                    (property_name,),
                )
                state, revision = self._read_state_from_connection()
                self._rebuild_rows(state)
                self._set_metadata(revision)
                self._conn.commit()
                self._state, self._version = state, revision
                return self
            except Exception:
                self._conn.rollback()
                raise

    # Friendly names are intentionally scoped to this experiment class.
    index_scalar_property = create_scalar_index
    create_index = create_scalar_index

    def drop_scalar_index(self, property_name: str) -> None:
        property_name = _property_name(property_name)
        self._ensure_index_current()
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    f"DELETE FROM {_ALIAS}.indexed_properties WHERE property_name=?",
                    (property_name,),
                )
                self._conn.execute(
                    f"DELETE FROM {_ALIAS}.property_values WHERE property_name=?",
                    (property_name,),
                )
                source = self._conn.execute("SELECT revision FROM metadata WHERE id=1").fetchone()
                self._set_metadata(source[0])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    drop_index = drop_scalar_index

    def _resolve_target_id_sql(self, target: str | Node, root_id: str) -> str:
        """Resolve an ID/path using only the ordered SQLite edge table."""
        if isinstance(target, Node):
            target = target.id
        if not isinstance(target, str):
            raise NotFoundError(target)
        if not isinstance(root_id, str):
            raise StorageCorruptionError("invalid SQLite metadata root ID")
        try:
            _validate_id(root_id)
        except InvalidOperationError as exc:
            raise StorageCorruptionError("invalid SQLite metadata root ID") from exc
        if self._conn.execute("SELECT 1 FROM nodes WHERE id=?", (root_id,)).fetchone() is None:
            raise StorageCorruptionError("SQLite metadata root does not exist")
        if target.startswith("/"):
            current = root_id
            for part in _path_parts(target):
                row = self._conn.execute(
                    """SELECT c.child_id FROM children c JOIN nodes n ON n.id=c.child_id
                       WHERE c.parent_id=? AND n.name=? ORDER BY c.position LIMIT 1""",
                    (current, part),
                ).fetchone()
                if row is None:
                    raise NotFoundError(target)
                current = row[0]
            return current
        row = self._conn.execute("SELECT id FROM nodes WHERE id=?", (target,)).fetchone()
        if row is None:
            raise NotFoundError(target)
        return row[0]

    def _decode_direct_records(
        self, rows: list[tuple[Any, ...]], *, root_id: str
    ) -> list[dict[str, Any]]:
        """Decode candidate rows and child edges, without reading all nodes."""
        records: list[dict[str, Any]] = []
        ids: list[str] = []
        try:
            for row in rows:
                (node_id, name, typ, properties, parent_id, created_at,
                 modified_at, depth, order_path) = row
                _validate_id(node_id)
                _validate_name(name, root=node_id == root_id)
                if not isinstance(typ, str) or not typ:
                    raise InvalidOperationError("invalid node type")
                if not isinstance(properties, str):
                    raise InvalidOperationError("invalid node properties encoding")
                decoded = _decode_value(json.loads(properties))
                props = _clone_properties(decoded)
                if parent_id is not None:
                    _validate_id(parent_id)
                _validate_timestamp(created_at)
                _validate_timestamp(modified_at)
                if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
                    raise InvalidOperationError("invalid query depth")
                if not isinstance(order_path, str):
                    raise InvalidOperationError("invalid query order")
                records.append({
                    "id": node_id, "name": name, "type": typ,
                    "properties": props, "parent_id": parent_id,
                    "children": [], "created_at": created_at,
                    "modified_at": modified_at, "_depth": depth,
                    "_order": order_path,
                })
                ids.append(node_id)
            children: dict[str, list[str]] = {node_id: [] for node_id in ids}
            if ids:
                placeholders = ",".join("?" for _ in ids)
                child_rows = self._conn.execute(
                    f"""SELECT c.parent_id, c.child_id, c.position
                        FROM children c JOIN nodes n ON n.id=c.child_id
                        WHERE c.parent_id IN ({placeholders})
                        ORDER BY c.parent_id, c.position""",
                    ids,
                ).fetchall()
                positions: dict[str, int] = {node_id: 0 for node_id in ids}
                for parent_id, child_id, position in child_rows:
                    if parent_id not in children:
                        raise StorageCorruptionError("invalid direct-query child parent")
                    if not isinstance(position, int) or isinstance(position, bool) or position != positions[parent_id]:
                        raise StorageCorruptionError("invalid direct-query child position")
                    positions[parent_id] += 1
                    children[parent_id].append(child_id)
            for record in records:
                record["children"] = children[record["id"]]
            return records
        except StorageCorruptionError:
            raise
        except (TypeError, ValueError, KeyError, json.JSONDecodeError,
                InvalidOperationError, InvalidPropertyError) as exc:
            raise StorageCorruptionError(f"invalid SQLite direct-query row: {exc}") from exc

    def lookup_scalar(
        self,
        property_name: str,
        value: Any,
        target: str = "/",
        *,
        recursive: bool = True,
        include_root: bool = False,
        predicate: Callable[[Any], bool] | Mapping[str, Any] | None = None,
    ) -> "_DirectScalarQuery":
        """Lookup an exact typed scalar without loading the complete tree.

        The sidecar narrows the source to matching IDs, while a recursive SQL
        CTE computes the primary-child traversal order.  Only matching node
        rows (and their child ID lists needed by detached :class:`Node` views)
        are decoded into Python.  This path is intentionally limited to exact
        scalar equality: arbitrary predicates remain a Python final filter,
        but still run only against indexed candidates.
        """
        property_name = _property_name(property_name)
        key = _scalar_key(value)
        if key is None:
            raise TypeError("property-index lookup values must be scalar")
        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a bool")
        if not isinstance(include_root, bool):
            raise TypeError("include_root must be a bool")
        if predicate is not None and not callable(predicate) and not isinstance(predicate, Mapping):
            raise TypeError("predicate must be callable, a property mapping, or None")

        # A stale sidecar can be caused by another SQLiteTreeStore handle;
        # catch up before taking the coherent source+sidecar read snapshot.
        for _attempt in range(2):
            self._ensure_index_current()
            with self._lock:
                self._ensure_open()
                self._conn.execute("BEGIN")
                try:
                    source = self._conn.execute(
                        "SELECT revision, root_id FROM metadata WHERE id=1"
                    ).fetchone()
                    metadata = self._metadata()
                    if (
                        source is None
                        or not isinstance(source[0], int)
                        or isinstance(source[0], bool)
                        or source[0] < 0
                        or metadata is None
                        or metadata[0] != self._source_identity()
                        or metadata[1] != source[0]
                    ):
                        self._conn.rollback()
                        continue
                    root_id = self._resolve_target_id_sql(target, source[1])
                    candidate_missing = self._conn.execute(
                        f"""SELECT 1 FROM {_ALIAS}.property_values p
                            LEFT JOIN nodes n ON n.id = p.node_id
                            WHERE p.property_name=? AND p.value_type=? AND p.value=?
                              AND n.id IS NULL LIMIT 1""",
                        (property_name, key[0], key[1]),
                    ).fetchone()
                    if candidate_missing is not None:
                        raise StorageCorruptionError(
                            "property-index row references a missing source node"
                        )
                    # A fixed-width position component gives SQLite's text
                    # ordering the same depth-first order as Query._iter_ids.
                    # SQLite child positions are signed 64-bit integers, so
                    # 20 columns cover every representable position.
                    rows = self._conn.execute(
                        f"""WITH RECURSIVE tree(node_id, order_path, depth) AS (
                               SELECT ?, CAST('' AS TEXT), 0
                               UNION ALL
                               SELECT c.child_id,
                                      tree.order_path || printf('%020d/', c.position),
                                      tree.depth + 1
                               FROM tree
                               JOIN children c ON c.parent_id = tree.node_id
                           )
                           SELECT n.id, n.name, n.type, n.properties, n.parent_id,
                                  n.created_at, n.modified_at, tree.depth, tree.order_path
                           FROM tree
                           JOIN nodes n ON n.id = tree.node_id
                           JOIN {_ALIAS}.property_values p ON p.node_id = n.id
                           WHERE p.property_name=? AND p.value_type=? AND p.value=?
                           ORDER BY tree.order_path""",
                        (root_id, property_name, key[0], key[1]),
                    ).fetchall()
                    # ``root_id`` is the traversal target, not necessarily the
                    # database root.  Name validation must still recognize only
                    # the actual singleton root as having the empty-name
                    # sentinel; an indexed non-root target can legitimately be
                    # returned by ``include_root=True``.
                    records = self._decode_direct_records(rows, root_id=source[1])
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
            exact_key = _index_key(value)
            def exact(node: Node) -> bool:
                actual = _property_value(node.properties, property_name)
                return actual is not _MISSING and _index_key(actual) == exact_key
            final_predicate = exact
            if predicate is not None:
                extra = _predicate_from(predicate)
                assert extra is not None
                old = final_predicate
                final_predicate = lambda node: bool(old(node)) and bool(extra(node))
            return _DirectScalarQuery(
                records,
                recursive=recursive,
                include_root=include_root,
                predicate=final_predicate,
            )
        raise StorageCorruptionError("property-index revision changed during lookup")

    query_scalar = lookup_scalar
    lookup = lookup_scalar

    def scalar_ids(self, property_name: str, value: Any, target: str = "/", **kwargs: Any) -> tuple[str, ...]:
        return self.lookup_scalar(property_name, value, target, **kwargs).ids()



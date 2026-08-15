"""Opt-in incremental SQLite commit experiment for GROVE.

``SQLiteTreeStore`` intentionally rewrites the complete relational snapshot on
commits.  This module prototypes a conservative alternative: it diffs the
validated transaction snapshot against the durable snapshot and updates only
changed node rows and changed ordered edge lists.  The experiment is kept out
of the core adapter until measurements and failure testing justify a migration.

The diff is computed while the caller owns ``BEGIN IMMEDIATE``.  Nodes are
inserted/updated/deleted under that transaction, and edge rows are replaced
only for parents whose ordered child list changed.  SQLite therefore keeps
its usual all-or-nothing recovery guarantees while readers never observe a
partially applied tree.
"""
from __future__ import annotations

from typing import Any

from .sqlite_store import SQLiteTreeStore


class SQLiteIncrementalTreeStore(SQLiteTreeStore):
    """Experimental :class:`SQLiteTreeStore` with dirty-row commits.

    This class is deliberately separate from ``SQLiteTreeStore``.  It keeps the
    same public API and schema, so a database can be compared with the core
    adapter or reopened by it.  ``last_commit_stats`` reports logical rows
    changed by the most recent successful commit; these counters are useful for
    experiments and are not a stability promise for production telemetry.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._last_commit_stats: dict[str, int] | None = None
        self._pending_commit_stats: dict[str, int] | None = None
        super().__init__(*args, **kwargs)

    def _commit(self, tx: Any) -> None:
        # ``_write_state`` runs before SQLite COMMIT.  Publish counters only
        # after the parent implementation returns successfully, otherwise a
        # killed/failed transaction could masquerade as a successful sample.
        self._pending_commit_stats = None
        try:
            super()._commit(tx)
        except Exception:
            self._pending_commit_stats = None
            raise
        else:
            self._last_commit_stats = self._pending_commit_stats
            self._pending_commit_stats = None

    @property
    def last_commit_stats(self) -> dict[str, int] | None:
        """Copy of dirty-row counters for the most recent successful commit."""
        return None if self._last_commit_stats is None else dict(self._last_commit_stats)

    # A shorter spelling is convenient in benchmark scripts.
    @property
    def commit_stats(self) -> dict[str, int] | None:
        return self.last_commit_stats

    @staticmethod
    def _node_columns(record: dict[str, Any], properties_json: str) -> tuple[Any, ...]:
        return (
            record["id"], record["name"], record["type"], properties_json,
            record["parent_id"], record["created_at"], record["modified_at"],
        )

    def _write_state(self, state: dict[str, Any]) -> None:
        """Apply a validated state by touching only dirty relational rows.

        The caller (``SQLiteTreeStore._commit``) already owns ``BEGIN
        IMMEDIATE``.  Reading the durable state here is intentional: metadata
        revision checks alone cannot detect an out-of-band row edit that keeps
        the same revision.  Loading and validating that state gives this
        prototype the same fail-closed behavior as the normal full rewrite,
        while making the diff authoritative for the rows actually in SQLite.
        """
        old_state, _old_revision = self._read_state_from_connection()
        old_nodes = old_state["nodes"]
        new_nodes = state["nodes"]
        old_ids = set(old_nodes)
        new_ids = set(new_nodes)
        removed_ids = old_ids - new_ids
        inserted_ids = new_ids - old_ids
        common_ids = old_ids & new_ids

        encoded_new = {
            node_id: self._properties_json(record["properties"])
            for node_id, record in new_nodes.items()
        }

        def columns_equal(node_id: str) -> bool:
            old = old_nodes[node_id]
            new = new_nodes[node_id]
            return self._node_columns(old, self._properties_json(old["properties"])) == self._node_columns(
                new, encoded_new[node_id]
            )

        updated_ids = {node_id for node_id in common_ids if not columns_equal(node_id)}
        dirty_parents = {
            node_id
            for node_id in old_ids | new_ids
            if old_nodes.get(node_id, {}).get("children")
            != new_nodes.get(node_id, {}).get("children")
        }

        # Remove changed edge lists first.  This handles moves and reorders
        # without transient PRIMARY KEY/UNIQUE conflicts.  Edges for deleted
        # parent rows are explicitly removed as well (rather than relying on a
        # cascade), which makes row accounting and old-schema behavior clear.
        edge_deletes = 0
        for parent_id in sorted(dirty_parents):
            cursor = self._conn.execute(
                "DELETE FROM children WHERE parent_id = ?", (parent_id,)
            )
            edge_deletes += max(cursor.rowcount, 0)

        # A deleted node can still be referenced by another deleted node while
        # this transaction is in progress.  Nulling all removed parent links
        # keeps the operation valid with immediate-FK databases too, matching
        # the defensive behavior of the core writer.
        node_detaches = 0
        for node_id in sorted(removed_ids):
            old = old_nodes[node_id]
            if old["parent_id"] is not None:
                self._conn.execute(
                    "UPDATE nodes SET parent_id = NULL WHERE id = ?", (node_id,)
                )
                node_detaches += 1

        node_deletes = 0
        for node_id in sorted(removed_ids):
            self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            node_deletes += 1

        # New rows are inserted before changed existing rows so a moved node
        # can point at a newly inserted parent.  The shipped schema uses
        # deferred parent FKs, as does the core writer.
        for node_id in sorted(inserted_ids):
            record = new_nodes[node_id]
            self._conn.execute(
                """INSERT INTO nodes
                   (id, name, type, properties, parent_id, created_at, modified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                self._node_columns(record, encoded_new[node_id]),
            )

        node_updates = 0
        for node_id in sorted(updated_ids):
            record = new_nodes[node_id]
            values = self._node_columns(record, encoded_new[node_id])
            self._conn.execute(
                """UPDATE nodes SET id = ?, name = ?, type = ?, properties = ?,
                   parent_id = ?, created_at = ?, modified_at = ? WHERE id = ?""",
                values + (node_id,),
            )
            node_updates += 1

        edge_inserts = 0
        for parent_id in sorted(dirty_parents & new_ids):
            for position, child_id in enumerate(new_nodes[parent_id]["children"]):
                self._conn.execute(
                    """INSERT INTO children (parent_id, child_id, position)
                       VALUES (?, ?, ?)""",
                    (parent_id, child_id, position),
                )
                edge_inserts += 1

        # Metadata is updated by the parent commit method and is included as a
        # separate counter there.  ``rows_written`` intentionally excludes it:
        # it measures tree rows, which is the useful dirty-vs-rewrite metric.
        self._pending_commit_stats = {
            "node_inserts": len(inserted_ids),
            "node_updates": node_updates,
            "node_deletes": node_deletes,
            "node_detaches": node_detaches,
            "edge_inserts": edge_inserts,
            "edge_deletes": edge_deletes,
            "rows_written": len(inserted_ids) + node_updates + node_detaches
            + node_deletes + edge_inserts + edge_deletes,
            "old_nodes": len(old_ids),
            "new_nodes": len(new_ids),
            "old_edges": sum(len(record["children"]) for record in old_nodes.values()),
            "new_edges": sum(len(record["children"]) for record in new_nodes.values()),
        }


# Explicit experiment-oriented alias names make discovery easy without
# exposing this class through grove.__init__.
IncrementalSQLiteTreeStore = SQLiteIncrementalTreeStore
SQLiteDirtyRowTreeStore = SQLiteIncrementalTreeStore

__all__ = [
    "SQLiteIncrementalTreeStore",
    "IncrementalSQLiteTreeStore",
    "SQLiteDirtyRowTreeStore",
]

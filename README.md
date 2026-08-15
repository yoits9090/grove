# GROVE

GROVE is a small, typed, persistent object-tree database. This repository is
currently the **0.1 vertical slice**, not the complete mission: it prioritizes
clear invariants, deterministic behavior, and crash-testable persistence over
scale and feature breadth.

## Current slice

- Stable opaque string IDs, names, types, detached node views, and properties.
- Exactly one root, ordered children, unique sibling names, and cycle-safe moves.
- Create, read, update, delete, rename, move, copy, transactions, and optimistic
  transaction conflict detection.
- Absolute normalized paths (`/a/b`) and ID lookup.
- JSON subtree export/import with explicit encodings for bytes, timestamps,
  and references.
- Detached snapshot queries with typed predicates and lightweight in-memory
  property indexes (`Query`, `PropertyIndex`).
- Optional schemas for node types and property constraints, validated atomically
  on create, update, and import (`Schema`, `SchemaValidationError`).
- A checksummed append-only whole-snapshot log with fsync and safe truncated-tail
  recovery (`PersistentTreeStore`).
- An experimental SQLite WAL backend with relational `nodes`/`children` edges,
  durable revisions, and cross-instance optimistic conflict detection
  (`SQLiteTreeStore`).
- A small durable logical history API (`SQLiteHistory`, `Snapshot`) built from
  SQLite online-backup artifacts; see `docs/logical-history-experiment.md`.
- Basic synchronous change subscriptions and a `grove` tree/get/export CLI.

The snapshot log is intentionally a conservative reference implementation. It
rewrites the whole logical state at each commit and is unsuitable for large
workloads. SQLite is the current correctness-first storage experiment; see
`docs/decisions.md`. The durable scalar-index experiment remains private; logical history is a
small public API with deliberately narrow guarantees.

## Quick start

```python
from grove import TreeStore, Reference

store = TreeStore()
orgs = store.create("organizations")
acme = store.create("acme", parent=orgs.id, type="organization")
alice = store.create("alice", parent="/organizations/acme", properties={"active": True})
store.rename(alice.id, "alice-admin")
store.move(alice.id, "/organizations")
print(store.path(alice.id))
print(store.export_json("/organizations", indent=2))
```

Durable use:

```python
from grove import PersistentTreeStore
with PersistentTreeStore("grove.log") as db:
    db.create("system")
# Reopening recovers the last complete checksum-verified frame.
```

Run tests with `uv run pytest`. The CLI is `uv run grove DB tree`.

## Optional schemas

Pass a `Schema` when constructing any store (including `PersistentTreeStore`
and `SQLiteTreeStore`) to enforce node type and property constraints. A
shorthand declaration maps property names to Python types; the explicit form
adds required properties and rejects unknown properties:

```python
from grove import Schema, SchemaValidationError, TreeStore

schema = Schema({
    "person": {
        "properties": {"name": str, "age": {"type": int, "required": True}},
        "required": ["name"],
        "allow_extra": False,
    },
})
db = TreeStore(schema=schema)
db.create("alice", type="person", properties={"name": "Alice", "age": 42})
try:
    db.create("bad", type="person", properties={"name": 7, "age": 1})
except SchemaValidationError:
    pass  # no node was created
```

A schema with declared types rejects undeclared types by default (the root
sentinel is always allowed). Set `allow_unknown_types=True` to constrain only
declared types. Property constraints may be Python types, tuples of types,
`{"type": ...}` declarations, `enum`/`values`, or a callable predicate.
Validation is performed before each mutation is committed; failed imports and
updates are atomic, including when used inside an explicit transaction.

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
- A checksummed append-only whole-snapshot log with fsync and safe truncated-tail
  recovery (`PersistentTreeStore`).
- Basic synchronous change subscriptions and a `grove` tree/get/export CLI.

The snapshot log is intentionally a conservative reference implementation. It
rewrites the whole logical state at each commit and is unsuitable for large
workloads. SQLite is the leading next storage experiment; see `docs/decisions.md`.

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

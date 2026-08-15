"""Isolated incremental Merkle hashing experiment for GROVE.

This module deliberately lives outside :mod:`grove` and has no effect on any
GROVE store or public API.  It consumes the JSON-compatible value returned by
``TreeStore.export`` (or ``export_json``), canonicalizes each exported node,
and computes a SHA-256 Merkle digest for the exported tree.

``IncrementalMerkle.update`` accepts a fresh export and reuses cached digests
for unchanged subtrees.  It still has to inspect the export to discover which
nodes changed, but it does not perform cryptographic hashing for a subtree
whose node payload, child-ID order, and descendants are unchanged.  The
returned :class:`UpdateStats` makes that distinction explicit; this is useful
for an experiment and should not be mistaken for a production incremental
storage implementation.

The parent ID and timestamps are included in each node payload.  Therefore a
full-tree export changes when a node is moved, renamed, or updated.  As GROVE
exports are self-contained, the exported root's ``parent_id`` is intentionally
``None``; a subtree's *location* is not represented by its parent edge, though
the move operation still updates ``modified_at`` and is consequently visible
in its hash.  Use ``store.export('/')`` when the complete hierarchy is needed.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping


_HASH_ALGORITHM = "sha256"
_DOMAIN = b"grove-experiment-merkle-v1\x00"
_NODE_FIELDS = (
    "id",
    "name",
    "type",
    "properties",
    "parent_id",
    "created_at",
    "modified_at",
)
_REQUIRED_FIELDS = frozenset(("id", "name", "type", "properties", "children"))


def _canonical_value(value: Any, *, path: str = "$") -> Any:
    """Validate and detach a JSON value used by an exported GROVE tree.

    GROVE's export format is JSON-compatible already, but validating here
    prevents accidental hash ambiguity (especially NaN and non-string map
    keys) when this experiment is called directly.
    """
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number at {path}")
        return value
    if isinstance(value, list):
        return [_canonical_value(item, path=f"{path}[{index}]")
                for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"object key at {path} must be a string")
            result[key] = _canonical_value(item, path=f"{path}.{key}")
        return result
    raise TypeError(f"unsupported value at {path}: {type(value).__name__}")


def canonical_encode(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for an exported GROVE value.

    Object keys are sorted recursively by ``json.dumps``.  Compact separators,
    UTF-8 output, and ``allow_nan=False`` make the byte representation stable
    across processes and Python versions for valid GROVE exports.
    """
    detached = _canonical_value(value)
    return json.dumps(
        detached,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# Names that are convenient in notebooks and make the encoding being used
# obvious to callers of the experiment.
canonical_json = canonical_encode
canonical_bytes = canonical_encode


def _load_export(data: Mapping[str, Any] | str) -> tuple[str, dict[str, "_Node"]]:
    """Flatten a nested GROVE subtree export and perform structural checks."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("export is not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise TypeError("export must be a mapping or JSON object")

    nodes: dict[str, _Node] = {}
    active: set[str] = set()

    def visit(raw: Mapping[str, Any], expected_parent: str | None,
              *, is_root: bool) -> str:
        if not isinstance(raw, Mapping):
            raise ValueError("export child must be an object")
        missing = _REQUIRED_FIELDS - set(raw)
        if missing:
            raise ValueError(f"export node missing fields: {sorted(missing)!r}")
        node_id = raw["id"]
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("export node IDs must be non-empty strings")
        if node_id in active:
            raise ValueError("export contains a cycle")
        if node_id in nodes:
            raise ValueError(f"export contains duplicate node ID: {node_id!r}")
        parent_id = raw.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError(f"invalid parent_id for {node_id!r}")
        if is_root:
            # A subtree export always detaches its root.  Accepting a parent
            # here would make the exported representation ambiguous.
            if parent_id is not None:
                raise ValueError("export root must have parent_id=None")
        elif expected_parent is not None and parent_id != expected_parent:
            raise ValueError(f"child {node_id!r} has incorrect parent_id")
        for field_name in ("name", "type", "created_at", "modified_at"):
            if not isinstance(raw.get(field_name), str):
                raise ValueError(f"export field {field_name!r} must be a string")
        if not isinstance(raw["properties"], Mapping):
            raise ValueError(f"properties for {node_id!r} must be an object")
        children = raw["children"]
        if not isinstance(children, list):
            raise ValueError(f"children for {node_id!r} must be a list")

        active.add(node_id)
        child_ids: list[str] = []
        try:
            # Record this node only after validating child IDs recursively.  A
            # duplicate sibling ID is diagnosed by the same global check.
            for child in children:
                if not isinstance(child, Mapping):
                    raise ValueError(f"child of {node_id!r} must be an object")
                child_ids.append(visit(child, node_id, is_root=False))
        finally:
            active.remove(node_id)

        own = {field_name: raw[field_name] for field_name in _NODE_FIELDS}
        own_bytes = canonical_encode(own)
        child_ids_tuple = tuple(child_ids)
        # This structural signature is deliberately not a cryptographic hash:
        # it lets update() detect a changed descendant without hashing every
        # descendant first.  The actual Merkle digest remains _digest() below.
        signature = (own_bytes, tuple(nodes[child_id].signature for child_id in child_ids_tuple))
        nodes[node_id] = _Node(
            node_id=node_id,
            own_bytes=own_bytes,
            child_ids=child_ids_tuple,
            signature=signature,
        )
        return node_id

    root_id = visit(data, None, is_root=True)
    return root_id, nodes


@dataclass(frozen=True, slots=True)
class UpdateStats:
    """Instrumentation for one initial build or incremental update.

    ``nodes_hashed`` counts nodes for which a SHA-256 digest was actually
    computed.  ``nodes_reused`` counts nodes covered by an old cached subtree
    and therefore skipped.  ``nodes_visited`` counts recursive comparison
    calls, while ``canonical_bytes`` is the number of node-payload bytes
    canonicalized while reading the supplied export.
    """

    root_hash: str
    nodes_hashed: int
    nodes_reused: int
    nodes_visited: int
    canonical_bytes: int
    elapsed_ns: int
    hashed_node_ids: tuple[str, ...] = field(default_factory=tuple)
    reused_node_ids: tuple[str, ...] = field(default_factory=tuple)
    changed_node_ids: tuple[str, ...] = field(default_factory=tuple)
    removed_node_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def elapsed_ms(self) -> float:
        return self.elapsed_ns / 1_000_000.0

    @property
    def hash_work(self) -> int:
        """Alias emphasizing that this is digest work, not export traversal."""
        return self.nodes_hashed


@dataclass(frozen=True, slots=True)
class _Node:
    node_id: str
    own_bytes: bytes
    child_ids: tuple[str, ...]
    signature: tuple[Any, ...]


class IncrementalMerkle:
    """Merkle digest cache over one or more GROVE exported subtrees.

    The constructor performs a complete build.  Call :meth:`update` with a
    subsequent export from the same logical tree to reuse unchanged hashes.
    IDs are expected to be stable, as they are in GROVE across move/rename and
    property updates.  A fresh root or ``preserve_ids=False`` import naturally
    behaves as a complete replacement.
    """

    def __init__(self, exported: Mapping[str, Any] | str):
        started = time.perf_counter_ns()
        root_id, nodes = _load_export(exported)
        hashes: dict[str, bytes] = {}
        sizes: dict[str, int] = {}
        hashed_ids: list[str] = []

        def build(node_id: str) -> bytes:
            node = nodes[node_id]
            children = [build(child_id) for child_id in node.child_ids]
            digest = _digest(node.own_bytes, children)
            hashes[node_id] = digest
            sizes[node_id] = 1 + sum(sizes[child_id] for child_id in node.child_ids)
            hashed_ids.append(node_id)
            return digest

        root_digest = build(root_id)
        self._root_id = root_id
        self._nodes = nodes
        self._hashes = hashes
        self._sizes = sizes
        self._root_hash = root_digest.hex()
        self._last_stats = UpdateStats(
            root_hash=self._root_hash,
            nodes_hashed=len(nodes),
            nodes_reused=0,
            nodes_visited=len(nodes),
            canonical_bytes=sum(len(node.own_bytes) for node in nodes.values()),
            elapsed_ns=time.perf_counter_ns() - started,
            hashed_node_ids=tuple(hashed_ids),
            changed_node_ids=tuple(sorted(nodes)),
        )

    @classmethod
    def from_store(cls, store: Any, target: Any = "/") -> "IncrementalMerkle":
        """Build from a GROVE store using its detached exported subtree."""
        return cls(store.export(target))

    @property
    def root_id(self) -> str:
        return self._root_id

    @property
    def root_hash(self) -> str:
        return self._root_hash

    @property
    def last_stats(self) -> UpdateStats:
        return self._last_stats

    @property
    def node_hashes(self) -> dict[str, str]:
        """Return a detached ID-to-hex-digest mapping for diagnostics."""
        return {node_id: digest.hex() for node_id, digest in self._hashes.items()}

    def node_hash(self, node_id: str) -> str:
        """Return a cached digest by stable GROVE node ID."""
        try:
            return self._hashes[node_id].hex()
        except KeyError as exc:
            raise KeyError(node_id) from exc

    hash_for = node_hash

    def update(self, exported: Mapping[str, Any] | str) -> UpdateStats:
        """Update the cache from a fresh GROVE export and return instrumentation."""
        started = time.perf_counter_ns()
        root_id, new_nodes = _load_export(exported)
        old_nodes, old_hashes, old_sizes = self._nodes, self._hashes, self._sizes
        new_hashes: dict[str, bytes] = {}
        new_sizes: dict[str, int] = {}
        hashed_ids: list[str] = []
        reused_ids: list[str] = []
        visited = 0

        def compute(node_id: str) -> bytes:
            nonlocal visited
            visited += 1
            node = new_nodes[node_id]
            old = old_nodes.get(node_id)
            # The recursively-built structural signature detects changed
            # descendants while avoiding cryptographic hashing.  A matching
            # signature means this entire exported subtree is unchanged, so its
            # old Merkle digest can be reused as one unit.
            if old is not None and old.signature == node.signature:
                # Copy all cached descendants into the new map.  Keeping these
                # entries available makes node_hash() useful for diagnostics,
                # while no digest work is performed for any of them.
                def reuse(cached_id: str) -> None:
                    new_hashes[cached_id] = old_hashes[cached_id]
                    new_sizes[cached_id] = old_sizes[cached_id]
                    reused_ids.append(cached_id)
                    for child_id in old_nodes[cached_id].child_ids:
                        reuse(child_id)
                reuse(node_id)
                return new_hashes[node_id]

            children = [compute(child_id) for child_id in node.child_ids]
            digest = _digest(node.own_bytes, children)
            new_hashes[node_id] = digest
            new_sizes[node_id] = 1 + sum(new_sizes[child_id] for child_id in node.child_ids)
            hashed_ids.append(node_id)
            return digest

        root_digest = compute(root_id)
        removed = tuple(sorted(set(old_nodes) - set(new_nodes)))
        changed = tuple(sorted(
            node_id for node_id in new_nodes
            if node_id in old_hashes and node_id in new_hashes and old_hashes[node_id] != new_hashes[node_id]
        ))
        self._root_id, self._nodes = root_id, new_nodes
        self._hashes, self._sizes = new_hashes, new_sizes
        self._root_hash = root_digest.hex()
        canonical_total = sum(len(node.own_bytes) for node in new_nodes.values())
        stats = UpdateStats(
            root_hash=self._root_hash,
            nodes_hashed=len(hashed_ids),
            nodes_reused=len(reused_ids),
            nodes_visited=visited,
            canonical_bytes=canonical_total,
            elapsed_ns=time.perf_counter_ns() - started,
            hashed_node_ids=tuple(hashed_ids),
            reused_node_ids=tuple(reused_ids),
            changed_node_ids=changed,
            removed_node_ids=removed,
        )
        self._last_stats = stats
        return stats


def _digest(own_bytes: bytes, children: list[bytes]) -> bytes:
    hasher = hashlib.sha256()
    hasher.update(_DOMAIN)
    # Length-prefix every component to make the framing unambiguous even if
    # this experiment later changes the canonical representation.
    hasher.update(len(own_bytes).to_bytes(8, "big"))
    hasher.update(own_bytes)
    hasher.update(len(children).to_bytes(8, "big"))
    for child in children:
        hasher.update(len(child).to_bytes(8, "big"))
        hasher.update(child)
    return hasher.digest()


def merkle_hash(exported: Mapping[str, Any] | str) -> str:
    """Compute a complete SHA-256 Merkle hash for a GROVE export."""
    return IncrementalMerkle(exported).root_hash


hash_export = merkle_hash


def benchmark(*, branch_count: int = 8, leaves_per_branch: int = 128) -> dict[str, Any]:
    """Run a small reproducible unchanged-subtree benchmark.

    The return value is JSON serializable and intentionally reports counts as
    well as timings.  Timings are observational only; use the counts as the
    portable result of the experiment.
    """
    from grove import TreeStore

    store = TreeStore()
    branches = [store.create(f"branch-{index}") for index in range(branch_count)]
    leaves = []
    for branch in branches:
        for index in range(leaves_per_branch):
            leaves.append(store.create(
                f"leaf-{index}", parent=branch.id,
                properties={"value": index, "branch": branch.name},
            ))
    exported = store.export("/")
    incremental = IncrementalMerkle(exported)
    target = leaves[len(leaves) // 2]
    before = incremental.node_hash(target.id)
    store.update(target.id, properties={"value": -1})
    updated = store.export("/")
    incremental_stats = incremental.update(updated)
    full_started = time.perf_counter_ns()
    full_hash = merkle_hash(updated)
    full_elapsed = time.perf_counter_ns() - full_started
    assert full_hash == incremental.root_hash
    return {
        "nodes": 1 + branch_count + branch_count * leaves_per_branch,
        "branch_count": branch_count,
        "leaves_per_branch": leaves_per_branch,
        "target": target.id,
        "target_hash_changed": before != incremental.node_hash(target.id),
        "incremental": {
            "nodes_hashed": incremental_stats.nodes_hashed,
            "nodes_reused": incremental_stats.nodes_reused,
            "nodes_visited": incremental_stats.nodes_visited,
            "elapsed_ms": incremental_stats.elapsed_ms,
            "root_hash": incremental.root_hash,
        },
        "full": {"nodes_hashed": 1 + branch_count + branch_count * leaves_per_branch,
                 "elapsed_ms": full_elapsed / 1_000_000.0,
                 "root_hash": full_hash},
    }


def main() -> None:
    print(json.dumps(benchmark(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

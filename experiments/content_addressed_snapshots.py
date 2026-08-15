"""Content-addressed structural-sharing snapshot experiment.

This module is intentionally isolated from GROVE's production API.  It is a
small storage prototype for evaluating a possible replacement for whole-state
snapshot persistence:

* every node is encoded as a canonical, immutable JSON blob;
* a node records child *blob hashes*, rather than a parent pointer, so moving
  an existing subtree reuses all of that subtree's blobs;
* a root manifest names the root blob and is atomically published; and
* retention GC marks blobs reachable from retained manifests before deleting
  anything.

The on-disk layout is::

    <directory>/nodes/<sha256>.blob
    <directory>/manifests/manifest-<root-sha256>.json
    <directory>/CURRENT

A node blob deliberately does not store ``parent_id``.  Parent IDs are
reconstructed while loading, which is what permits a move to share the moved
subtree.  This is an experiment and not a public GROVE API.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from grove.errors import InvalidPropertyError, StorageCorruptionError
from grove.store import (
    _check_invariants,
    _decode_value,
    _encode_value,
)
from grove import TreeStore

_FORMAT = 1
_HASH_LENGTH = 64


def _canonical_json(value: Any) -> bytes:
    """Encode JSON deterministically, with no whitespace or NaN values."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StorageCorruptionError(f"value is not canonical JSON: {exc}") from exc


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync (unsupported on some platforms)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    """Durably write ``payload`` and atomically install ``path``.

    The destination is never opened for writing.  If it already exists, it
    must be byte-for-byte identical; this both enforces immutability and makes
    concurrent/idempotent publication harmless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise StorageCorruptionError(f"cannot read immutable artifact {path}") from exc
        if existing != payload:
            raise StorageCorruptionError(f"immutable artifact collision at {path}")
        return
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # No reader can observe this path until after the fully fsynced file
        # has been renamed into place.
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass



def _atomic_replace(path: Path, payload: bytes) -> None:
    """Atomically replace a mutable publication pointer such as CURRENT."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

def _check_hash(value: Any, label: str = "hash") -> str:
    if not isinstance(value, str) or len(value) != _HASH_LENGTH:
        raise StorageCorruptionError(f"invalid {label}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise StorageCorruptionError(f"invalid {label}") from exc
    return value


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A published immutable snapshot.

    ``open`` materializes a detached :class:`grove.TreeStore`; mutations to
    that returned store cannot modify this repository.  ``path`` is retained
    as a convenient alias for the manifest path used by older experiments.
    """

    snapshot_id: str
    root_hash: str
    revision: int | None
    manifest_path: Path = field(compare=False)
    _repository: "ContentAddressedSnapshotStore" = field(repr=False, compare=False)

    @property
    def path(self) -> Path:
        return self.manifest_path

    @property
    def manifest(self) -> Path:
        return self.manifest_path

    def open(self) -> TreeStore:
        return self._repository.open(self)

    def load(self) -> TreeStore:
        return self.open()


@dataclass(frozen=True, slots=True)
class RepositoryReport:
    """Storage accounting returned by :meth:`report`."""

    manifest_count: int
    blob_count: int
    reachable_blob_count: int
    unreachable_blob_count: int
    manifest_bytes: int
    blob_bytes: int
    reachable_blob_bytes: int
    unreachable_blob_bytes: int
    temporary_count: int = 0

    @property
    def total_bytes(self) -> int:
        return self.manifest_bytes + self.blob_bytes

    def as_dict(self) -> dict[str, int]:
        return {
            "manifest_count": self.manifest_count,
            "blob_count": self.blob_count,
            "reachable_blob_count": self.reachable_blob_count,
            "unreachable_blob_count": self.unreachable_blob_count,
            "manifest_bytes": self.manifest_bytes,
            "blob_bytes": self.blob_bytes,
            "reachable_blob_bytes": self.reachable_blob_bytes,
            "unreachable_blob_bytes": self.unreachable_blob_bytes,
            "temporary_count": self.temporary_count,
            "total_bytes": self.total_bytes,
        }

    def __getitem__(self, key: str) -> int:
        return self.as_dict()[key]


@dataclass(frozen=True, slots=True)
class GCReport:
    """Result of a retention collection pass."""

    kept_snapshots: tuple[str, ...]
    removed_snapshots: tuple[str, ...]
    deleted_blobs: tuple[str, ...]
    reclaimed_bytes: int
    dry_run: bool

    @property
    def deleted_blob_count(self) -> int:
        return len(self.deleted_blobs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept_snapshots": self.kept_snapshots,
            "removed_snapshots": self.removed_snapshots,
            "deleted_blobs": self.deleted_blobs,
            "reclaimed_bytes": self.reclaimed_bytes,
            "dry_run": self.dry_run,
        }


class ContentAddressedSnapshotStore:
    """Filesystem repository for structural-sharing snapshots.

    ``store`` may be supplied to the constructor, or to each ``capture``
    call.  A store is only read through its detached private state snapshot;
    no production class, schema, or commit path is changed by this experiment.
    The private state access is intentional and limited to this prototype.
    """

    def __init__(self, directory: str | os.PathLike[str], store: Any | None = None):
        self.directory = Path(directory)
        self.nodes_dir = self.directory / "nodes"
        self.manifests_dir = self.directory / "manifests"
        self.current_path = self.directory / "CURRENT"
        self.store = store
        self._lock = threading.RLock()
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)

    # Useful spelling for callers comparing this experiment with SQLiteHistory.
    @property
    def blob_dir(self) -> Path:
        return self.nodes_dir

    @property
    def manifest_dir(self) -> Path:
        return self.manifests_dir

    def blob_path(self, blob_hash: str) -> Path:
        return self.nodes_dir / f"{_check_hash(blob_hash)}.blob"

    def _state_from(self, source: Any | None) -> tuple[dict[str, Any], int | None]:
        source = self.store if source is None else source
        if source is None:
            raise TypeError("capture requires a TreeStore-like source")
        # _query_state_snapshot is available on TreeStore and its durable
        # subclasses, and takes the source lock for a coherent detached view.
        if hasattr(source, "_query_state_snapshot"):
            state = source._query_state_snapshot()
        else:
            lock = getattr(source, "_lock", None)
            if lock is None or not hasattr(source, "_state"):
                raise TypeError("source must provide a TreeStore-like state")
            with lock:
                state = copy.deepcopy(source._state)
        if not isinstance(state, dict):
            raise StorageCorruptionError("source state is not a mapping")
        _check_invariants(state)
        revision = getattr(source, "_version", None)
        if isinstance(revision, bool) or not isinstance(revision, int):
            revision = None
        return state, revision

    def _encode_node(self, record: Mapping[str, Any], children: list[str]) -> bytes:
        # parent_id is intentionally omitted: it is derived from the edge in
        # the parent blob and therefore does not invalidate moved subtrees.
        node = {
            "format": _FORMAT,
            "id": record["id"],
            "name": record["name"],
            "type": record["type"],
            "properties": _encode_value(record["properties"]),
            "children": children,
            "created_at": record["created_at"],
            "modified_at": record["modified_at"],
        }
        return _canonical_json(node)

    def _publish_nodes(self, state: Mapping[str, Any]) -> tuple[str, int]:
        nodes = state["nodes"]
        hashes: dict[str, str] = {}
        payloads: dict[str, bytes] = {}

        # State is a tree, but process defensively so malformed custom state
        # cannot recurse forever.  Existing store invariants catch normal use.
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(node_id: str) -> str:
            if node_id in done:
                return hashes[node_id]
            if node_id in visiting:
                raise StorageCorruptionError("cycle while hashing snapshot")
            if node_id not in nodes:
                raise StorageCorruptionError("node references missing child")
            visiting.add(node_id)
            record = nodes[node_id]
            child_hashes = [visit(child_id) for child_id in record["children"]]
            payload = self._encode_node(record, child_hashes)
            blob_hash = _digest(payload)
            hashes[node_id] = blob_hash
            payloads.setdefault(blob_hash, payload)
            visiting.remove(node_id)
            done.add(node_id)
            return blob_hash

        root_hash = visit(state["root_id"])
        for blob_hash, payload in payloads.items():
            _atomic_bytes(self.blob_path(blob_hash), payload)
        return root_hash, len(done)

    def _manifest_path(self, root_hash: str) -> Path:
        return self.manifests_dir / f"manifest-{_check_hash(root_hash)}.json"

    def _read_manifest(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("format") != _FORMAT:
                raise ValueError("unknown manifest format")
            root_hash = _check_hash(raw.get("root_hash"), "root hash")
            snapshot_id = raw.get("snapshot_id", root_hash)
            if snapshot_id != root_hash:
                raise ValueError("snapshot ID must equal root hash")
            expected = self._manifest_path(root_hash)
            if path.name != expected.name:
                raise ValueError("manifest filename/hash mismatch")
            revision = raw.get("revision")
            if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0):
                raise ValueError("invalid revision")
            node_count = raw.get("node_count")
            if isinstance(node_count, bool) or not isinstance(node_count, int) or node_count < 1:
                raise ValueError("invalid node count")
            created_at = raw.get("created_at")
            if not isinstance(created_at, str):
                raise ValueError("invalid creation timestamp")
            # Publication writes canonical bytes.  Reject hand-edited or
            # non-canonical JSON rather than silently accepting an artifact
            # whose immutable metadata no longer matches its publication.
            expected = {
                "format": _FORMAT,
                "snapshot_id": snapshot_id,
                "root_hash": root_hash,
                "revision": revision,
                "node_count": node_count,
                "created_at": created_at,
            }
            if _canonical_json(raw) != _canonical_json(expected):
                raise ValueError("manifest is not canonical")
            return expected
        except (OSError, json.JSONDecodeError, TypeError, ValueError, StorageCorruptionError) as exc:
            raise StorageCorruptionError(f"invalid snapshot manifest {path}: {exc}") from exc

    def _publish_manifest(self, root_hash: str, revision: int | None, node_count: int) -> Path:
        path = self._manifest_path(root_hash)
        manifest = {
            "format": _FORMAT,
            "snapshot_id": root_hash,
            "root_hash": root_hash,
            "revision": revision,
            "node_count": node_count,
            "created_at": _utc_now(),
        }
        payload = _canonical_json(manifest)
        # Existing manifests are immutable too.  Their timestamp/revision may
        # differ on a repeated capture, so preserve the first publication.
        if path.exists():
            # The root hash is the immutable identity.  A repeated capture
            # from another handle may report a different local revision even
            # though the content is identical; preserve the first manifest's
            # auxiliary metadata and treat publication as idempotent.
            self._read_manifest(path)
        else:
            _atomic_bytes(path, payload)
        return path

    def _publish_current(self, path: Path) -> None:
        # CURRENT is a replaceable reference, unlike node blobs/manifests.
        _atomic_replace(self.current_path, (path.name + "\n").encode("utf-8"))

    def capture(self, store: Any | None = None) -> Snapshot:
        """Materialize and atomically publish one source revision."""
        state, revision = self._state_from(store)
        with self._lock:
            root_hash, node_count = self._publish_nodes(state)
            manifest_path = self._publish_manifest(root_hash, revision, node_count)
            # The manifest has been fsynced and renamed before it can become
            # current.  A crash before this point leaves only GC-able blobs.
            self._publish_current(manifest_path)
            # If this root was already published, its immutable manifest may
            # carry an earlier source revision.  Return the manifest's
            # authoritative metadata rather than an in-memory revision that
            # does not describe this content-addressed artifact.
            return self._snapshot_from_path(manifest_path)

    publish = capture

    def _snapshot_from_path(self, path: Path) -> Snapshot:
        manifest = self._read_manifest(path)
        return Snapshot(
            manifest["snapshot_id"], manifest["root_hash"], manifest["revision"], path, self
        )

    def snapshots(self) -> tuple[Snapshot, ...]:
        result: list[Snapshot] = []
        for path in self.manifests_dir.glob("manifest-*.json"):
            try:
                result.append(self._snapshot_from_path(path))
            except StorageCorruptionError:
                # Enumeration is intentionally strict: a torn/invalid final
                # publication must not silently masquerade as a valid snapshot.
                raise
        result.sort(key=lambda item: ((-1 if item.revision is None else item.revision), item.snapshot_id))
        return tuple(result)

    def revisions(self) -> tuple[int, ...]:
        return tuple(s.revision for s in self.snapshots() if s.revision is not None)

    def snapshot(self, snapshot_id: str | Snapshot) -> Snapshot:
        if isinstance(snapshot_id, Snapshot):
            if snapshot_id._repository is not self:
                snapshot_id = snapshot_id.snapshot_id
            else:
                return self._snapshot_from_path(snapshot_id.manifest_path)
        if not isinstance(snapshot_id, str):
            raise TypeError("snapshot ID must be a string")
        # Accept the root hash and the complete manifest filename for easy CLI
        # use, while still requiring a canonical hash in the resulting path.
        if snapshot_id.startswith("manifest-"):
            snapshot_id = snapshot_id.removeprefix("manifest-").removesuffix(".json")
        path = self._manifest_path(snapshot_id)
        if not path.exists():
            raise KeyError(snapshot_id)
        return self._snapshot_from_path(path)

    def current(self) -> Snapshot | None:
        try:
            name = self.current_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not name:
            return None
        path = self.manifests_dir / name
        # Reject path traversal and non-manifest references.
        if Path(name).name != name or not name.startswith("manifest-"):
            raise StorageCorruptionError("invalid CURRENT reference")
        if not path.exists():
            raise StorageCorruptionError("CURRENT refers to missing manifest")
        return self._snapshot_from_path(path)

    def _load_node(self, blob_hash: str) -> dict[str, Any]:
        path = self.blob_path(blob_hash)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise StorageCorruptionError(f"missing node blob {blob_hash}") from exc
        if _digest(payload) != blob_hash:
            raise StorageCorruptionError(f"node blob hash mismatch {blob_hash}")
        try:
            raw = json.loads(payload.decode("utf-8"))
            if not isinstance(raw, dict) or raw.get("format") != _FORMAT:
                raise ValueError("unknown node format")
            expected_keys = {
                "format", "id", "name", "type", "properties", "children",
                "created_at", "modified_at",
            }
            if set(raw) != expected_keys or _canonical_json(raw) != payload:
                raise ValueError("node blob is not canonical")
            if not isinstance(raw.get("id"), str) or not raw["id"]:
                raise ValueError("invalid node ID")
            if not isinstance(raw.get("name"), str) or not isinstance(raw.get("type"), str):
                raise ValueError("invalid node fields")
            children = raw.get("children")
            if not isinstance(children, list) or not all(isinstance(x, str) for x in children):
                raise ValueError("invalid children")
            # Decode to ensure tags and property values follow GROVE semantics.
            properties = _decode_value(raw.get("properties"))
            if not isinstance(properties, dict):
                raise ValueError("properties must be an object")
            return {
                "id": raw["id"], "name": raw["name"], "type": raw["type"],
                "properties": properties, "children": list(children),
                "created_at": raw.get("created_at"), "modified_at": raw.get("modified_at"),
            }
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError,
                KeyError, InvalidPropertyError) as exc:
            raise StorageCorruptionError(f"invalid node blob {blob_hash}: {exc}") from exc

    def open(self, snapshot: Snapshot | str) -> TreeStore:
        """Validate and materialize a snapshot as a detached TreeStore."""
        item = self.snapshot(snapshot) if isinstance(snapshot, str) else snapshot
        if item._repository is not self:
            item = self.snapshot(item.snapshot_id)
        manifest = self._read_manifest(item.manifest_path)
        if manifest["root_hash"] != item.root_hash:
            raise StorageCorruptionError("snapshot object does not match manifest")
        records: dict[str, dict[str, Any]] = {}
        visiting: set[str] = set()
        seen: set[str] = set()

        def visit(blob_hash: str, parent_id: str | None, *, is_root: bool = False) -> str:
            _check_hash(blob_hash, "node hash")
            if blob_hash in visiting:
                raise StorageCorruptionError("cycle in node blobs")
            record = self._load_node(blob_hash)
            node_id = record["id"]
            if node_id in seen:
                raise StorageCorruptionError("node ID appears more than once")
            visiting.add(blob_hash)
            if node_id in records:
                raise StorageCorruptionError("duplicate node ID")
            records[node_id] = {
                "id": node_id, "name": record["name"], "type": record["type"],
                "properties": record["properties"], "parent_id": parent_id,
                "children": [], "created_at": record["created_at"],
                "modified_at": record["modified_at"],
            }
            seen.add(node_id)
            for child_hash in record["children"]:
                child_id = visit(child_hash, node_id)
                records[node_id]["children"].append(child_id)
            visiting.remove(blob_hash)
            return node_id

        root_id = visit(manifest["root_hash"], None, is_root=True)
        state = {"root_id": root_id, "nodes": records}
        try:
            _check_invariants(state)
        except Exception as exc:
            raise StorageCorruptionError(f"snapshot invariant failure: {exc}") from exc
        if len(records) != manifest["node_count"]:
            raise StorageCorruptionError("manifest node count does not match root blob")
        return TreeStore(state=state)

    load = open

    def _reachable(self, roots: Iterable[str]) -> set[str]:
        reachable: set[str] = set()
        pending = list(roots)
        while pending:
            blob_hash = pending.pop()
            _check_hash(blob_hash, "node hash")
            if blob_hash in reachable:
                continue
            record = self._load_node(blob_hash)
            reachable.add(blob_hash)
            pending.extend(record["children"])
        return reachable

    def _manifest_paths(self) -> list[Path]:
        return sorted(self.manifests_dir.glob("manifest-*.json"))

    def report(self) -> RepositoryReport:
        manifests = self._manifest_paths()
        parsed = [self._read_manifest(path) for path in manifests]
        reachable = self._reachable(m["root_hash"] for m in parsed) if parsed else set()
        blobs = list(self.nodes_dir.glob("*.blob"))
        blob_sizes = {p.stem: p.stat().st_size for p in blobs}
        reachable_bytes = sum(blob_sizes.get(h, 0) for h in reachable)
        blob_bytes = sum(blob_sizes.values())
        temporary = list(self.nodes_dir.glob(".*.tmp")) + list(self.manifests_dir.glob(".*.tmp"))
        return RepositoryReport(
            manifest_count=len(manifests), blob_count=len(blobs),
            reachable_blob_count=sum(1 for p in blobs if p.stem in reachable),
            unreachable_blob_count=sum(1 for p in blobs if p.stem not in reachable),
            manifest_bytes=sum(p.stat().st_size for p in manifests),
            blob_bytes=blob_bytes, reachable_blob_bytes=reachable_bytes,
            unreachable_blob_bytes=blob_bytes - reachable_bytes,
            temporary_count=len(temporary),
        )

    def gc(self, keep: int | Iterable[str | Snapshot] | None = None, *, dry_run: bool = False) -> GCReport:
        """Collect old manifests and unreachable blobs.

        ``keep=None`` keeps every published manifest (safe audit mode).
        An integer keeps the newest N revisions.  An iterable keeps the named
        snapshot IDs.  Unkept manifests are removed along with blobs no longer
        reachable from any kept root.  ``dry_run`` reports exactly what would
        be removed without changing the repository.
        """
        with self._lock:
            paths = self._manifest_paths()
            items = [self._snapshot_from_path(path) for path in paths]
            items.sort(key=lambda s: ((-1 if s.revision is None else s.revision), s.snapshot_id))
            if keep is None:
                retained = items
            elif isinstance(keep, bool):
                raise TypeError("keep must be a non-negative integer or snapshot IDs")
            elif isinstance(keep, int):
                if keep < 0:
                    raise ValueError("keep must be non-negative")
                retained = items[-keep:] if keep else []
            else:
                wanted = set()
                for value in keep:
                    wanted.add(value.snapshot_id if isinstance(value, Snapshot) else value)
                retained = [item for item in items if item.snapshot_id in wanted]
            retained_ids = tuple(item.snapshot_id for item in retained)
            retained_roots = [item.root_hash for item in retained]
            reachable = self._reachable(retained_roots) if retained_roots else set()
            remove_items = [item for item in items if item.snapshot_id not in set(retained_ids)]
            blobs = []
            for path in self.nodes_dir.glob("*.blob"):
                if path.stem not in reachable:
                    blobs.append(path)
            reclaimed = sum(path.stat().st_size for path in blobs)
            # Validate and remember CURRENT before deleting manifests; after a
            # delete it may legitimately point at a manifest being retired.
            old_current = self.current() if not dry_run else None
            if not dry_run:
                for item in remove_items:
                    try:
                        item.manifest_path.unlink()
                    except FileNotFoundError:
                        pass
                for path in blobs:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                # Keep CURRENT useful after retention, or remove it when no
                # snapshots remain.  This update is itself atomic.
                if old_current is None or old_current.snapshot_id not in set(retained_ids):
                    if retained:
                        self._publish_current(retained[-1].manifest_path)
                    else:
                        try:
                            self.current_path.unlink()
                        except FileNotFoundError:
                            pass
                _fsync_directory(self.directory)
            return GCReport(
                tuple(retained_ids), tuple(item.snapshot_id for item in remove_items),
                tuple(path.stem for path in blobs), reclaimed, dry_run,
            )

    collect = gc

    def delete_snapshot(self, snapshot: str | Snapshot) -> None:
        """Drop one manifest; call :meth:`gc` to reclaim now-unreferenced blobs."""
        item = self.snapshot(snapshot)
        old_current = self.current()
        try:
            item.manifest_path.unlink()
        except FileNotFoundError:
            return
        if old_current is not None and old_current.snapshot_id == item.snapshot_id:
            remaining = self.snapshots()
            if remaining:
                self._publish_current(remaining[-1].manifest_path)
            else:
                try:
                    self.current_path.unlink()
                except FileNotFoundError:
                    pass


# Friendly aliases; keeping names here costs nothing and makes the disposable
# experiment easy to discover without adding anything to grove.__init__.
ContentAddressedSnapshots = ContentAddressedSnapshotStore
StructuralSharingSnapshots = ContentAddressedSnapshotStore
# Additional descriptive spellings for notebooks and one-off benchmark code.
SnapshotStore = ContentAddressedSnapshotStore
ContentAddressedStore = ContentAddressedSnapshotStore


def demo() -> None:
    """Run a tiny structural-sharing and GC demonstration."""
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        source = TreeStore()
        source.create("left")
        source.create("right")
        repo = ContentAddressedSnapshotStore(directory)
        first = repo.capture(source)
        source.rename("/left", "moved")
        second = repo.capture(source)
        print("snapshots:", first.snapshot_id[:12], second.snapshot_id[:12])
        print("repository:", repo.report().as_dict())
        print("retained only newest:", repo.gc(keep=1).as_dict())


if __name__ == "__main__":
    demo()

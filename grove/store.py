"""In-memory and append-only persistent stores for GROVE.

The first vertical slice intentionally uses a whole-state snapshot per commit.
It is not intended for very large datasets; the format is deliberately simple
so crash recovery and tree invariants are easy to test before optimizing.
"""
from __future__ import annotations

import base64
import copy
import datetime as _dt
import json
import math
import os
import struct
import threading
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import (AlreadyExistsError, InvalidOperationError,
                     InvalidPropertyError, NotFoundError,
                     StorageCorruptionError)
from .model import Node
from .types import Reference
from .schema import Schema, SchemaValidationError

_MAGIC = b"GROV1\0"
_HEADER = struct.Struct(">6sQ")
_CRC = struct.Struct(">I")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


def _validate_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidOperationError("timestamps must be ISO-8601 strings")
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidOperationError("timestamps must be ISO-8601 strings") from exc
    if parsed.tzinfo is None:
        raise InvalidOperationError("timestamps must include a timezone")
    return value


def _validate_name(name: str, *, root: bool = False) -> str:
    if not isinstance(name, str):
        raise InvalidOperationError("node name must be a string")
    if root:
        if name != "":
            raise InvalidOperationError("the root name is empty")
        return name
    if not name or "/" in name or "\x00" in name or name in (".", ".."):
        raise InvalidOperationError("node names must be non-empty path components")
    return name


def _clone_value(value: Any, seen: set[int] | None = None) -> Any:
    """Validate and detach a supported property value."""
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (bool, int, str, bytes, Reference)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidPropertyError("NaN and infinity are not valid properties")
        return value
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            raise InvalidPropertyError("timestamps must be timezone-aware")
        return value
    oid = id(value)
    if oid in seen:
        raise InvalidPropertyError("cyclic property containers are not supported")
    seen.add(oid)
    try:
        if isinstance(value, (list, tuple)):
            return [_clone_value(x, seen) for x in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InvalidPropertyError("map keys must be strings")
                result[key] = _clone_value(item, seen)
            return result
    finally:
        seen.remove(oid)
    raise InvalidPropertyError(f"unsupported property type: {type(value).__name__}")


def _clone_properties(properties: Mapping[str, Any] | None) -> dict[str, Any]:
    if properties is None:
        return {}
    if not isinstance(properties, Mapping):
        raise InvalidPropertyError("properties must be a mapping")
    result = _clone_value(dict(properties))
    # The outer map is required to have string keys as well.
    assert isinstance(result, dict)
    return result


def _remap_value(value: Any, id_map: Mapping[str, str]) -> Any:
    """Copy a property value, rewriting references whose IDs were remapped."""
    if isinstance(value, Reference):
        return Reference(id_map.get(value.node_id, value.node_id))
    if isinstance(value, list):
        return [_remap_value(item, id_map) for item in value]
    if isinstance(value, dict):
        return {key: _remap_value(item, id_map) for key, item in value.items()}
    return value


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"$grove": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            raise InvalidPropertyError("timestamps must be timezone-aware")
        return {"$grove": "timestamp", "value": value.isoformat()}
    if isinstance(value, Reference):
        return {"$grove": "reference", "id": value.node_id}
    if isinstance(value, list):
        return [_encode_value(x) for x in value]
    if isinstance(value, dict):
        return {key: _encode_value(item) for key, item in value.items()}
    raise InvalidPropertyError(f"unsupported property type: {type(value).__name__}")


def _decode_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_value(x) for x in value]
    if isinstance(value, dict):
        tag = value.get("$grove")
        if tag == "bytes" and set(value) == {"$grove", "base64"}:
            try:
                encoded = value["base64"]
                if not isinstance(encoded, str): raise ValueError("not a string")
                return base64.b64decode(encoded.encode("ascii"), validate=True)
            except (TypeError, ValueError, UnicodeEncodeError, base64.binascii.Error) as exc:
                raise InvalidPropertyError("invalid encoded bytes") from exc
        if tag == "timestamp" and set(value) == {"$grove", "value"}:
            try:
                result = _dt.datetime.fromisoformat(value["value"])
            except (TypeError, ValueError) as exc:
                raise InvalidPropertyError("invalid encoded timestamp") from exc
            if result.tzinfo is None:
                raise InvalidPropertyError("encoded timestamp must have timezone")
            return result
        if tag == "reference" and set(value) == {"$grove", "id"}:
            ref_id = value["id"]
            if (not isinstance(ref_id, str) or not ref_id or "/" in ref_id or "\x00" in ref_id):
                raise InvalidPropertyError("invalid reference ID")
            try:
                return Reference(ref_id)
            except (TypeError, ValueError) as exc:
                raise InvalidPropertyError("invalid reference ID") from exc
        if "$grove" in value:
            raise InvalidPropertyError("unknown or malformed $grove value")
        return {key: _decode_value(item) for key, item in value.items()}
    return value


def _validate_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id or "/" in node_id or "\x00" in node_id:
        raise InvalidOperationError("node IDs must be non-empty opaque strings")
    return node_id


def _record_to_node(record: dict[str, Any]) -> Node:
    return Node(record["id"], record["name"], record["type"],
                copy.deepcopy(record["properties"]), record["parent_id"],
                tuple(record["children"]), record["created_at"], record["modified_at"])


def _state_copy(state: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(state)


def _new_state() -> dict[str, Any]:
    root_id = str(uuid.uuid4())
    now = _now()
    return {"root_id": root_id, "nodes": {root_id: {
        "id": root_id, "name": "", "type": "root", "properties": {},
        "parent_id": None, "children": [], "created_at": now, "modified_at": now,
    }}}


def _snapshot_json(state: dict[str, Any]) -> bytes:
    payload = {"format": 1, "root_id": state["root_id"], "nodes": {}}
    for node_id, record in state["nodes"].items():
        payload["nodes"][node_id] = {
            "id": record["id"], "name": record["name"], "type": record["type"],
            "properties": _encode_value(record["properties"]),
            "parent_id": record["parent_id"], "children": record["children"],
            "created_at": record["created_at"], "modified_at": record["modified_at"],
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _snapshot_from_json(payload: bytes) -> dict[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("snapshot must be a JSON object")
        if raw.get("format") != 1 or not isinstance(raw.get("nodes"), dict):
            raise ValueError("unknown snapshot format")
        root_id = _validate_id(raw["root_id"])
        nodes: dict[str, dict[str, Any]] = {}
        for key, item in raw["nodes"].items():
            if not isinstance(item, dict) or key != item.get("id"):
                raise ValueError("invalid node record")
            node_id = _validate_id(item["id"])
            name = _validate_name(item.get("name", ""), root=(node_id == root_id))
            typ = item.get("type")
            if not isinstance(typ, str) or not typ:
                raise ValueError("invalid node type")
            decoded_properties = _decode_value(item.get("properties", {}))
            if not isinstance(decoded_properties, Mapping):
                raise InvalidPropertyError("properties must be a mapping")
            props = _clone_properties(decoded_properties)
            parent = item.get("parent_id")
            if parent is not None:
                _validate_id(parent)
            children = item.get("children")
            if not isinstance(children, list) or any(not isinstance(x, str) for x in children):
                raise ValueError("invalid children")
            created, modified = item.get("created_at"), item.get("modified_at")
            _validate_timestamp(created)
            _validate_timestamp(modified)
            nodes[node_id] = {"id": node_id, "name": name, "type": typ,
                "properties": props, "parent_id": parent, "children": list(children),
                "created_at": created, "modified_at": modified}
        state = {"root_id": root_id, "nodes": nodes}
        _check_invariants(state)
        return state
    except (KeyError, TypeError, ValueError, InvalidOperationError, InvalidPropertyError) as exc:
        raise StorageCorruptionError(f"invalid snapshot: {exc}") from exc


def _check_invariants(state: dict[str, Any]) -> None:
    root_id, nodes = state.get("root_id"), state.get("nodes")
    if not isinstance(root_id, str) or not isinstance(nodes, dict) or root_id not in nodes:
        raise InvalidOperationError("state must contain one root")
    root = nodes[root_id]
    if root["parent_id"] is not None or root["name"] != "":
        raise InvalidOperationError("root must have no parent and empty name")
    seen_children: set[str] = set()
    for node_id, record in nodes.items():
        if record["id"] != node_id:
            raise InvalidOperationError("node ID map mismatch")
        _validate_name(record["name"], root=node_id == root_id)
        _validate_id(node_id)
        _validate_timestamp(record["created_at"])
        _validate_timestamp(record["modified_at"])
        # ISO-8601 strings with different offsets do not sort by instant.
        # Compare parsed timezone-aware values so an offset cannot make a
        # modification that predates creation appear newer lexicographically.
        created_at = _dt.datetime.fromisoformat(record["created_at"])
        modified_at = _dt.datetime.fromisoformat(record["modified_at"])
        if modified_at < created_at:
            raise InvalidOperationError("modified_at cannot precede created_at")
        if record["parent_id"] is not None and record["parent_id"] not in nodes:
            raise InvalidOperationError("missing parent")
        children = record["children"]
        if len(children) != len(set(children)):
            raise InvalidOperationError("duplicate child")
        names: set[str] = set()
        for child_id in children:
            if child_id not in nodes:
                raise InvalidOperationError("missing child")
            child = nodes[child_id]
            if child["parent_id"] != node_id:
                raise InvalidOperationError("parent/child mismatch")
            if child["name"] in names:
                raise InvalidOperationError("duplicate sibling name")
            names.add(child["name"])
            if child_id in seen_children:
                raise InvalidOperationError("child appears under multiple parents")
            seen_children.add(child_id)
    if seen_children != set(nodes) - {root_id}:
        raise InvalidOperationError("disconnected node")
    # Parent pointers plus exactly one root imply acyclic reachability, but walk
    # explicitly to make corruption diagnostics clear.
    for node_id in nodes:
        visited: set[str] = set()
        current = node_id
        while current is not None:
            if current in visited:
                raise InvalidOperationError("cycle in primary hierarchy")
            visited.add(current)
            current = nodes[current]["parent_id"]


def _path_parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise InvalidOperationError("paths must be absolute")
    if path == "/":
        return []
    if path.endswith("/") or "//" in path:
        raise InvalidOperationError("paths cannot contain empty components")
    parts = path[1:].split("/")
    for part in parts:
        _validate_name(part)
    return parts


@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    node_id: str
    path: str


class Subscription:
    def __init__(self, store: "TreeStore", callback: Callable[[Change], None], node_id: str | None, recursive: bool):
        self._store, self._callback, self._node_id, self._recursive = store, callback, node_id, recursive
        self._active = True
    def close(self) -> None:
        if self._active:
            self._active = False
            self._store._subscriptions.discard(self)
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def _matches(self, change: Change, old_state: dict[str, Any], new_state: dict[str, Any]) -> bool:
        if not self._active or self._node_id is None: return self._active
        if change.node_id == self._node_id: return True
        if not self._recursive: return False
        # Check both sides: deletes disappear from the new state and moves can
        # leave the subscribed subtree. This preserves a useful event contract
        # without emitting one event per descendant.
        for state in (old_state, new_state):
            current = state["nodes"].get(change.node_id)
            while current and current["parent_id"] is not None:
                if current["parent_id"] == self._node_id: return True
                current = state["nodes"].get(current["parent_id"])
        return False


class TreeStore:
    """Thread-safe in-memory tree with optimistic atomic transactions.

    ``schema`` is optional.  Pass a :class:`~grove.schema.Schema` (or its
    type-to-declaration mapping) to validate node types and properties on every
    create, update, and import operation.
    """
    def __init__(self, *, state: dict[str, Any] | None = None,
                 schema: Schema | Mapping[str, Any] | None = None):
        self._lock = threading.RLock()
        self._schema = schema if isinstance(schema, Schema) else Schema(schema)
        self._state = _state_copy(state) if state is not None else _new_state()
        _check_invariants(self._state)
        # Existing state supplied to a schema-configured store must not bypass
        # validation.  This also makes custom state loading fail before the
        # store becomes observable.
        for _record in self._state["nodes"].values():
            self._schema.validate(_record["type"], _record["properties"],
                                  node_name=_record["name"])
        self._version = 0
        self._subscriptions: set[Subscription] = set()
        # Secondary indexes are lightweight, lazily rebuilt views.  Keeping
        # them on the store lets callers reuse an index while preserving the
        # immutable snapshot semantics of each query.
        self._indexes: dict[str, Any] = {}

    @property
    def root(self) -> Node:
        return self.get("/")

    @property
    def schema(self) -> Schema:
        """The store's immutable-by-convention validation schema."""
        return self._schema

    def set_schema(self, schema: Schema | Mapping[str, Any] | None) -> Schema:
        """Replace the schema after validating the complete current state.

        The replacement is atomic: an invalid schema or existing node leaves
        the previous schema installed.
        """
        candidate = schema if isinstance(schema, Schema) else Schema(schema)
        with self._lock:
            for _record in self._state["nodes"].values():
                candidate.validate(_record["type"], _record["properties"],
                                   node_name=_record["name"])
            self._schema = candidate
            return candidate

    configure_schema = set_schema

    def _resolve(self, state: dict[str, Any], target: str | Node) -> str:
        if isinstance(target, Node):
            target = target.id
        if not isinstance(target, str):
            raise NotFoundError(target)
        if target.startswith("/"):
            current = state["root_id"]
            for part in _path_parts(target):
                found = None
                for child_id in state["nodes"][current]["children"]:
                    if state["nodes"][child_id]["name"] == part:
                        found = child_id; break
                if found is None: raise NotFoundError(target)
                current = found
            return current
        if target not in state["nodes"]: raise NotFoundError(target)
        return target

    def get(self, target: str | Node) -> Node:
        with self._lock:
            return _record_to_node(self._state["nodes"][self._resolve(self._state, target)])
    read = get

    def exists(self, target: str | Node) -> bool:
        try: self.get(target); return True
        except (NotFoundError, InvalidOperationError): return False

    def path(self, target: str | Node) -> str:
        with self._lock:
            node_id = self._resolve(self._state, target)
            names=[]; current=node_id
            while current != self._state["root_id"]:
                rec=self._state["nodes"][current]; names.append(rec["name"]); current=rec["parent_id"]
            return "/" + "/".join(reversed(names))

    def _query_state_snapshot(self) -> dict[str, Any]:
        """Return one coherent detached state for the query layer."""
        with self._lock:
            return _state_copy(self._state)

    def query(self, target: str | Node = "/", *, recursive: bool = True,
              include_root: bool = False, predicate=None, **criteria):
        """Create a query over a detached snapshot of ``target``.

        By default the target itself is excluded (the root sentinel is thus
        never returned) and its direct children are traversed in stored order.
        Set ``include_root=True`` to include the target.  ``predicate`` may be
        a callable receiving a :class:`Node` or a mapping of typed property
        values; keyword criteria are equivalent mapping entries and ``type``
        filters node type.
        """
        from .query import Query
        state = self._query_state_snapshot()
        node_id = self._resolve(state, target)
        if criteria:
            if predicate is None:
                predicate = criteria
            else:
                # Query.where handles conjunction, while keeping this method
                # convenient for callers using both forms.
                return Query._from_snapshot(
                    state, node_id, recursive=recursive,
                    include_root=include_root, predicate=predicate,
                ).where(criteria)
        return Query._from_snapshot(
            state, node_id, recursive=recursive,
            include_root=include_root, predicate=predicate,
        )

    def index_property(self, property_name: str):
        """Create or return a lazy secondary index for ``property_name``."""
        from .query import PropertyIndex
        with self._lock:
            # SQLite handles have a real resource lifecycle.  Keep index
            # creation consistent with all other public operations instead of
            # handing out an index that can never be used after close().
            ensure_open = getattr(self, "_ensure_open", None)
            if ensure_open is not None:
                ensure_open()
            index = self._indexes.get(property_name)
            if index is None:
                index = PropertyIndex(self, property_name)
                self._indexes[property_name] = index
            return index

    # Explicit aliases make the small index API discoverable without changing
    # any existing CRUD operation signatures.
    create_index = index_property
    index = index_property
    find = query

    def drop_index(self, property_name: str) -> None:
        with self._lock:
            ensure_open = getattr(self, "_ensure_open", None)
            if ensure_open is not None:
                ensure_open()
            self._indexes.pop(property_name, None)

    def transaction(self) -> "Transaction":
        with self._lock:
            return Transaction(self, self._version, _state_copy(self._state), self._schema)

    def _mutate(self, method: str, *args, **kwargs):
        with self.transaction() as tx:
            result = getattr(tx, method)(*args, **kwargs)
        return result

    def create(self, *args, **kwargs): return self._mutate("create", *args, **kwargs)
    def update(self, *args, **kwargs): return self._mutate("update", *args, **kwargs)
    def rename(self, *args, **kwargs): return self._mutate("rename", *args, **kwargs)
    def move(self, *args, **kwargs): return self._mutate("move", *args, **kwargs)
    def delete(self, *args, **kwargs): return self._mutate("delete", *args, **kwargs)
    def copy(self, *args, **kwargs): return self._mutate("copy", *args, **kwargs)
    def import_tree(self, *args, **kwargs): return self._mutate("import_tree", *args, **kwargs)

    def export(self, target: str | Node = "/") -> dict[str, Any]:
        with self._lock:
            node_id = self._resolve(self._state, target)
            def rec(nid: str) -> dict[str, Any]:
                n=self._state["nodes"][nid]
                return {"id": n["id"], "name": n["name"], "type": n["type"],
                    "properties": _encode_value(n["properties"]),
                    # A subtree export is self-contained: its exported root
                    # has no parent even when the live node does.
                    "parent_id": None if nid == node_id else n["parent_id"],
                    "created_at": n["created_at"], "modified_at": n["modified_at"],
                    "children": [rec(cid) for cid in n["children"]]}
            return rec(node_id)

    def export_json(self, target: str | Node = "/", *, indent: int | None = 2) -> str:
        return json.dumps(self.export(target), ensure_ascii=False, indent=indent)

    def subscribe(self, callback: Callable[[Change], None], node: str | Node | None = None, *, recursive: bool = True) -> Subscription:
        with self._lock:
            node_id = None if node is None else self._resolve(self._state, node)
            sub=Subscription(self, callback, node_id, recursive); self._subscriptions.add(sub); return sub

    def _commit(self, tx: "Transaction") -> None:
        with self._lock:
            if tx._base_version != self._version:
                raise InvalidOperationError("transaction conflict: store changed since transaction began")
            _check_invariants(tx._state)
            for _record in tx._state["nodes"].values():
                self._schema.validate(_record["type"], _record["properties"],
                                      node_name=_record["name"])
            old=self._state
            self._state = tx._state
            self._version += 1
            changes = tx._changes
            subscriptions = tuple(self._subscriptions)
        for change in changes:
            for sub in subscriptions:
                if sub._matches(change, old, self._state):
                    try: sub._callback(change)
                    except Exception: pass


class Transaction:
    def __init__(self, store: TreeStore, base_version: int, state: dict[str, Any],
                 schema: Schema | None = None):
        self._store, self._base_version, self._state = store, base_version, state
        self._schema = schema if schema is not None else store.schema
        self._changes: list[Change] = []
        self._done = False

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None: self.rollback()
        elif not self._done: self.commit()
        return False

    def _ensure_open(self):
        if self._done: raise InvalidOperationError("transaction is closed")
    def _resolve(self, target):
        self._ensure_open()
        return self._store._resolve(self._state, target)
    def get(self, target): return _record_to_node(self._state["nodes"][self._resolve(target)])
    read = get
    def path(self, target):
        self._ensure_open()
        nid=self._resolve(target); names=[]
        while nid != self._state["root_id"]:
            n=self._state["nodes"][nid]; names.append(n["name"]); nid=n["parent_id"]
        return "/" + "/".join(reversed(names))

    def _query_state_snapshot(self):
        """Return the transaction's detached staged state for queries."""
        self._ensure_open()
        return _state_copy(self._state)

    def query(self, target: str | Node = "/", *, recursive: bool = True,
              include_root: bool = False, predicate=None, **criteria):
        """Query staged transaction state without observing later commits."""
        self._ensure_open()
        from .query import Query
        state = _state_copy(self._state)
        node_id = self._resolve(target)
        query = Query._from_snapshot(
            state, node_id, recursive=recursive, include_root=include_root,
            predicate=predicate,
        )
        return query.where(criteria) if criteria else query

    find = query

    def _touch(self, nid: str, kind: str):
        self._state["nodes"][nid]["modified_at"] = _now()
        self._changes.append(Change(kind, nid, self.path(nid)))

    def create(self, name: str, *, parent: str | Node = "/", type: str = "object", properties: Mapping[str, Any] | None = None, node_id: str | None = None, index: int | None = None) -> Node:
        self._ensure_open(); parent_id=self._resolve(parent); _validate_name(name)
        if not isinstance(type, str) or not type: raise InvalidOperationError("type must be a non-empty string")
        if node_id is None: node_id=str(uuid.uuid4())
        _validate_id(node_id)
        if node_id in self._state["nodes"]: raise AlreadyExistsError(node_id)
        parent_rec=self._state["nodes"][parent_id]
        if any(self._state["nodes"][cid]["name"] == name for cid in parent_rec["children"]): raise AlreadyExistsError(name)
        if index is not None and (not isinstance(index,int) or index < 0 or index > len(parent_rec["children"])):
            raise InvalidOperationError("index out of range")
        cloned_properties = _clone_properties(properties)
        self._schema.validate(type, cloned_properties, node_name=name)
        now=_now(); record={"id":node_id,"name":name,"type":type,"properties":cloned_properties,"parent_id":parent_id,"children":[],"created_at":now,"modified_at":now}
        self._state["nodes"][node_id]=record
        children=parent_rec["children"]
        if index is None: children.append(node_id)
        else:
            if not isinstance(index,int) or index < 0 or index > len(children): raise InvalidOperationError("index out of range")
            children.insert(index,node_id)
        self._touch(parent_id,"create"); self._changes.append(Change("create",node_id,self.path(node_id)))
        return _record_to_node(record)

    def update(self, target, *, properties: Mapping[str, Any] | None = None, type: str | None = None, merge: bool = False) -> Node:
        self._ensure_open(); nid=self._resolve(target); rec=self._state["nodes"][nid]
        if type is not None and (not isinstance(type,str) or not type):
            raise InvalidOperationError("type must be non-empty")
        if properties is None and type is None:
            return _record_to_node(rec)
        new_props = _clone_properties(properties) if properties is not None else None
        if new_props is not None:
            if merge:
                merged=copy.deepcopy(rec["properties"]); merged.update(new_props); rec["properties"]=merged
            else: rec["properties"]=new_props
        candidate_type = type if type is not None else rec["type"]
        candidate_properties = rec["properties"]
        self._schema.validate(candidate_type, candidate_properties,
                              node_name=rec["name"])
        if type is not None: rec["type"]=type
        self._touch(nid,"update"); return _record_to_node(rec)

    def rename(self, target, name: str) -> Node:
        self._ensure_open(); nid=self._resolve(target); rec=self._state["nodes"][nid]
        if nid == self._state["root_id"]: raise InvalidOperationError("cannot rename root")
        _validate_name(name); parent=self._state["nodes"][rec["parent_id"]]
        if name != rec["name"] and any(self._state["nodes"][cid]["name"] == name for cid in parent["children"]): raise AlreadyExistsError(name)
        rec["name"]=name; self._touch(nid,"rename"); self._touch(parent["id"],"rename"); return _record_to_node(rec)

    def move(self, target, parent: str | Node, *, name: str | None = None, index: int | None = None) -> Node:
        self._ensure_open(); nid=self._resolve(target); rec=self._state["nodes"][nid]; new_parent_id=self._resolve(parent)
        if nid == self._state["root_id"]: raise InvalidOperationError("cannot move root")
        if nid == new_parent_id: raise InvalidOperationError("cannot move node into itself")
        current=new_parent_id
        while current != self._state["root_id"]:
            current=self._state["nodes"][current]["parent_id"]
            if current == nid: raise InvalidOperationError("cannot move node into its descendant")
        if name is None: name=rec["name"]
        _validate_name(name); destination=self._state["nodes"][new_parent_id]
        if any(cid != nid and self._state["nodes"][cid]["name"] == name for cid in destination["children"]): raise AlreadyExistsError(name)
        old_parent=self._state["nodes"][rec["parent_id"]]
        same_parent = new_parent_id == old_parent["id"]
        current_index = old_parent["children"].index(nid)
        available = len(destination["children"]) - (1 if same_parent else 0)
        if index is not None and (not isinstance(index,int) or index < 0 or index > available):
            raise InvalidOperationError("index out of range")
        if index is None:
            index = available
        # In a same-parent move, the requested index addresses the final list
        # after removal. Treat an unchanged name/position as a true no-op.
        if same_parent and index == current_index and name == rec["name"]:
            return _record_to_node(rec)
        old_parent["children"].remove(nid)
        destination["children"].insert(index,nid); rec["parent_id"]=new_parent_id; rec["name"]=name
        self._touch(nid,"move"); self._touch(old_parent["id"],"move")
        if destination["id"] != old_parent["id"]: self._touch(destination["id"],"move")
        return _record_to_node(rec)

    def delete(self, target, *, recursive: bool = False) -> None:
        self._ensure_open(); nid=self._resolve(target)
        if nid == self._state["root_id"]: raise InvalidOperationError("cannot delete root")
        rec=self._state["nodes"][nid]
        if rec["children"] and not recursive: raise InvalidOperationError("node has children; use recursive=True")
        parent=self._state["nodes"][rec["parent_id"]]; parent["children"].remove(nid)
        doomed=[]
        def walk(x):
            doomed.append(x)
            for c in self._state["nodes"][x]["children"]: walk(c)
        walk(nid)
        for x in doomed: del self._state["nodes"][x]
        self._touch(parent["id"],"delete"); self._changes.append(Change("delete",nid,self.path_from_parts(parent["id"],rec["name"])))

    def path_from_parts(self, parent_id: str, child_name: str) -> str:
        return self.path(parent_id).rstrip("/") + "/" + child_name

    def copy(self, target, parent: str | Node, *, name: str | None = None, preserve_ids: bool = False, index: int | None = None) -> Node:
        self._ensure_open(); source_id=self._resolve(target); source=self._state["nodes"][source_id]; parent_id=self._resolve(parent)
        if source_id == self._state["root_id"] and name is None: raise InvalidOperationError("copied root needs a name")
        id_map={}
        def collect(old):
            id_map[old] = old if preserve_ids else str(uuid.uuid4())
            for child in self._state["nodes"][old]["children"]: collect(child)
        collect(source_id)
        if any(x in self._state["nodes"] for x in id_map.values()): raise AlreadyExistsError("copied ID already exists")
        root_name = source["name"] if name is None else name; _validate_name(root_name)
        dest=self._state["nodes"][parent_id]
        if any(self._state["nodes"][c]["name"] == root_name for c in dest["children"]): raise AlreadyExistsError(root_name)
        if index is not None and (not isinstance(index,int) or index < 0 or index > len(dest["children"])):
            raise InvalidOperationError("index out of range")
        def clone(old, p):
            original=self._state["nodes"][old]; nid=id_map[old]; now=_now()
            self._state["nodes"][nid]={"id":nid,"name":root_name if old==source_id else original["name"],"type":original["type"],"properties":_remap_value(original["properties"], id_map),"parent_id":p,"children":[],"created_at":now,"modified_at":now}
            for child in original["children"]:
                clone(child,nid); self._state["nodes"][nid]["children"].append(id_map[child])
        clone(source_id,parent_id)
        if index is None: dest["children"].append(id_map[source_id])
        else: dest["children"].insert(index,id_map[source_id])
        self._touch(parent_id,"copy"); self._changes.append(Change("copy",id_map[source_id],self.path(id_map[source_id])))
        return _record_to_node(self._state["nodes"][id_map[source_id]])

    def import_tree(self, data: Mapping[str, Any] | str, *, parent: str | Node = "/", preserve_ids: bool = True, name: str | None = None, index: int | None = None) -> Node:
        self._ensure_open(); parent_id=self._resolve(parent)
        if isinstance(data,str):
            try: data=json.loads(data)
            except json.JSONDecodeError as exc: raise InvalidOperationError("invalid tree JSON") from exc
        if not isinstance(data, Mapping): raise InvalidOperationError("tree must be a mapping")
        required={"id","name","type","properties","children"}
        def validate(raw, is_root=False, expected_parent=None):
            if not isinstance(raw,Mapping) or not required.issubset(raw): raise InvalidOperationError("malformed tree node")
            oldid=_validate_id(raw["id"]); nm=_validate_name(raw["name"],root=False if not is_root else raw["name"] == "")
            claimed_parent = raw.get("parent_id")
            if claimed_parent is not None:
                _validate_id(claimed_parent)
            if is_root and claimed_parent is not None:
                raise InvalidOperationError("import root must have no parent")
            if expected_parent is not None and claimed_parent is not None and claimed_parent != expected_parent:
                raise InvalidOperationError("child parent_id does not match its parent")
            if not isinstance(raw["type"],str) or not raw["type"]: raise InvalidOperationError("invalid type")
            decoded_properties = _decode_value(raw["properties"])
            if not isinstance(decoded_properties, Mapping):
                raise InvalidPropertyError("properties must be a mapping")
            props=_clone_properties(decoded_properties)
            self._schema.validate(raw["type"], props, node_name=raw.get("name"))
            if "created_at" in raw: _validate_timestamp(raw["created_at"])
            if "modified_at" in raw: _validate_timestamp(raw["modified_at"])
            children=raw["children"]
            if not isinstance(children,list): raise InvalidOperationError("children must be a list")
            child_names=set()
            for child in children:
                if not isinstance(child,Mapping) or child.get("name") in child_names: raise InvalidOperationError("duplicate child name")
                child_names.add(child.get("name")); validate(child, expected_parent=oldid)
            return oldid,nm,raw["type"],props,children
        root_export = data.get("name") == "" and data.get("parent_id") is None
        validate(data, True)
        all_ids=[]
        def ids(raw):
            all_ids.append(raw["id"])
            for child in raw["children"]: ids(child)
        ids(data)
        if len(all_ids)!=len(set(all_ids)): raise InvalidOperationError("duplicate IDs in import")
        id_map={old: (old if preserve_ids else str(uuid.uuid4())) for old in all_ids}
        root_name=data["name"] if name is None else name
        if root_export:
            # A complete export may replace an empty destination database,
            # preserving the exported root ID when requested. Replacing a
            # non-empty root is deliberately forbidden so import remains an
            # atomic, unsurprising operation.
            if name is not None or index is not None or parent_id != self._state["root_id"]:
                raise InvalidOperationError("a complete tree can only import into an empty database root")
            dest=self._state["nodes"][parent_id]
            if dest["children"]:
                raise AlreadyExistsError("cannot replace a non-empty database root")
            old_root_id=self._state["root_id"]
            if any(nid in self._state["nodes"] and nid != old_root_id for nid in id_map.values()):
                raise AlreadyExistsError("import ID already exists")
            self._state={"root_id": id_map[data["id"]], "nodes": {}}
            parent_for_import=None
        else:
            _validate_name(root_name)
            dest=self._state["nodes"][parent_id]
            if any(nid in self._state["nodes"] for nid in id_map.values()): raise AlreadyExistsError("import ID already exists")
            if any(self._state["nodes"][c]["name"] == root_name for c in dest["children"]): raise AlreadyExistsError(root_name)
            if index is not None and (not isinstance(index,int) or index < 0 or index > len(dest["children"])):
                raise InvalidOperationError("index out of range")
            parent_for_import=parent_id
        def add(raw,pid,isroot=False):
            oldid,nm,typ,props,children=validate(raw,isroot); nid=id_map[oldid]; now=_now()
            props = _remap_value(props, id_map)
            created = raw.get("created_at", now)
            modified = raw.get("modified_at", now)
            _validate_timestamp(created); _validate_timestamp(modified)
            self._state["nodes"][nid]={"id":nid,"name":root_name if isroot else nm,"type":typ,"properties":props,"parent_id":pid,"children":[],"created_at":created,"modified_at":modified}
            for child in children:
                cid=add(child,nid); self._state["nodes"][nid]["children"].append(cid)
            return nid
        new_id=add(data,parent_for_import,True)
        if not root_export:
            if index is None: dest["children"].append(new_id)
            else: dest["children"].insert(index,new_id)
            self._touch(parent_id,"import")
        self._changes.append(Change("import",new_id,self.path(new_id)))
        return _record_to_node(self._state["nodes"][new_id])

    def commit(self):
        self._ensure_open(); self._store._commit(self); self._done=True
    def rollback(self): self._done=True



def _atomic_operation(method):
    """Make a caught operation failure non-mutating within a transaction."""
    def wrapped(self, *args, **kwargs):
        self._ensure_open()
        before_state = _state_copy(self._state)
        before_changes = list(self._changes)
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            self._state = before_state
            self._changes = before_changes
            raise
    wrapped.__name__ = method.__name__
    wrapped.__doc__ = method.__doc__
    return wrapped


for _operation_name in ("create", "update", "rename", "move", "delete", "copy", "import_tree"):
    setattr(Transaction, _operation_name, _atomic_operation(getattr(Transaction, _operation_name)))


class PersistentTreeStore(TreeStore):
    """TreeStore backed by a checksummed append-only snapshot log.

    Every committed transaction appends one complete snapshot and fsyncs it
    before publishing the new in-memory state. A truncated final frame is
    ignored on recovery; a complete frame with a bad checksum is rejected.
    """
    def __init__(self, path: str | os.PathLike[str], *, schema: Schema | Mapping[str, Any] | None = None):
        self.path_on_disk=Path(path)
        self.path_on_disk.parent.mkdir(parents=True, exist_ok=True)
        self._file_lock=threading.RLock()
        state=self._recover()
        super().__init__(state=state, schema=schema)
        if self.path_on_disk.stat().st_size == 0:
            with self._file_lock:
                self._append(state)

    def _recover(self):
        if not self.path_on_disk.exists():
            self.path_on_disk.touch()
        data=self.path_on_disk.read_bytes()
        if not data:
            return _new_state()
        pos=0; last=None
        while pos < len(data):
            if len(data)-pos < _HEADER.size:
                break
            magic,length=_HEADER.unpack(data[pos:pos+_HEADER.size])
            if magic != _MAGIC:
                # A bad prefix followed by another recognizable frame means
                # corruption between committed frames, not a torn suffix. Do
                # not silently discard the later acknowledged state.
                if data.find(_MAGIC, pos + 1) != -1:
                    raise StorageCorruptionError("corrupt bytes between committed frames")
                break
            end=pos+_HEADER.size+length+_CRC.size
            # A complete header with an incomplete payload/CRC is a torn tail,
            # regardless of the advertised length. A complete frame above the
            # safety limit is corruption and is rejected below.
            if end > len(data):
                break
            if length > 512*1024*1024:
                raise StorageCorruptionError("invalid frame length")
            payload=data[pos+_HEADER.size:pos+_HEADER.size+length]
            expected=_CRC.unpack(data[pos+_HEADER.size+length:end])[0]
            if zlib.crc32(payload) & 0xffffffff != expected:
                raise StorageCorruptionError("checksum mismatch in complete frame")
            last=_snapshot_from_json(payload); pos=end
        if last is None:
            # A non-empty file cannot be mistaken for a new database: that
            # would silently hide corruption or acknowledged data loss.
            raise StorageCorruptionError("no valid frame in non-empty database")
        if pos < len(data):
            # Discard only the uncommitted/torn suffix. This also prevents a
            # garbage tail from hiding future valid commits on the next open.
            with self.path_on_disk.open("r+b") as f:
                f.truncate(pos); f.flush(); os.fsync(f.fileno())
        return last

    def _append(self,state):
        payload=_snapshot_json(state)
        frame=_HEADER.pack(_MAGIC,len(payload))+payload+_CRC.pack(zlib.crc32(payload)&0xffffffff)
        with self.path_on_disk.open("ab") as f:
            f.write(frame); f.flush(); os.fsync(f.fileno())

    def _commit(self, tx):
        with self._lock:
            if tx._base_version != self._version: raise InvalidOperationError("transaction conflict: store changed since transaction began")
            _check_invariants(tx._state)
            old = self._state
            with self._file_lock: self._append(tx._state)
            self._state=tx._state; self._version += 1; subscriptions=tuple(self._subscriptions)
        for change in tx._changes:
            for sub in subscriptions:
                if sub._matches(change, old, self._state):
                    try: sub._callback(change)
                    except Exception: pass

    def close(self):
        return None
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

"""Snapshot queries and lightweight secondary property indexes for GROVE.

Queries deliberately operate on a detached state snapshot.  A query can
therefore be retained while other threads mutate a store without observing
partially committed data or later updates.  Traversal follows the ordered
primary child edges; a property index only narrows candidates and does not
change query ordering or filtering semantics.
"""
from __future__ import annotations

import copy
import datetime as _dt
import math
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from .model import Node
from .types import Reference
from .store import _record_to_node, _state_copy


_MISSING = object()


def _index_key(value: Any) -> Any:
    """Return a typed, recursively hashable key for a property value.

    Python's normal equality aliases ``True`` and ``1`` and cannot hash maps or
    lists.  Index keys retain GROVE's value types so an index lookup never
    returns a node because of an accidental Python coercion.
    """
    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        # Match finite Python equality semantics while retaining the type tag.
        # The store rejects NaN/Infinity, so canonical zero is sufficient.
        return ("float", 0.0 if value == 0.0 else value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, bytes):
        return ("bytes", value)
    if isinstance(value, _dt.datetime):
        normalized = value.astimezone(_dt.timezone.utc) if value.tzinfo else value
        return ("datetime", normalized.isoformat())
    if isinstance(value, Reference):
        return ("reference", value.node_id)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_index_key(item) for item in value))
    if isinstance(value, dict):
        return ("dict", tuple(sorted((key, _index_key(item)) for key, item in value.items())))
    # Store validation means this is normally unreachable, but keeping a
    # deterministic fallback makes the index robust when used with a custom
    # state in tests.
    return (type(value).__qualname__, repr(value))


def _property_value(properties: Mapping[str, Any], name: str) -> Any:
    """Read a top-level property, with dotted lookup for nested maps."""
    if name in properties:
        return properties[name]
    if "." not in name:
        return _MISSING
    current: Any = properties
    for part in name.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _matches_criteria(node: Node, criteria: Mapping[str, Any]) -> bool:
    for key, expected in criteria.items():
        if key == "type":
            if node.type != expected:
                return False
            continue
        actual = _property_value(node.properties, key)
        if actual is _MISSING or _index_key(actual) != _index_key(expected):
            return False
    return True


def _predicate_from(value: Any) -> Callable[[Node], bool] | None:
    if value is None:
        return None
    if callable(value):
        return value
    if isinstance(value, Mapping):
        criteria = dict(value)
        return lambda node: _matches_criteria(node, criteria)
    raise TypeError("predicate must be callable, a property mapping, or None")


class Query(Iterable[Node]):
    """An immutable query over one detached GROVE state snapshot.

    ``Query`` instances are normally obtained from :meth:`TreeStore.query`.
    Results are materialized as fresh detached :class:`~grove.Node` views on
    each iteration, so mutating a returned properties map cannot affect a
    subsequent result or the source store.
    """

    def __init__(
        self,
        state: dict[str, Any],
        root_id: str,
        *,
        recursive: bool = True,
        include_root: bool = False,
        predicate: Callable[[Node], bool] | Mapping[str, Any] | None = None,
        candidate_ids: Iterable[str] | None = None,
    ) -> None:
        self._state = _state_copy(state)
        if root_id not in self._state["nodes"]:
            raise KeyError(root_id)
        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a bool")
        if not isinstance(include_root, bool):
            raise TypeError("include_root must be a bool")
        self._root_id = root_id
        self._recursive = recursive
        self._include_root = include_root
        self._predicate = _predicate_from(predicate)
        self._candidate_ids = None if candidate_ids is None else frozenset(candidate_ids)

    @classmethod
    def _from_snapshot(cls, state: dict[str, Any], root_id: str, **kwargs: Any) -> "Query":
        return cls(state, root_id, **kwargs)

    def _copy(self, **changes: Any) -> "Query":
        options = {
            "recursive": self._recursive,
            "include_root": self._include_root,
            "predicate": self._predicate,
            "candidate_ids": self._candidate_ids,
        }
        options.update(changes)
        return Query(self._state, self._root_id, **options)

    def where(
        self,
        predicate: Callable[[Node], bool] | Mapping[str, Any] | None = None,
        **criteria: Any,
    ) -> "Query":
        """Return a query with an additional predicate/property filter.

        Mapping predicates and keyword criteria are exact typed property
        matches; ``type=...`` filters the node type.  A callable receives a
        detached ``Node`` view.
        """
        if predicate is None and not criteria:
            return self
        new_predicate = _predicate_from(predicate)
        if criteria:
            criterion_predicate = _predicate_from(criteria)
            if new_predicate is None:
                new_predicate = criterion_predicate
            else:
                old = new_predicate
                assert criterion_predicate is not None
                new_predicate = lambda node: bool(old(node)) and bool(criterion_predicate(node))
        if new_predicate is None:
            return self
        if self._predicate is None:
            combined = new_predicate
        else:
            old = self._predicate
            combined = lambda node: bool(old(node)) and bool(new_predicate(node))
        return self._copy(predicate=combined)

    filter = where

    def descendants(self, *, recursive: bool = True, include_root: bool = False) -> "Query":
        """Return this same snapshot with a different traversal scope."""
        return self._copy(recursive=recursive, include_root=include_root)

    def by_type(self, node_type: str) -> "Query":
        return self.where(type=node_type)

    def _iter_ids(self) -> Iterator[str]:
        """Yield matching IDs in depth-first child order without recursion.

        The query already owns a detached state snapshot. Traversal keeps only
        a bounded frontier of pending IDs and never builds a result list; the
        snapshot itself remains O(N), while traversal bookkeeping is bounded by
        the active frontier rather than the number of yielded results.  An explicit stack
        also avoids Python's recursion limit for valid, very deep trees.
        """
        nodes = self._state["nodes"]
        if self._include_root:
            initial = (self._root_id,)
        else:
            initial = nodes[self._root_id]["children"]

        # Keep one child iterator per active depth rather than a reversed list
        # of all pending IDs.  This preserves stored order while bounding
        # traversal bookkeeping by depth, including for a broad sibling set.
        pending = [(iter(initial), 0)]
        while pending:
            children, depth = pending[-1]
            try:
                node_id = next(children)
            except StopIteration:
                pending.pop()
                continue
            if self._candidate_ids is None or node_id in self._candidate_ids:
                yield node_id
            # With include_root=True and recursive=False, include the target
            # and its direct children. Without the target, recursive=False
            # means exactly the target's direct children.
            if self._recursive or (self._include_root and depth == 0):
                pending.append((iter(nodes[node_id]["children"]), depth + 1))

    def iter(self) -> Iterator[Node]:
        """Return a lazy iterator over this query's detached snapshot.

        Calling ``iter(query)`` remains equivalent.  No result collection is
        retained: only traversal bookkeeping and the currently yielded,
        detached :class:`Node` are live outside the snapshot.  The snapshot
        itself is intentionally retained so later store commits cannot alter
        iteration results.
        """
        return iter(self)

    # Descriptive aliases for callers who prefer explicit traversal names.
    iter_nodes = iter
    traverse = iter

    def __iter__(self) -> Iterator[Node]:
        for node_id in self._iter_ids():
            node = _record_to_node(self._state["nodes"][node_id])
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
        return (
            f"Query(root_id={self._root_id!r}, recursive={self._recursive!r}, "
            f"include_root={self._include_root!r})"
        )


class PropertyIndex:
    """A lightweight secondary index over one property name.

    The index is attached to a store and refreshed lazily by store revision.
    It stores only node IDs; lookups construct a query against one coherent
    detached snapshot, preserving normal query traversal and predicate rules.
    Values are matched with GROVE's typed equality (for example ``True`` does
    not match ``1``).  Missing properties are not indexed.
    """

    def __init__(self, store: Any, property_name: str) -> None:
        if not isinstance(property_name, str) or not property_name:
            raise ValueError("indexed property name must be a non-empty string")
        if "\x00" in property_name:
            raise ValueError("indexed property name cannot contain NUL")
        self.store = store
        self.property_name = property_name
        self.name = property_name
        self._revision: int | None = None
        self._mapping: dict[Any, frozenset[str]] = {}

    def _state_mapping(self, state: dict[str, Any]) -> dict[Any, frozenset[str]]:
        # Build from the exact state passed by the caller.  A store can commit
        # between copying a query snapshot and inspecting its revision, so a
        # revision-only cache could accidentally label an older mapping as
        # current.  Rebuilding this intentionally small index is safer and
        # keeps lookup results tied to one coherent snapshot.
        mutable: dict[Any, set[str]] = {}
        for node_id, record in state["nodes"].items():
            value = _property_value(record["properties"], self.property_name)
            if value is _MISSING:
                continue
            mutable.setdefault(_index_key(value), set()).add(node_id)
        mapping = {key: frozenset(ids) for key, ids in mutable.items()}
        # Return this call's mapping object, not the mutable cache slot.  Two
        # concurrent lookups can rebuild against different snapshots; using
        # ``self._mapping`` in the return expression could hand a caller the
        # other lookup's candidate set after an interleaving assignment.
        self._mapping = mapping
        self._revision = getattr(self.store, "_version", None)
        return mapping

    def lookup(
        self,
        value: Any,
        target: str | Node = "/",
        *,
        recursive: bool = True,
        include_root: bool = False,
        predicate: Callable[[Node], bool] | Mapping[str, Any] | None = None,
    ) -> Query:
        """Query nodes whose indexed property exactly equals ``value``."""
        # The state snapshot and index map are obtained in one store operation
        # where possible.  SQLite's override refreshes before copying state.
        state = self.store._query_state_snapshot()
        root_id = self.store._resolve(state, target)
        ids = self._state_mapping(state).get(_index_key(value), frozenset())
        return Query._from_snapshot(
            state,
            root_id,
            recursive=recursive,
            include_root=include_root,
            predicate=predicate,
            candidate_ids=ids,
        )

    query = lookup
    get = lookup
    find = lookup

    def ids(self, value: Any, target: str | Node = "/", **kwargs: Any) -> tuple[str, ...]:
        return self.lookup(value, target, **kwargs).ids()

    def nodes(self, value: Any, target: str | Node = "/", **kwargs: Any) -> list[Node]:
        return self.lookup(value, target, **kwargs).all()

    def __getitem__(self, value: Any) -> Query:
        return self.lookup(value)

    def __contains__(self, value: Any) -> bool:
        state = self.store._query_state_snapshot()
        return bool(self._state_mapping(state).get(_index_key(value)))

    def __repr__(self) -> str:
        return f"PropertyIndex({self.property_name!r})"

"""Bounded shape/mutation coverage for the SQLite dirty-row experiment."""
from __future__ import annotations

import random

import pytest

from grove import SQLiteTreeStore
from grove.sqlite_incremental_experiment import SQLiteIncrementalTreeStore


_FAMILIES = ("deep", "broad", "skew")


def _shape(store):
    def walk(node):
        return {
            "name": node.name,
            "type": node.type,
            "properties": node.properties,
            "children": [walk(store.get(child)) for child in node.children],
        }

    return walk(store.root)


def _build(store, family: str, count: int = 64) -> None:
    rng = random.Random(17)
    previous = "/"
    with store.transaction() as tx:
        for index in range(count):
            ids = [f"n{item}" for item in range(index)]
            if family == "deep":
                parent = previous
            elif family == "broad":
                parent = "/"
            else:
                parent = "/" if not ids or rng.random() < 0.10 else ids[
                    min(int(rng.random() ** 4 * len(ids)), len(ids) - 1)
                ]
            tx.create(
                f"n{index}",
                node_id=f"n{index}",
                parent=parent,
                properties={"family": family, "index": index},
            )
            previous = f"n{index}"


def _leaf(store):
    nodes = store._state["nodes"]
    root_id = store._state["root_id"]
    return next(
        node_id for node_id, record in nodes.items()
        if node_id != root_id and not record["children"]
    )


@pytest.mark.parametrize("family", _FAMILIES)
def test_incremental_shape_families_match_through_mutations_and_reopen(tmp_path, family):
    """Exercise representative deep, broad, and skewed trees.

    This is intentionally a small correctness matrix, not a performance gate.
    Reopening between mutation groups gives the dirty writer the same durable
    lifecycle exercised by the benchmark's reopen-like workload.
    """
    full = SQLiteTreeStore(tmp_path / f"{family}-full.db")
    dirty = SQLiteIncrementalTreeStore(tmp_path / f"{family}-dirty.db")
    try:
        _build(full, family)
        _build(dirty, family)
        assert _shape(full) == _shape(dirty)

        target = _leaf(full)
        parent = full._state["nodes"][target]["parent_id"]
        original_name = full._state["nodes"][target]["name"]
        rename_target = next(
            node_id for node_id, record in full._state["nodes"].items()
            if node_id != full._state["root_id"] and record["children"]
        ) if any(
            record["children"]
            for node_id, record in full._state["nodes"].items()
            if node_id != full._state["root_id"]
        ) else target
        rename_name = full._state["nodes"][rename_target]["name"]
        extra_id = f"extra-{family}"
        doomed_id = f"doomed-{family}"
        doomed_child_id = f"doomed-child-{family}"
        parent_arg = "/" if parent == full._state["root_id"] else parent

        operations = [
            lambda store: store.update(
                target, properties={"family": family, "changed": True}
            ),
            lambda store: store.rename(rename_target, f"renamed-{family}"),
            lambda store: store.rename(rename_target, rename_name),
            lambda store: store.move(target, "/", name=f"moved-{family}", index=0),
            lambda store: store.move(target, parent_arg, name=original_name, index=0),
            lambda store: store.create(
                "extra", parent=target, node_id=extra_id, properties={"extra": True}
            ),
            lambda store: store.update(
                extra_id, properties={"updated": True}, merge=True
            ),
            lambda store: store.move(extra_id, "/", name="extra-at-root", index=0),
            lambda store: store.move(extra_id, target, name="extra", index=0),
            lambda store: store.delete(extra_id, recursive=True),
            lambda store: store.create("doomed", parent=target, node_id=doomed_id),
            lambda store: store.create(
                "doomed-child", parent=doomed_id, node_id=doomed_child_id
            ),
            lambda store: store.delete(doomed_id, recursive=True),
        ]
        for index, operation in enumerate(operations, start=1):
            operation(full)
            operation(dirty)
            assert _shape(full) == _shape(dirty), (family, index)
            # Reopen-like boundaries also verify that the incremental rows are
            # accepted by the core reader and remain equivalent after refresh.
            if index % 3 == 0:
                dirty.close()
                dirty = SQLiteIncrementalTreeStore(tmp_path / f"{family}-dirty.db")
                assert _shape(full) == _shape(dirty), (family, index, "reopen")
    finally:
        full.close()
        dirty.close()

"""Regression tests for lazy, detached query traversal."""
from __future__ import annotations

import tracemalloc

from grove import TreeStore


def _chain(length: int) -> TreeStore:
    store = TreeStore()
    # Stage the shape in one transaction.  Besides making this fixture cheap,
    # it ensures the traversal test is about iterator memory, not per-commit
    # snapshot copying.
    with store.transaction() as tx:
        parent = "/"
        for index in range(length):
            parent = tx.create(f"node-{index}", parent=parent).id
    return store


def test_lazy_iterator_handles_a_chain_deeper_than_python_recursion_limit():
    store = _chain(1_250)
    iterator = store.query(include_root=True).iter()

    # A generator is lazy: no predicate or node conversion has run before the
    # first next() call, and iteration does not recurse through the tree.
    assert iter(iterator) is iterator
    first = next(iterator)
    assert first.name == ""
    names = [node.name for node in iterator]
    assert len(names) == 1_250
    assert names[0] == "node-0"
    assert names[-1] == "node-1249"


def test_broad_traversal_does_not_materialize_results_or_retain_them():
    store = TreeStore()
    with store.transaction() as tx:
        branch = tx.create("branch")
        for index in range(10_000):
            tx.create(f"leaf-{index}", parent=branch.id)

    visited = []
    query = store.query(branch.id).where(
        lambda node: visited.append(node.name) or True
    )
    iterator = query.iter()
    assert visited == []
    assert next(iterator).name == "leaf-0"
    assert visited == ["leaf-0"]

    # The detached query snapshot is created above.  This measures only the
    # traversal/result machinery: one yielded Node and the pending child-ID
    # stack, rather than a second list containing all 10,000 nodes.
    tracemalloc.start()
    baseline_current, _ = tracemalloc.get_traced_memory()
    count = 1
    for _node in iterator:
        count += 1
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert count == 10_000
    assert len(visited) == 10_000
    # Keep this deliberately generous across Python versions while guarding
    # against accidentally changing traversal back to list materialization.
    assert peak - baseline_current < 4_000_000


def test_traverse_alias_preserves_snapshot_and_primary_child_order():
    store = TreeStore()
    parent = store.create("parent")
    first = store.create("first", parent=parent.id)
    second = store.create("second", parent=parent.id)
    iterator = store.query(parent.id).traverse()

    store.rename(first.id, "changed")
    assert [node.name for node in iterator] == ["first", "second"]

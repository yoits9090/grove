"""The immutable public node view used by the tree API."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Tuple

@dataclass(frozen=True, slots=True)
class Node:
    id: str
    name: str
    type: str
    properties: dict[str, Any]
    parent_id: str | None
    children: Tuple[str, ...]
    created_at: str
    modified_at: str

    @property
    def node_type(self) -> str:
        """Alias for callers who prefer the explicit spelling."""
        return self.type

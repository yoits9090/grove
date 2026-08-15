"""Values with explicit semantics in the GROVE property model."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Reference:
    """A non-owning link to a node ID.

    References do not participate in parent/child invariants and may be
    dangling after the referenced node is deleted.
    """
    node_id: str

    def __post_init__(self) -> None:
        if (not isinstance(self.node_id, str) or not self.node_id or
                "/" in self.node_id or "\x00" in self.node_id):
            raise ValueError("Reference.node_id must be a non-empty opaque ID")

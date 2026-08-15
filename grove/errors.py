"""Public exceptions raised by GROVE."""

class GroveError(Exception):
    """Base class for all expected GROVE errors."""

class NotFoundError(GroveError, KeyError):
    """A node or path does not exist."""

class AlreadyExistsError(GroveError):
    """A sibling with the requested name, or an ID, already exists."""

class InvalidOperationError(GroveError, ValueError):
    """An operation would violate a tree invariant."""

class InvalidPropertyError(GroveError, TypeError):
    """A property contains a value outside GROVE's supported types."""

class StorageCorruptionError(GroveError):
    """The durable log has a corrupt committed frame."""

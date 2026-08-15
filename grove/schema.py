"""Minimal, opt-in schema validation for GROVE nodes.

A :class:`Schema` maps node types to property declarations.  Declarations may
use Python types (or tuples of types), callables, or small dictionaries such
as ``{"type": str, "required": True}``.  A type declaration may be a
shorthand property mapping or an explicit mapping containing ``properties``,
``required`` and ``allow_extra``.

Schemas are deliberately in-memory configuration.  They are checked before a
mutation is committed, so failed create/update/import operations leave the
transaction unchanged.
"""
from __future__ import annotations

import types as _types
from collections.abc import Mapping, Iterable
from typing import Any, Union, get_args, get_origin

from .errors import InvalidOperationError


class SchemaValidationError(InvalidOperationError):
    """A node does not satisfy the store's configured :class:`Schema`."""


_MISSING = object()
_RESERVED = {"properties", "required", "allow_extra",
             "allow_unknown", "additional_properties",
             "required_properties"}


def _type_name(value: Any) -> str:
    if isinstance(value, tuple):
        return " | ".join(_type_name(x) for x in value)
    if isinstance(value, type):
        return value.__name__
    return repr(value)


def _check_type(value: Any, expected: Any) -> bool:
    if expected is Any or expected is object or expected is None:
        return True if expected is not None else value is None
    if isinstance(expected, str):
        expected = {"string": str, "str": str, "integer": int,
                    "int": int, "number": (int, float),
                    "float": float, "boolean": bool, "bool": bool,
                    "array": list, "list": list, "object": dict,
                    "map": dict, "null": type(None)}.get(expected, expected)
        if isinstance(expected, str):
            return type(value).__name__ == expected
    origin = get_origin(expected)
    args = get_args(expected)
    if origin in (_types.UnionType, Union):
        return any(_check_type(value, item) for item in args)
    if origin in (list, tuple, set, frozenset):
        if not isinstance(value, origin):
            return False
        if args and args[0] is not Any:
            return all(_check_type(item, args[0]) for item in value)
        return True
    if origin in (dict, Mapping):
        if not isinstance(value, Mapping):
            return False
        if len(args) == 2 and args[0] is not Any and args[1] is not Any:
            return all(_check_type(k, args[0]) and _check_type(v, args[1])
                       for k, v in value.items())
        return True
    if isinstance(expected, tuple):
        return any(_check_type(value, item) for item in expected)
    if isinstance(expected, type):
        # bool is an int subclass, but GROVE's typed values distinguish them.
        if expected is int:
            return type(value) is int
        if expected is float:
            return type(value) is float
        return isinstance(value, expected)
    return value == expected


def _run_constraint(value: Any, constraint: Any) -> bool:
    """Evaluate one property declaration without mutating user objects."""
    if constraint is Any or constraint is None or isinstance(constraint, type):
        return _check_type(value, constraint)
    if isinstance(constraint, tuple):
        return _check_type(value, constraint)
    if isinstance(constraint, Mapping):
        # ``type``/``types`` is the common form.  ``enum`` and ``values`` are
        # useful tiny additions that keep the API practical without a DSL.
        expected = constraint.get("type", constraint.get("types", _MISSING))
        if expected is not _MISSING and not _check_type(value, expected):
            return False
        enum = constraint.get("enum", constraint.get("values", _MISSING))
        if enum is not _MISSING:
            try:
                if value not in enum:
                    return False
            except (TypeError, ValueError):
                return False
        validator = constraint.get("validator", constraint.get("validate", _MISSING))
        if validator is not _MISSING:
            if not callable(validator):
                return False
            try:
                if not bool(validator(value)):
                    return False
            except Exception:
                return False
        # A mapping with no recognized key is treated as a literal mapping.
        if expected is _MISSING and enum is _MISSING and validator is _MISSING:
            # ``{"required": True}`` is a useful unconstrained, required
            # property declaration.  Other unrecognized mappings are treated
            # as literal values so accidental schema typos fail closed.
            if "required" in constraint:
                return True
            return value == constraint
        return True
    if callable(constraint):
        try:
            return bool(constraint(value))
        except Exception:
            return False
    return value == constraint


class Schema:
    """Opt-in node type/property constraints.

    ``Schema({"person": {"properties": {"name": str},
    "required": ["name"]}})`` is the explicit form.  The shorthand
    ``Schema({"person": {"name": str}})`` declares the same property as
    optional.  Once at least one type is declared, unknown types are rejected
    by default (the built-in ``root`` sentinel remains valid); pass
    ``allow_unknown_types=True`` to constrain only types that are declared.
    ``Schema()`` is a no-op schema.
    """

    def __init__(self, types: Mapping[str, Any] | None = None, *,
                 node_types: Mapping[str, Any] | None = None,
                 allow_unknown_types: bool | None = None) -> None:
        if types is not None and node_types is not None:
            raise TypeError("provide either types or node_types, not both")
        declarations = node_types if node_types is not None else types
        if declarations is None:
            declarations = {}
        if not isinstance(declarations, Mapping):
            raise TypeError("schema types must be a mapping")
        self.types: dict[str, dict[str, Any]] = {}
        for node_type, declaration in declarations.items():
            if not isinstance(node_type, str) or not node_type:
                raise TypeError("schema node types must be non-empty strings")
            self.types[node_type] = self._normalize_declaration(declaration)
        if allow_unknown_types is None:
            allow_unknown_types = not bool(self.types)
        if not isinstance(allow_unknown_types, bool):
            raise TypeError("allow_unknown_types must be a bool")
        self.allow_unknown_types = allow_unknown_types

    @staticmethod
    def _normalize_declaration(declaration: Any) -> dict[str, Any]:
        if declaration is None:
            return {"properties": {}, "required": frozenset(),
                    "allow_extra": True}
        if not isinstance(declaration, Mapping):
            raise TypeError("each schema node type must have a mapping declaration")
        explicit = any(key in declaration for key in _RESERVED)
        if explicit:
            raw_props = declaration.get("properties", {})
            if not isinstance(raw_props, Mapping):
                raise TypeError("schema properties must be a mapping")
            required = declaration.get("required",
                                      declaration.get("required_properties", ()))
            if isinstance(required, str) or not isinstance(required, Iterable):
                raise TypeError("schema required properties must be iterable")
            required = frozenset(required)
            if any(not isinstance(k, str) for k in required):
                raise TypeError("schema required property names must be strings")
            allow_extra = declaration.get("allow_extra",
                          declaration.get("allow_unknown",
                          declaration.get("additional_properties", True)))
            if not isinstance(allow_extra, bool):
                raise TypeError("schema allow_extra must be a bool")
            return {"properties": dict(raw_props), "required": required,
                    "allow_extra": allow_extra}
        # Shorthand direct property mapping.
        return {"properties": dict(declaration), "required": frozenset(),
                "allow_extra": True}

    def validate(self, node_type: str, properties: Mapping[str, Any], *,
                 node_name: str | None = None) -> None:
        """Raise :class:`SchemaValidationError` if a node is not valid."""
        if not isinstance(node_type, str) or not node_type:
            raise SchemaValidationError("node type must be a non-empty string")
        if not isinstance(properties, Mapping):
            raise SchemaValidationError("properties must be a mapping")
        declaration = self.types.get(node_type)
        if declaration is None:
            if self.allow_unknown_types or node_type == "root":
                return
            where = f" for node {node_name!r}" if node_name is not None else ""
            raise SchemaValidationError(f"unknown node type {node_type!r}{where}")
        expected = declaration["properties"]
        required = declaration["required"]
        for name in required:
            if name not in properties:
                raise SchemaValidationError(
                    f"missing required property {name!r} for type {node_type!r}")
        if not declaration["allow_extra"]:
            extras = set(properties) - set(expected)
            if extras:
                raise SchemaValidationError(
                    f"unknown properties for type {node_type!r}: {sorted(extras)!r}")
        for name, constraint in expected.items():
            if name not in properties:
                # A property declaration can mark itself required.
                if isinstance(constraint, Mapping) and constraint.get("required", False):
                    raise SchemaValidationError(
                        f"missing required property {name!r} for type {node_type!r}")
                continue
            value = properties[name]
            if not _run_constraint(value, constraint):
                raise SchemaValidationError(
                    f"property {name!r} on type {node_type!r} does not satisfy {_type_name(constraint)}")

    def validate_node(self, node_type: str, properties: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        # Accept either ``validate_node(type, properties)`` or a detached Node
        # for callers validating imported/application models.
        if properties is None and hasattr(node_type, "type") and hasattr(node_type, "properties"):
            node = node_type
            self.validate(node.type, node.properties, node_name=getattr(node, "name", None), **kwargs)
            return
        self.validate(node_type, properties if properties is not None else {}, **kwargs)

    def __repr__(self) -> str:
        return f"Schema(types={tuple(self.types)!r}, allow_unknown_types={self.allow_unknown_types!r})"


# Friendly short alias for applications that prefer ``SchemaError``.
SchemaError = SchemaValidationError

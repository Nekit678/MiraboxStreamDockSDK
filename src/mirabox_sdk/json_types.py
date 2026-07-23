"""JSON value types shared by the MiraBox protocol models."""

from __future__ import annotations

import math
from typing import TypeAlias, TypeGuard

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def clone_json_object(value: object) -> JsonObject:
    """Validate a JSON object while cloning its mutable containers.

    The validation and cloning happen in the same recursive traversal. Immutable
    scalar values are reused, while every nested list and dictionary is rebuilt.

    Args:
        value: Arbitrary value expected to contain a finite JSON object.

    Returns:
        An isolated JSON object.

    Raises:
        ValueError: If ``value`` is not a finite JSON object.
    """

    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return _clone_json_dict(value)


def _clone_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("expected a finite JSON value")
        return value
    if isinstance(value, list):
        return [_clone_json_value(item) for item in value]
    if isinstance(value, dict):
        return _clone_json_dict(value)
    raise ValueError("expected a JSON value")


def _clone_json_dict(value: dict[object, object]) -> JsonObject:
    cloned: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("expected JSON object keys to be strings")
        cloned[key] = _clone_json_value(item)
    return cloned


def is_json_value(value: object) -> TypeGuard[JsonValue]:
    """Return whether a value can be represented safely by the JSON protocol.

    Accepted values are ``None``, booleans, integers, finite floats, strings,
    lists of accepted values, and dictionaries with string keys and accepted
    values. ``NaN`` and positive or negative infinity are rejected even though
    Python's default JSON encoder can emit them as non-standard tokens.

    Args:
        value: Arbitrary value to inspect recursively.

    Returns:
        ``True`` when ``value`` satisfies :data:`JsonValue`. Type checkers also
        narrow the value to that alias in the true branch.
    """

    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and is_json_value(item) for key, item in value.items())
    return False

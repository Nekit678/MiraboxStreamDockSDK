"""JSON value types shared by the MiraBox protocol models."""

from __future__ import annotations

import math
from typing import TypeAlias, TypeGuard

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


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

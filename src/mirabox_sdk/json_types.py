"""JSON value types shared by the MiraBox protocol models."""

from __future__ import annotations

import math
from collections.abc import (
    Callable,
    ItemsView,
    Iterable,
    Iterator,
    KeysView,
    Mapping,
    ValuesView,
)
from typing import Any, TypeAlias, TypeGuard, overload

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


def _copy_on_write_json_object(
    value: JsonObject,
    *,
    on_mutation: Callable[[], None] | None = None,
) -> JsonObject:
    """Return an isolated, lazily copied view of an owned JSON object.

    The source object must not be mutated after this function is called. The
    root is shallow-copied immediately; nested containers are shallow-copied
    only when traversed. Separate views can therefore share one validated
    snapshot without sharing subsequent writes.

    Args:
        value: Owned JSON object used as the immutable backing snapshot.
        on_mutation: Optional callback invoked after a validated view mutation
            commits.

    Returns:
        A ``dict`` subclass compatible with :data:`JsonObject`.
    """

    owner = _CopyOnWriteOwner(on_mutation)
    result = owner.wrap(value)
    if not isinstance(result, dict):  # pragma: no cover - narrowed by input type
        raise AssertionError("JSON object root was not a dictionary")
    return result


class _CopyOnWriteOwner:
    __slots__ = ("_containers", "_on_mutation")

    def __init__(self, on_mutation: Callable[[], None] | None) -> None:
        self._containers: dict[int, _CopyOnWriteJsonDict | _CopyOnWriteJsonList] = {}
        self._on_mutation = on_mutation

    def wrap(self, value: JsonValue) -> JsonValue:
        if isinstance(value, (_CopyOnWriteJsonDict, _CopyOnWriteJsonList)):
            return value
        if not isinstance(value, (dict, list)):
            return value

        identity = id(value)
        existing = self._containers.get(identity)
        if existing is not None:
            return existing

        if isinstance(value, dict):
            wrapped: _CopyOnWriteJsonDict | _CopyOnWriteJsonList = _CopyOnWriteJsonDict(value, self)
        else:
            wrapped = _CopyOnWriteJsonList(value, self)
        self._containers[identity] = wrapped
        return wrapped

    def changed(self) -> None:
        if self._on_mutation is not None:
            self._on_mutation()


_MISSING = object()


class _CopyOnWriteJsonDict(dict[str, JsonValue]):
    __slots__ = ("_owner",)

    def __init__(self, source: dict[str, JsonValue], owner: _CopyOnWriteOwner) -> None:
        # Populate the built-in storage so C-level consumers such as
        # ``json.dumps`` see the same mapping as Python-level consumers.
        super().__init__(source)
        self._owner = owner

    def __len__(self) -> int:
        return dict.__len__(self)

    def __iter__(self) -> Iterator[str]:
        return dict.__iter__(self)

    def __getitem__(self, key: str) -> JsonValue:
        value = dict.__getitem__(self, key)
        wrapped = self._owner.wrap(value)
        if wrapped is not value:
            dict.__setitem__(self, key, wrapped)
        return wrapped

    def __setitem__(self, key: str, value: JsonValue) -> None:
        if not isinstance(key, str):
            raise ValueError("expected JSON object keys to be strings")
        cloned = _clone_json_value(value)
        dict.__setitem__(self, key, cloned)
        self._owner.changed()

    def __delitem__(self, key: str) -> None:
        dict.__delitem__(self, key)
        self._owner.changed()

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key)

    def __repr__(self) -> str:
        return dict.__repr__(self)

    def __eq__(self, other: object) -> bool:
        return dict.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return dict.__ne__(self, other)

    def keys(self) -> KeysView[str]:
        return KeysView(self)

    def items(self) -> ItemsView[str, JsonValue]:
        return ItemsView(self)

    def values(self) -> ValuesView[JsonValue]:
        return ValuesView(self)

    @classmethod
    def fromkeys(
        cls,
        iterable: Iterable[str],
        value: JsonValue = None,
    ) -> JsonObject:
        return dict.fromkeys(iterable, value)

    def get(self, key: str, default: JsonValue = None) -> JsonValue:
        if key in self:
            return self[key]
        return default

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop(self, key: str, default: Any) -> JsonValue | Any: ...

    def pop(self, key: str, default: Any = _MISSING) -> JsonValue | Any:
        if key in self:
            value = self[key]
            del self[key]
            return value
        if default is _MISSING:
            raise KeyError(key)
        return default

    def popitem(self) -> tuple[str, JsonValue]:
        key, raw_value = dict.popitem(self)
        value = self._owner.wrap(raw_value)
        self._owner.changed()
        return key, value

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue:
        if key in self:
            return self[key]
        self[key] = default
        return self[key]

    def clear(self) -> None:
        if self:
            dict.clear(self)
            self._owner.changed()

    def update(
        self,
        other: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] = (),
        /,
        **kwargs: JsonValue,
    ) -> None:
        updates = dict(other)
        updates.update(kwargs)
        cloned = _clone_json_dict(updates)
        if cloned:
            dict.update(self, cloned)
            self._owner.changed()

    def copy(self) -> JsonObject:
        return {key: self[key] for key in self}

    def __copy__(self) -> JsonObject:
        return self.copy()

    def __deepcopy__(self, memo: dict[int, object]) -> JsonObject:
        result = clone_json_object(self)
        memo[id(self)] = result
        return result

    def __reduce__(self) -> tuple[type[dict[Any, Any]], tuple[JsonObject]]:
        return dict, (self.copy(),)

    def __or__(self, other: Mapping[str, JsonValue]) -> JsonObject:
        result = self.copy()
        result.update(other)
        return result

    def __ror__(self, other: Mapping[str, JsonValue]) -> JsonObject:
        result = dict(other)
        result.update(self)
        return result

    def __ior__(self, other: Mapping[str, JsonValue]) -> _CopyOnWriteJsonDict:
        self.update(other)
        return self


class _CopyOnWriteJsonList(list[JsonValue]):
    __slots__ = ("_owner",)

    def __init__(self, source: list[JsonValue], owner: _CopyOnWriteOwner) -> None:
        # As with dictionaries, real shallow storage preserves compatibility
        # with CPython APIs that read list subclasses without calling __iter__.
        super().__init__(source)
        self._owner = owner

    def __len__(self) -> int:
        return list.__len__(self)

    @overload
    def __getitem__(self, index: int) -> JsonValue: ...

    @overload
    def __getitem__(self, index: slice) -> list[JsonValue]: ...

    def __getitem__(self, index: int | slice) -> JsonValue | list[JsonValue]:
        if isinstance(index, slice):
            return [self[item_index] for item_index in range(*index.indices(len(self)))]
        value = list.__getitem__(self, index)
        wrapped = self._owner.wrap(value)
        if wrapped is not value:
            list.__setitem__(self, index, wrapped)
        return wrapped

    @overload
    def __setitem__(self, index: int, value: JsonValue) -> None: ...

    @overload
    def __setitem__(self, index: slice, value: Iterable[JsonValue]) -> None: ...

    def __setitem__(
        self,
        index: int | slice,
        value: JsonValue | Iterable[JsonValue],
    ) -> None:
        if isinstance(index, slice):
            if not isinstance(value, Iterable):
                raise TypeError("can only assign an iterable")
            cloned: JsonValue | list[JsonValue] = [_clone_json_value(item) for item in value]
        else:
            cloned = _clone_json_value(value)
        list.__setitem__(self, index, cloned)  # type: ignore[arg-type]
        self._owner.changed()

    def __delitem__(self, index: int | slice) -> None:
        list.__delitem__(self, index)
        self._owner.changed()

    def __iter__(self) -> Iterator[JsonValue]:
        return (self[index] for index in range(len(self)))

    def __reversed__(self) -> Iterator[JsonValue]:
        return (self[index] for index in range(len(self) - 1, -1, -1))

    def __contains__(self, value: object) -> bool:
        return any(item == value for item in self)

    def __repr__(self) -> str:
        return list.__repr__(self)

    def __eq__(self, other: object) -> bool:
        return list.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return list.__ne__(self, other)

    def __lt__(self, other: list[JsonValue]) -> bool:
        return list.__lt__(self, other)

    def __le__(self, other: list[JsonValue]) -> bool:
        return list.__le__(self, other)

    def __gt__(self, other: list[JsonValue]) -> bool:
        return list.__gt__(self, other)

    def __ge__(self, other: list[JsonValue]) -> bool:
        return list.__ge__(self, other)

    def append(self, value: JsonValue) -> None:
        cloned = _clone_json_value(value)
        list.append(self, cloned)
        self._owner.changed()

    def extend(self, values: Iterable[JsonValue]) -> None:
        cloned = [_clone_json_value(value) for value in values]
        if cloned:
            list.extend(self, cloned)
            self._owner.changed()

    def insert(self, index: int, value: JsonValue) -> None:
        cloned = _clone_json_value(value)
        list.insert(self, index, cloned)
        self._owner.changed()

    def pop(self, index: int = -1) -> JsonValue:
        value = self[index]
        list.pop(self, index)
        self._owner.changed()
        return value

    def remove(self, value: JsonValue) -> None:
        for index, item in enumerate(self):
            if item == value:
                del self[index]
                return
        raise ValueError("list.remove(x): x not in list")

    def clear(self) -> None:
        if self:
            list.clear(self)
            self._owner.changed()

    def index(
        self,
        value: JsonValue,
        start: int = 0,
        stop: int | None = None,
    ) -> int:
        values = self.copy()
        if stop is None:
            return values.index(value, start)
        return values.index(value, start, stop)

    def count(self, value: JsonValue) -> int:
        return sum(item == value for item in self)

    def reverse(self) -> None:
        list.reverse(self)
        self._owner.changed()

    def sort(self, *, key: Callable[[JsonValue], Any] | None = None, reverse: bool = False) -> None:
        sorted_values = self.copy()
        sorted_values.sort(key=key, reverse=reverse)
        list.__setitem__(self, slice(None), sorted_values)
        self._owner.changed()

    def copy(self) -> list[JsonValue]:
        return list(self)

    def __copy__(self) -> list[JsonValue]:
        return self.copy()

    def __deepcopy__(self, memo: dict[int, object]) -> list[JsonValue]:
        result = [_clone_json_value(value) for value in self]
        memo[id(self)] = result
        return result

    def __reduce__(self) -> tuple[type[list[Any]], tuple[list[JsonValue]]]:
        return list, (self.copy(),)

    def __add__(self, other: list[JsonValue]) -> list[JsonValue]:
        return self.copy() + list(other)

    def __radd__(self, other: list[JsonValue]) -> list[JsonValue]:
        return list(other) + self.copy()

    def __iadd__(self, other: Iterable[JsonValue]) -> _CopyOnWriteJsonList:
        self.extend(other)
        return self

    def __mul__(self, count: int) -> list[JsonValue]:
        return self.copy() * count

    def __rmul__(self, count: int) -> list[JsonValue]:
        return self * count

    def __imul__(self, count: int) -> _CopyOnWriteJsonList:
        list.__imul__(self, count)
        self._owner.changed()
        return self


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

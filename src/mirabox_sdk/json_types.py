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


def _clone_json_value(
    value: object,
    container_has_only_scalars: dict[int, bool] | None = None,
) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("expected a finite JSON value")
        return value
    if isinstance(value, list):
        cloned_list: list[JsonValue] = []
        has_only_scalars = True
        for item in value:
            cloned_item = _clone_json_value(item, container_has_only_scalars)
            if isinstance(cloned_item, (dict, list)):
                has_only_scalars = False
            cloned_list.append(cloned_item)
        if container_has_only_scalars is not None:
            container_has_only_scalars[id(cloned_list)] = has_only_scalars
        return cloned_list
    if isinstance(value, dict):
        return _clone_json_dict(value, container_has_only_scalars)
    raise ValueError("expected a JSON value")


def _clone_json_dict(
    value: dict[object, object],
    container_has_only_scalars: dict[int, bool] | None = None,
) -> JsonObject:
    cloned: JsonObject = {}
    has_only_scalars = True
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("expected JSON object keys to be strings")
        cloned_item = _clone_json_value(item, container_has_only_scalars)
        if isinstance(cloned_item, (dict, list)):
            has_only_scalars = False
        cloned[key] = cloned_item
    if container_has_only_scalars is not None:
        container_has_only_scalars[id(cloned)] = has_only_scalars
    return cloned


class _CopyOnWriteJsonSource:
    __slots__ = ("container_has_only_scalars", "value")

    def __init__(
        self,
        value: JsonObject,
        container_has_only_scalars: dict[int, bool] | None = None,
    ) -> None:
        self.value = value
        if container_has_only_scalars is not None:
            self.container_has_only_scalars = container_has_only_scalars
            return

        self.container_has_only_scalars: dict[int, bool] = {}
        pending: list[dict[str, JsonValue] | list[JsonValue]] = [value]
        while pending:
            container = pending.pop()
            values = container.values() if isinstance(container, dict) else container
            has_only_scalars = True
            for item in values:
                if isinstance(item, (dict, list)):
                    has_only_scalars = False
                    pending.append(item)
            self.container_has_only_scalars[id(container)] = has_only_scalars


def _clone_json_object_source(value: object) -> _CopyOnWriteJsonSource:
    """Validate, clone, and prepare one owned copy-on-write JSON snapshot."""

    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    container_has_only_scalars: dict[int, bool] = {}
    cloned = _clone_json_dict(value, container_has_only_scalars)
    return _CopyOnWriteJsonSource(cloned, container_has_only_scalars)


def _prepare_copy_on_write_json_object(value: JsonObject) -> _CopyOnWriteJsonSource:
    """Precompute immutable snapshot metadata shared by multiple COW views."""

    return _CopyOnWriteJsonSource(value)


def _copy_on_write_json_object(
    value: JsonObject | _CopyOnWriteJsonSource,
    *,
    on_mutation: Callable[[], None] | None = None,
) -> JsonObject:
    """Return an isolated, lazily copied view of an owned JSON object.

    The source object must not be mutated after this function is called.
    Dictionaries retain sparse overlays and lists materialize only before a
    structural mutation. Separate views can therefore share one validated
    snapshot without copying wide containers or sharing subsequent writes.

    Args:
        value: Owned JSON object or prepared immutable backing snapshot.
        on_mutation: Optional callback invoked after a validated view mutation
            commits.

    Returns:
        A ``dict`` subclass compatible with :data:`JsonObject`.
    """

    source = (
        value
        if isinstance(value, _CopyOnWriteJsonSource)
        else _prepare_copy_on_write_json_object(value)
    )
    owner = _CopyOnWriteOwner(source, on_mutation)
    result = owner.wrap(source.value)
    if not isinstance(result, dict):  # pragma: no cover - narrowed by input type
        raise AssertionError("JSON object root was not a dictionary")
    return result


def _get_copy_on_write_json_source(value: JsonObject) -> _CopyOnWriteJsonSource:
    """Return the prepared snapshot backing an SDK-created copy-on-write root."""

    if not isinstance(value, _CopyOnWriteJsonDict):
        raise TypeError("expected a copy-on-write JSON object")
    return value._owner._source


class _CopyOnWriteOwner:
    __slots__ = ("_containers", "_on_mutation", "_source")

    def __init__(
        self,
        source: _CopyOnWriteJsonSource,
        on_mutation: Callable[[], None] | None,
    ) -> None:
        self._containers: dict[int, _CopyOnWriteJsonDict | _CopyOnWriteJsonList] = {}
        self._on_mutation = on_mutation
        self._source = source

    def wrap(self, value: JsonValue) -> JsonValue:
        if not isinstance(value, (dict, list)):
            return value
        if isinstance(value, (_CopyOnWriteJsonDict, _CopyOnWriteJsonList)):
            return value

        identity = id(value)
        existing = self._containers.get(identity)
        if existing is not None:
            return existing

        source_has_only_scalars = self._source.container_has_only_scalars.get(identity)
        if isinstance(value, dict):
            wrapped: _CopyOnWriteJsonDict | _CopyOnWriteJsonList = _CopyOnWriteJsonDict(
                value,
                self,
                source_has_only_scalars,
            )
        else:
            wrapped = _CopyOnWriteJsonList(value, self, source_has_only_scalars)
        self._containers[identity] = wrapped
        return wrapped

    def changed(self) -> None:
        if self._on_mutation is not None:
            self._on_mutation()


_MISSING = object()
_COW_SENTINEL = object()


class _CopyOnWriteJsonDict(dict[str, JsonValue]):
    __slots__ = (
        "_added",
        "_deleted",
        "_owner",
        "_source",
        "_source_has_only_scalars",
        "_source_cleared",
        "_updated",
    )

    def __init__(
        self,
        source: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] = (),
        owner: _CopyOnWriteOwner | None = None,
        source_has_only_scalars: bool | None = None,
    ) -> None:
        if owner is None:
            standalone_source = dict(source)
            prepared_source = _prepare_copy_on_write_json_object(standalone_source)
            owner = _CopyOnWriteOwner(prepared_source, None)
            source = standalone_source
            source_has_only_scalars = prepared_source.container_has_only_scalars.get(id(source))
        elif not isinstance(source, dict):  # pragma: no cover - internal invariant
            raise TypeError("copy-on-write source must be a dictionary")

        # CPython's JSON encoder skips methods on physically empty dict
        # subclasses. A private sentinel keeps it on the mapping path, while
        # every public operation below exposes only the logical overlay.
        super().__init__()
        dict.__setitem__(self, _COW_SENTINEL, None)  # type: ignore[arg-type]
        self._owner = owner
        self._source = source
        self._source_has_only_scalars = source_has_only_scalars
        self._updated: dict[str, JsonValue] | None = None
        self._added: dict[str, JsonValue] | None = None
        self._deleted: set[str] | None = None
        self._source_cleared = False

    def __len__(self) -> int:
        source_length = 0 if self._source_cleared else len(self._source)
        deleted_length = 0 if self._deleted is None else len(self._deleted)
        added_length = 0 if self._added is None else len(self._added)
        return source_length - deleted_length + added_length

    def __iter__(self) -> Iterator[str]:
        if not self._source_cleared:
            deleted = self._deleted
            for key in self._source:
                if deleted is None or key not in deleted:
                    yield key
        if self._added is not None:
            yield from self._added

    def __reversed__(self) -> Iterator[str]:
        if self._added is not None:
            yield from reversed(self._added)
        if not self._source_cleared:
            deleted = self._deleted
            for key in reversed(self._source):
                if deleted is None or key not in deleted:
                    yield key

    def __getitem__(self, key: str) -> JsonValue:
        location: str
        if self._added is not None and key in self._added:
            value = self._added[key]
            location = "added"
        elif self._updated is not None and key in self._updated:
            value = self._updated[key]
            location = "updated"
        elif not self._source_cleared and self._deleted is None:
            value = self._source[key]
            location = "source"
        elif self._source_key_is_visible(key):
            value = self._source[key]
            location = "source"
        else:
            raise KeyError(key)

        wrapped = self._owner.wrap(value)
        if wrapped is not value:
            if location == "added":
                assert self._added is not None
                self._added[key] = wrapped
            else:
                if self._updated is None:
                    self._updated = {}
                self._updated[key] = wrapped
        return wrapped

    def __setitem__(self, key: str, value: JsonValue) -> None:
        if not isinstance(key, str):
            raise ValueError("expected JSON object keys to be strings")
        cloned = _clone_json_value(value)
        self._set_cloned(key, cloned)
        self._owner.changed()

    def __delitem__(self, key: str) -> None:
        if self._added is not None and key in self._added:
            del self._added[key]
            if not self._added:
                self._added = None
            self._owner.changed()
            return
        if not self._source_key_is_visible(key):
            raise KeyError(key)
        if self._updated is not None:
            self._updated.pop(key, None)
            if not self._updated:
                self._updated = None
        if self._deleted is None:
            self._deleted = set()
        self._deleted.add(key)
        self._owner.changed()

    def __contains__(self, key: object) -> bool:
        if self._added is not None and key in self._added:
            return True
        return self._source_key_is_visible(key)

    def __repr__(self) -> str:
        return repr(self.copy())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _CopyOnWriteJsonDict):
            other = other.copy()
        return self.copy() == other

    def __ne__(self, other: object) -> bool:
        return not self == other

    def _source_key_is_visible(self, key: object) -> bool:
        return (
            not self._source_cleared
            and key in self._source
            and (self._deleted is None or key not in self._deleted)
        )

    def _set_cloned(self, key: str, value: JsonValue) -> None:
        if self._added is not None and key in self._added:
            self._added[key] = value
        elif self._source_key_is_visible(key):
            if self._updated is None:
                self._updated = {}
            self._updated[key] = value
        else:
            if self._added is None:
                self._added = {}
            self._added[key] = value

    def keys(self) -> KeysView[str]:
        return KeysView(self)

    def items(self) -> ItemsView[str, JsonValue]:
        return _CopyOnWriteItemsView(self)

    def values(self) -> ValuesView[JsonValue]:
        return _CopyOnWriteValuesView(self)

    def _iter_items(self) -> Iterator[tuple[str, JsonValue]]:
        if self._has_unchanged_scalar_source():
            return iter(self._source.items())
        return ((key, self[key]) for key in self)

    def _iter_values(self) -> Iterator[JsonValue]:
        if self._has_unchanged_scalar_source():
            return iter(self._source.values())
        return (self[key] for key in self)

    def _has_unchanged_scalar_source(self) -> bool:
        if (
            self._source_cleared
            or self._updated is not None
            or self._added is not None
            or self._deleted is not None
        ):
            return False
        if self._source_has_only_scalars is None:
            self._source_has_only_scalars = all(
                not isinstance(value, (dict, list)) for value in self._source.values()
            )
        return self._source_has_only_scalars

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
        try:
            key = next(reversed(self))
        except StopIteration:
            raise KeyError("popitem(): dictionary is empty") from None
        value = self[key]
        del self[key]
        return key, value

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue:
        if key in self:
            return self[key]
        self[key] = default
        return self[key]

    def clear(self) -> None:
        if self:
            self._source_cleared = True
            self._updated = None
            self._added = None
            self._deleted = None
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
            for key, value in cloned.items():
                self._set_cloned(key, value)
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


class _CopyOnWriteItemsView(ItemsView[str, JsonValue]):
    __slots__ = ()

    def __iter__(self) -> Iterator[tuple[str, JsonValue]]:
        mapping = self._mapping
        if not isinstance(mapping, _CopyOnWriteJsonDict):  # pragma: no cover
            raise AssertionError("COW items view lost its mapping")
        return mapping._iter_items()


class _CopyOnWriteValuesView(ValuesView[JsonValue]):
    __slots__ = ()

    def __iter__(self) -> Iterator[JsonValue]:
        mapping = self._mapping
        if not isinstance(mapping, _CopyOnWriteJsonDict):  # pragma: no cover
            raise AssertionError("COW values view lost its mapping")
        return mapping._iter_values()


class _CopyOnWriteJsonList(list[JsonValue]):
    __slots__ = ("_owner", "_source", "_source_has_only_scalars", "_wrapped")

    def __init__(
        self,
        source: Iterable[JsonValue] = (),
        owner: _CopyOnWriteOwner | None = None,
        source_has_only_scalars: bool | None = None,
    ) -> None:
        if owner is None:
            standalone_source = list(source)
            prepared_source = _prepare_copy_on_write_json_object({"value": standalone_source})
            owner = _CopyOnWriteOwner(prepared_source, None)
            source = standalone_source
            source_has_only_scalars = prepared_source.container_has_only_scalars.get(id(source))
        elif not isinstance(source, list):  # pragma: no cover - internal invariant
            raise TypeError("copy-on-write source must be a list")

        # As with dictionaries, a sentinel ensures that CPython's JSON encoder
        # invokes the overridden iterator even for a logically empty source.
        super().__init__([_COW_SENTINEL])  # type: ignore[list-item]
        self._owner = owner
        self._source: list[JsonValue] | None = source
        self._source_has_only_scalars = source_has_only_scalars
        self._wrapped: dict[int, JsonValue] | None = None

    def __len__(self) -> int:
        if self._source is not None:
            return len(self._source)
        return list.__len__(self)

    def _materialize(self) -> None:
        if self._source is None:
            return
        values = self._source.copy()
        if self._wrapped is not None:
            for index, value in self._wrapped.items():
                values[index] = value
        list.__setitem__(self, slice(None), values)
        self._source = None
        self._wrapped = None

    @overload
    def __getitem__(self, index: int) -> JsonValue: ...

    @overload
    def __getitem__(self, index: slice) -> list[JsonValue]: ...

    def __getitem__(self, index: int | slice) -> JsonValue | list[JsonValue]:
        if isinstance(index, slice):
            return [self[item_index] for item_index in range(*index.indices(len(self)))]
        if self._source is None:
            value = list.__getitem__(self, index)
            normalized_index = index if index >= 0 else len(self) + index
        else:
            value = self._source[index]
            if self._wrapped is None:
                normalized_index = index
            else:
                normalized_index = index if index >= 0 else len(self._source) + index
                if normalized_index in self._wrapped:
                    value = self._wrapped[normalized_index]
        wrapped = self._owner.wrap(value)
        if wrapped is not value:
            if self._source is None:
                list.__setitem__(self, index, wrapped)
            else:
                if self._wrapped is None:
                    self._wrapped = {}
                    normalized_index = index if index >= 0 else len(self._source) + index
                self._wrapped[normalized_index] = wrapped
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
            self._materialize()
            list.__setitem__(self, index, cloned)
        else:
            cloned = _clone_json_value(value)
            if self._source is None:
                list.__setitem__(self, index, cloned)
            else:
                self._source[index]
                normalized_index = index if index >= 0 else len(self._source) + index
                if self._wrapped is None:
                    self._wrapped = {}
                self._wrapped[normalized_index] = cloned
        self._owner.changed()

    def __delitem__(self, index: int | slice) -> None:
        self._materialize()
        list.__delitem__(self, index)
        self._owner.changed()

    def __iter__(self) -> Iterator[JsonValue]:
        if self._source is not None and self._wrapped is None:
            if self._source_has_only_scalars is None:
                self._source_has_only_scalars = all(
                    not isinstance(value, (dict, list)) for value in self._source
                )
            if self._source_has_only_scalars:
                return iter(self._source)
        return (self[index] for index in range(len(self)))

    def __reversed__(self) -> Iterator[JsonValue]:
        return (self[index] for index in range(len(self) - 1, -1, -1))

    def __contains__(self, value: object) -> bool:
        return any(item == value for item in self)

    def __repr__(self) -> str:
        return repr(self.copy())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _CopyOnWriteJsonList):
            other = other.copy()
        return self.copy() == other

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __lt__(self, other: list[JsonValue]) -> bool:
        return self.copy() < list(other)

    def __le__(self, other: list[JsonValue]) -> bool:
        return self.copy() <= list(other)

    def __gt__(self, other: list[JsonValue]) -> bool:
        return self.copy() > list(other)

    def __ge__(self, other: list[JsonValue]) -> bool:
        return self.copy() >= list(other)

    def append(self, value: JsonValue) -> None:
        cloned = _clone_json_value(value)
        self._materialize()
        list.append(self, cloned)
        self._owner.changed()

    def extend(self, values: Iterable[JsonValue]) -> None:
        cloned = [_clone_json_value(value) for value in values]
        if cloned:
            self._materialize()
            list.extend(self, cloned)
            self._owner.changed()

    def insert(self, index: int, value: JsonValue) -> None:
        cloned = _clone_json_value(value)
        self._materialize()
        list.insert(self, index, cloned)
        self._owner.changed()

    def pop(self, index: int = -1) -> JsonValue:
        value = self[index]
        self._materialize()
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
            self._source = None
            self._wrapped = None
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
        self._materialize()
        list.reverse(self)
        self._owner.changed()

    def sort(self, *, key: Callable[[JsonValue], Any] | None = None, reverse: bool = False) -> None:
        sorted_values = self.copy()
        sorted_values.sort(key=key, reverse=reverse)
        self._source = None
        self._wrapped = None
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
        self._materialize()
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

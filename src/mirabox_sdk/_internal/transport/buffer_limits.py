"""API-independent byte accounting for frames and retained message objects."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import chain
from sys import getsizeof

DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_QUEUE_BYTE_LIMIT = 16 * 1024 * 1024


def validate_byte_limit(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def utf8_size(text: str, limit: int) -> int:
    """Count UTF-8 bytes, stopping above the limit without a full encoded copy."""

    if len(text) > limit:
        return limit + 1
    size = 0
    for offset in range(0, len(text), 4096):
        size += len(text[offset : offset + 4096].encode("utf-8", errors="surrogatepass"))
        if size > limit:
            break
    return size


def retained_size(value: object, limit: int) -> int:
    """Estimate retained DTO/JSON bytes, including COW backing containers.

    Each reachable object is counted once per message. Native container storage
    and instance attributes are read without invoking COW views or serializers.
    This excludes interpreter/allocator overhead and opaque native allocations.
    Iterators bound traversal scratch space even for very wide JSON objects.
    """

    seen: set[int] = set()
    pending: list[Iterator[object]] = [iter((value,))]
    size = 0
    while pending:
        item = next(pending[-1], _END)
        if item is _END:
            pending.pop()
            continue
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        size += getsizeof(item)
        if size > limit:
            return size
        # Scalars have no retained children; avoid an empty iterator per leaf.
        if isinstance(item, (str, bytes, int, float, bool, type)) or item is None:
            continue
        pending.append(_children(item))
    return size


_END = object()


def _children(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        yield from chain.from_iterable(dict.items(value))
    elif isinstance(value, list):
        yield from list.__iter__(value)
    elif isinstance(value, (tuple, set, frozenset)):
        yield from value
    attributes = getattr(value, "__dict__", None)
    if attributes is not None:
        yield attributes
    for cls in type(value).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name in ("__dict__", "__weakref__"):
                continue
            if name.startswith("__") and not name.endswith("__"):
                name = f"_{cls.__name__.lstrip('_')}{name}"
            try:
                yield object.__getattribute__(value, name)
            except AttributeError:
                pass  # An unset slot retains no value.

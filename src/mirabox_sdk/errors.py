"""Structured protocol errors raised while parsing MiraBox messages."""

from __future__ import annotations

from collections.abc import Sequence

PathComponent = str | int


def _format_path(path: Sequence[PathComponent]) -> str:
    result = "$"
    for component in path:
        if isinstance(component, int):
            result += f"[{component}]"
        else:
            result += f".{component}"
    return result


class StreamDockProtocolError(ValueError):
    """Base error containing a reason, event name, and failing JSON path.

    Attributes:
        reason: Human-readable explanation without event or path prefixes.
        event_name: Wire event associated with the failure, when known.
        path: Immutable sequence of string keys and integer array indexes from
            the JSON root to the failing value.

    ``str(error)`` renders paths in a JSONPath-like form such as
    ``$.payload.settings.count`` or ``$.devices[0].id``.
    """

    def __init__(
        self,
        reason: str,
        *,
        event_name: str | None = None,
        path: Sequence[PathComponent] = (),
    ) -> None:
        """Create a structured protocol error.

        Args:
            reason: Human-readable failure explanation.
            event_name: Optional wire event associated with the failure.
            path: Object keys and array indexes locating the invalid value.
        """

        self.reason = reason
        self.event_name = event_name
        self.path = tuple(path)
        event = f"event {event_name!r}, " if event_name is not None else ""
        super().__init__(f"{event}{_format_path(self.path)}: {reason}")


class MalformedEventError(StreamDockProtocolError):
    """Raised when a decoded value is not a valid Stream Dock event envelope."""


class InvalidFieldError(MalformedEventError):
    """Raised when a known event has a missing or type-invalid field."""


class UnsupportedEventError(StreamDockProtocolError):
    """Raised for an unknown event when forward compatibility is disabled."""

    def __init__(self, event_name: str) -> None:
        super().__init__(
            "unsupported Stream Dock event",
            event_name=event_name,
            path=("event",),
        )


class InvalidRegistrationInfoError(StreamDockProtocolError):
    """Raised when Stream Dock ``-info`` data violates the registration schema."""


class InvalidPluginLaunchArgumentsError(StreamDockProtocolError):
    """Raised when executable launch arguments are missing or outside valid ranges."""


class JsonCodecError(StreamDockProtocolError):
    """Base error for conversion between JSON objects and plugin-owned values."""


class JsonCodecDecodeError(JsonCodecError):
    """Raised when a JSON object cannot become the requested typed value."""


class JsonCodecEncodeError(JsonCodecError):
    """Raised when a typed value cannot become a finite JSON object."""

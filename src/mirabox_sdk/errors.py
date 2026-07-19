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
    """Base error containing the event name and failing JSON path."""

    def __init__(
        self,
        reason: str,
        *,
        event_name: str | None = None,
        path: Sequence[PathComponent] = (),
    ) -> None:
        self.reason = reason
        self.event_name = event_name
        self.path = tuple(path)
        event = f"event {event_name!r}, " if event_name is not None else ""
        super().__init__(f"{event}{_format_path(self.path)}: {reason}")


class MalformedEventError(StreamDockProtocolError):
    """The decoded message is not a valid Stream Dock event envelope."""


class InvalidFieldError(MalformedEventError):
    """A known event contains a missing or invalid field."""


class UnsupportedEventError(StreamDockProtocolError):
    """The event name is unknown and unknown events were explicitly disabled."""

    def __init__(self, event_name: str) -> None:
        super().__init__(
            "unsupported Stream Dock event",
            event_name=event_name,
            path=("event",),
        )


class InvalidRegistrationInfoError(StreamDockProtocolError):
    """The Stream Dock ``-info`` argument does not match the registration contract."""


class InvalidPluginLaunchArgumentsError(StreamDockProtocolError):
    """The executable launch arguments are inconsistent or outside valid ranges."""


class JsonCodecError(StreamDockProtocolError):
    """Base error raised while converting between JSON objects and typed values."""


class JsonCodecDecodeError(JsonCodecError):
    """A JSON object cannot be decoded into the requested typed value."""


class JsonCodecEncodeError(JsonCodecError):
    """A typed value cannot be encoded as a JSON object."""

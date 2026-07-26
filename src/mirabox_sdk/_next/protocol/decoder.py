"""Strict decoding of Stream Dock text frames into typed events."""

from __future__ import annotations

import json
from typing import NoReturn

from ...errors import MalformedEventError
from ...events import StreamDockEvent
from .ports import DecodedEventParser, StreamDockEventDecoder


def _reject_non_finite_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {value!r}")


class JsonStreamDockEventDecoder(StreamDockEventDecoder):
    """Decode strict JSON and pass decoded values to an event parser port.

    JSON syntax and non-finite constants are normalized to
    :class:`MalformedEventError`, so callers can handle every invalid inbound
    protocol frame through the existing structured error hierarchy.
    """

    __slots__ = ("_event_parser",)

    def __init__(self, event_parser: DecodedEventParser) -> None:
        self._event_parser = event_parser

    def decode(self, frame: str) -> StreamDockEvent:
        """Return the typed event represented by ``frame``.

        Raises:
            TypeError: If ``frame`` is not a text frame.
            MalformedEventError: If the frame is not strict JSON or does not
                contain a valid event envelope.
            InvalidFieldError: If a known event contains an invalid field.
        """

        if not isinstance(frame, str):
            raise TypeError("frame must be a string")

        try:
            value = json.loads(
                frame,
                parse_constant=_reject_non_finite_json_constant,
            )
        except json.JSONDecodeError as exc:
            raise MalformedEventError(
                f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
            ) from exc
        except ValueError as exc:
            raise MalformedEventError(f"invalid JSON: {exc}") from exc

        return self._event_parser.parse(value)

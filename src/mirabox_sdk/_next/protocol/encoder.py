"""Encoding of typed Stream Dock commands into strict JSON text frames."""

from __future__ import annotations

import json

from ...commands import StreamDockCommand, ValidatedWireMessage
from .ports import StreamDockCommandEncoder


class JsonStreamDockCommandEncoder(StreamDockCommandEncoder):
    """Serialize validated command envelopes exactly once as strict JSON."""

    __slots__ = ()

    def encode(self, command: StreamDockCommand) -> str:
        """Return the strict JSON text frame for ``command``.

        The command owns wire-envelope construction and validation. This codec
        performs the only JSON serialization step and preserves Unicode in the
        resulting text frame.

        Raises:
            TypeError: If the command does not return a
                :class:`ValidatedWireMessage`.
            ValueError: If the command contains a non-JSON value.
        """

        wire_message = command.to_validated_wire()
        if not isinstance(wire_message, ValidatedWireMessage):
            raise TypeError("command.to_validated_wire() must return ValidatedWireMessage")

        try:
            return json.dumps(
                wire_message._json_object(),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ValueError("Stream Dock command contains a non-JSON value") from None

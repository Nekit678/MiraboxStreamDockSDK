"""WebSocket transport for typed Stream Dock commands and events."""

from __future__ import annotations

import json
import logging
from typing import Any

import websocket

from .commands import StreamDockCommand
from .errors import StreamDockProtocolError
from .json_types import is_json_value
from .logging_config import _protocol_payload_logging_enabled
from .parser import parse_stream_dock_event
from .protocols import StreamDockConnection, StreamDockListener

logger = logging.getLogger(__name__)

_REDACTED = "<redacted>"
_LOGGABLE_PROTOCOL_FIELDS = ("event", "action", "context", "device", "uuid")


def _redact_protocol_message(message: object) -> object:
    if not isinstance(message, dict):
        return _REDACTED

    redacted = {
        field: value if isinstance(value, str) else _REDACTED
        for field in _LOGGABLE_PROTOCOL_FIELDS
        if (value := message.get(field)) is not None
    }
    if "payload" in message:
        redacted["payload"] = _REDACTED
    return redacted


def _log_protocol_message(direction: str, message: object) -> None:
    event = message.get("event") if isinstance(message, dict) else None
    context = message.get("context") if isinstance(message, dict) else None
    logger.info(
        "%s: event=%r context=%r",
        direction,
        event if isinstance(event, str) else None,
        context if isinstance(context, str) else None,
    )
    if logger.isEnabledFor(logging.DEBUG):
        log_message = (
            message if _protocol_payload_logging_enabled() else _redact_protocol_message(message)
        )
        logger.debug(
            "%s message: %s",
            direction,
            json.dumps(log_message, ensure_ascii=False),
        )


class WebSocketStreamDockConnection(StreamDockConnection):
    """Translate between Stream Dock WebSocket frames and typed SDK messages.

    The connection always targets ``127.0.0.1`` using the port supplied by the
    host application. Incoming malformed JSON and invalid protocol events are
    logged and ignored so one bad frame does not terminate the receive loop.
    Outgoing commands are validated as finite JSON before transmission.

    Args:
        port: Loopback WebSocket port supplied in the plugin launch arguments.

    Note:
        Payload fields are redacted from DEBUG protocol logs unless the
        application explicitly opts in with ``configure_logging``.
    """

    def __init__(self, port: int) -> None:
        """Create a loopback WebSocket client for the supplied host port."""

        self._listener: StreamDockListener | None = None
        self._ws = websocket.WebSocketApp(
            f"ws://127.0.0.1:{port}",
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

    def set_listener(self, listener: StreamDockListener) -> None:
        """Replace the listener receiving connection and parsed event callbacks.

        Args:
            listener: Object implementing :class:`StreamDockListener`.
        """

        self._listener = listener

    def run_forever(self) -> None:
        """Run the WebSocket event loop until the connection closes."""

        self._ws.run_forever()

    def close(self) -> None:
        """Request WebSocket shutdown.

        Calling this method delegates to ``websocket-client`` and is safe for
        the runtime to attempt during final cleanup.
        """

        self._ws.close()

    def send(self, command: StreamDockCommand) -> None:
        """Serialize, validate, log, and transmit one typed command.

        Args:
            command: Command whose :meth:`StreamDockCommand.to_wire` method
                returns the outgoing envelope.

        Raises:
            ValueError: If the command contains a non-JSON value or a non-finite
                floating-point number.
            TypeError: If JSON serialization cannot encode the envelope.
            WebSocketException: If the underlying connection cannot send the
                frame.
        """

        message = command.to_wire()
        if not is_json_value(message):
            raise ValueError("Stream Dock command contains a non-JSON value")
        raw_message = json.dumps(message, ensure_ascii=False, allow_nan=False)
        _log_protocol_message("Plugin -> Stream Dock", message)
        self._ws.send(raw_message)

    def _on_open(self, _ws: websocket.WebSocket) -> None:
        logger.info("Connected to Stream Dock")
        listener = self._listener
        if listener is not None:
            listener.on_stream_dock_connected()

    def _on_message(self, _ws: websocket.WebSocket, message: Any) -> None:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Ignoring invalid JSON from Stream Dock: %s", exc)
            return
        _log_protocol_message("Stream Dock -> Plugin", data)
        try:
            event = parse_stream_dock_event(data)
        except StreamDockProtocolError as exc:
            logger.warning("Ignoring malformed Stream Dock event: %s", exc)
            return

        listener = self._listener
        if listener is not None:
            listener.on_stream_dock_event(event)

    def _on_error(self, _ws: websocket.WebSocket, error: Any) -> None:
        logger.error("Stream Dock WebSocket error: %s", error)

    def _on_close(
        self,
        _ws: websocket.WebSocket,
        status_code: Any,
        message: Any,
    ) -> None:
        logger.info("Stream Dock connection closed: %s %s", status_code, message or "")

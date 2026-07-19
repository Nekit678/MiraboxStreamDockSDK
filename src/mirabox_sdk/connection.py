from __future__ import annotations

import json
import logging
from typing import Any

import websocket

from .commands import StreamDockCommand
from .errors import StreamDockProtocolError
from .json_types import is_json_value
from .parser import parse_stream_dock_event
from .protocols import StreamDockConnection, StreamDockListener

logger = logging.getLogger(__name__)

_REDACTED = "<redacted>"
_GLOBAL_SETTINGS_EVENTS = frozenset({"didReceiveGlobalSettings", "setGlobalSettings"})
_SENSITIVE_KEY_SUFFIXES = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "secretkey",
    "token",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return normalized == "globalsettings" or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_sensitive_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if isinstance(key, str) and _is_sensitive_key(key)
                else _redact_sensitive_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_fields(item) for item in value]
    return value


def _redact_protocol_message(message: object) -> object:
    redacted = _redact_sensitive_fields(message)
    if not isinstance(message, dict) or not isinstance(redacted, dict):
        return redacted
    event = message.get("event")
    if isinstance(event, str) and event in _GLOBAL_SETTINGS_EVENTS and "payload" in redacted:
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
        logger.debug(
            "%s message: %s",
            direction,
            json.dumps(_redact_protocol_message(message), ensure_ascii=False),
        )


class WebSocketStreamDockConnection(StreamDockConnection):
    """Translate between Stream Dock WebSocket frames and plugin messages."""

    def __init__(self, port: int) -> None:
        self._listener: StreamDockListener | None = None
        self._ws = websocket.WebSocketApp(
            f"ws://127.0.0.1:{port}",
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

    def set_listener(self, listener: StreamDockListener) -> None:
        self._listener = listener

    def run_forever(self) -> None:
        self._ws.run_forever()

    def close(self) -> None:
        self._ws.close()

    def send(self, command: StreamDockCommand) -> None:
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

from __future__ import annotations

import json
import logging
from typing import Any

import websocket

from .commands import StreamDockCommand
from .errors import StreamDockProtocolError
from .parser import parse_stream_dock_event
from .protocols import StreamDockConnection, StreamDockListener

logger = logging.getLogger(__name__)


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
        raw_message = json.dumps(command.to_wire(), ensure_ascii=False)
        logger.info("Plugin -> Stream Dock: %s", raw_message)
        self._ws.send(raw_message)

    def _on_open(self, _ws: websocket.WebSocket) -> None:
        logger.info("Connected to Stream Dock")
        listener = self._listener
        if listener is not None:
            listener.on_stream_dock_connected()

    def _on_message(self, _ws: websocket.WebSocket, message: Any) -> None:
        logger.info("Stream Dock -> Plugin: %s", message)
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Ignoring invalid JSON from Stream Dock: %s", exc)
            return
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

"""WebSocket transport for typed Stream Dock commands and events."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import websocket

from .commands import StreamDockCommand, ValidatedWireMessage
from .errors import StreamDockProtocolError
from .events import StreamDockEvent
from .inbound import (
    InboundOverflowPolicy,
    InboundQueueMetrics,
    _InboundEventDispatcher,
)
from .logging_config import _protocol_payload_logging_enabled
from .outbound import OutboundQueueMetrics, _OutboundCommandBus
from .parser import parse_stream_dock_event
from .protocols import StreamDockConnection, StreamDockListener

logger = logging.getLogger(__name__)

_REDACTED = "<redacted>"
_LOGGABLE_PROTOCOL_FIELDS = ("event", "action", "context", "device", "uuid")


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


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


def _log_protocol_message(
    direction: str,
    message: object,
    *,
    serialized_message: str | None = None,
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    event = message.get("event") if isinstance(message, dict) else None
    context = message.get("context") if isinstance(message, dict) else None
    logger.debug(
        "%s: event=%r context=%r",
        direction,
        event if isinstance(event, str) else None,
        context if isinstance(context, str) else None,
    )
    if not _protocol_payload_logging_enabled():
        logger.debug("%s message: %r", direction, _redact_protocol_message(message))
        return

    if serialized_message is None:
        serialized_message = json.dumps(message, ensure_ascii=False)
    logger.debug("%s message: %s", direction, serialized_message)


class WebSocketStreamDockConnection(StreamDockConnection):
    """Translate between Stream Dock WebSocket frames and typed SDK messages.

    The connection always targets ``127.0.0.1`` using the port supplied by the
    host application. Incoming malformed JSON and invalid protocol events are
    logged and ignored so one bad frame does not terminate the receive loop.
    One connection-owned outbound writer validates, serializes, logs, and
    transmits every command in queue order.

    Args:
        port: Loopback WebSocket port supplied in the plugin launch arguments.
        inbound_queue_limit: Maximum number of parsed events waiting for
            callback dispatch.
        overflow_policy: Non-blocking policy applied when the queue is full.
        coalesce_dial_rotations: Combine compatible queued rotations for the
            same action context by summing their ticks.
        inbound_shutdown_timeout: Maximum seconds to wait for queued callbacks
            during shutdown. ``None`` waits until the queue drains.
        outbound_queue_limit: Maximum number of commands waiting for the single
            WebSocket writer.
        coalesce_outbound_commands: Replace adjacent pending state-setting
            commands for the same target with the newest value.
        outbound_shutdown_timeout: Maximum seconds to wait for queued commands
            during shutdown. ``None`` waits until the queue drains.

    Note:
        Payload fields are redacted from DEBUG protocol logs unless the
        application explicitly opts in with ``configure_logging``.
    """

    def __init__(
        self,
        port: int,
        *,
        inbound_queue_limit: int = 1024,
        overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
        coalesce_dial_rotations: bool = False,
        inbound_shutdown_timeout: float | None = None,
        outbound_queue_limit: int = 1024,
        coalesce_outbound_commands: bool = False,
        outbound_shutdown_timeout: float | None = None,
    ) -> None:
        """Create a loopback WebSocket client for the supplied host port."""

        if type(inbound_queue_limit) is not int or inbound_queue_limit <= 0:
            raise ValueError("inbound_queue_limit must be a positive integer")
        try:
            overflow_policy = InboundOverflowPolicy(overflow_policy)
        except (TypeError, ValueError):
            raise ValueError(
                f"overflow_policy must be one of: "
                f"{', '.join(policy.value for policy in InboundOverflowPolicy)}"
            ) from None
        if type(coalesce_dial_rotations) is not bool:
            raise ValueError("coalesce_dial_rotations must be a boolean")
        if inbound_shutdown_timeout is not None and (
            type(inbound_shutdown_timeout) not in (int, float)
            or not math.isfinite(inbound_shutdown_timeout)
            or inbound_shutdown_timeout < 0
        ):
            raise ValueError(
                "inbound_shutdown_timeout must be a finite non-negative number or None"
            )
        if type(outbound_queue_limit) is not int or outbound_queue_limit <= 0:
            raise ValueError("outbound_queue_limit must be a positive integer")
        if type(coalesce_outbound_commands) is not bool:
            raise ValueError("coalesce_outbound_commands must be a boolean")
        if outbound_shutdown_timeout is not None and (
            type(outbound_shutdown_timeout) not in (int, float)
            or not math.isfinite(outbound_shutdown_timeout)
            or outbound_shutdown_timeout < 0
        ):
            raise ValueError(
                "outbound_shutdown_timeout must be a finite non-negative number or None"
            )

        self._listener: StreamDockListener | None = None
        self._inbound_shutdown_timeout = inbound_shutdown_timeout
        self._outbound_shutdown_timeout = outbound_shutdown_timeout
        self._inbound = _InboundEventDispatcher(
            queue_limit=inbound_queue_limit,
            overflow_policy=overflow_policy,
            coalesce_dial_rotations=coalesce_dial_rotations,
            dispatch=self._dispatch_inbound_event,
        )
        self._ws = websocket.WebSocketApp(
            f"ws://127.0.0.1:{port}",
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._outbound = _OutboundCommandBus(
            queue_limit=outbound_queue_limit,
            coalesce_commands=coalesce_outbound_commands,
            serialize=self._serialize_outbound_command,
            write=self._ws.send,
        )

    def set_listener(self, listener: StreamDockListener) -> None:
        """Replace the listener receiving connection and parsed event callbacks.

        Args:
            listener: Object implementing :class:`StreamDockListener`.
        """

        self._listener = listener

    def run_forever(self) -> None:
        """Run the WebSocket loop and drain callbacks and commands after it closes."""

        self._outbound.start()
        self._inbound.start()
        try:
            self._ws.run_forever()
        finally:
            self._inbound.stop_accepting()
            try:
                self._shutdown_inbound_dispatcher()
            finally:
                self._outbound.stop_accepting()
                self._shutdown_outbound_bus()

    def close(self) -> None:
        """Close the WebSocket and gracefully drain queued callbacks and commands.

        New inbound events are rejected first. Queued callbacks drain while
        outbound commands are still accepted, then the single writer drains
        before the WebSocket closes. Configured shutdown timeouts can bound
        either wait.
        """

        self._inbound.stop_accepting()
        try:
            self._shutdown_inbound_dispatcher()
        finally:
            self._outbound.stop_accepting()
            try:
                self._shutdown_outbound_bus()
            finally:
                self._ws.close()

    @property
    def inbound_queue_metrics(self) -> InboundQueueMetrics:
        """Return a thread-safe snapshot of inbound queue metrics."""

        return self._inbound.metrics()

    @property
    def outbound_queue_metrics(self) -> OutboundQueueMetrics:
        """Return a thread-safe snapshot of outbound queue metrics."""

        return self._outbound.metrics()

    def send(self, command: StreamDockCommand) -> None:
        """Submit one typed command to the single outbound writer.

        Args:
            command: Command whose :meth:`StreamDockCommand.to_wire` method
                returns the outgoing envelope.

        Raises:
            ValueError: If the command contains a value outside
                :data:`JsonValue` or a non-finite floating-point number.
            OutboundQueueFullError: If the bounded command queue is full.
            OutboundCommandBusClosedError: If connection shutdown has begun.
            WebSocketException: If the writer cannot send the frame.

        The single writer completes serialization and transport I/O before this
        method returns, preserving direct error reporting without writing from
        the caller's thread.
        """

        self._outbound.submit(command)

    def _serialize_outbound_command(self, command: StreamDockCommand) -> str:
        wire_message = command.to_validated_wire()
        if not isinstance(wire_message, ValidatedWireMessage):
            raise TypeError("command.to_validated_wire() must return ValidatedWireMessage")
        message = wire_message._json_object()
        try:
            raw_message = json.dumps(message, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("Stream Dock command contains a non-JSON value") from None
        _log_protocol_message(
            "Plugin -> Stream Dock",
            message,
            serialized_message=raw_message,
        )
        return raw_message

    def _on_open(self, _ws: websocket.WebSocket) -> None:
        logger.info("Connected to Stream Dock")
        listener = self._listener
        if listener is not None:
            listener.on_stream_dock_connected()

    def _on_message(self, _ws: websocket.WebSocket, message: Any) -> None:
        try:
            data = json.loads(message, parse_constant=_reject_non_finite_json_constant)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid JSON from Stream Dock: %s", exc)
            return
        _log_protocol_message(
            "Stream Dock -> Plugin",
            data,
            serialized_message=message if isinstance(message, str) else None,
        )
        try:
            event = parse_stream_dock_event(data)
        except StreamDockProtocolError as exc:
            logger.warning("Ignoring malformed Stream Dock event: %s", exc)
            return

        if not self._inbound.submit(event):
            logger.warning(
                "Dropping inbound Stream Dock event %s: queue policy rejected it",
                event.event_name,
            )

    def _on_error(self, _ws: websocket.WebSocket, error: Any) -> None:
        logger.error("Stream Dock WebSocket error: %s", error)

    def _on_close(
        self,
        _ws: websocket.WebSocket,
        status_code: Any,
        message: Any,
    ) -> None:
        logger.info("Stream Dock connection closed: %s %s", status_code, message or "")

    def _dispatch_inbound_event(self, event: StreamDockEvent) -> bool:
        listener = self._listener
        if listener is None:
            logger.warning(
                "Dropping inbound Stream Dock event %s: no listener is attached",
                event.event_name,
            )
            return False
        listener.on_stream_dock_event(event)
        return True

    def _shutdown_inbound_dispatcher(self) -> None:
        if not self._inbound.shutdown(timeout=self._inbound_shutdown_timeout):
            timeout = self._inbound_shutdown_timeout
            if timeout is None:  # pragma: no cover - an unbounded join cannot time out
                raise AssertionError("unbounded inbound queue shutdown timed out")
            logger.warning(
                "Inbound Stream Dock event queue did not drain within %.3f seconds; "
                "pending events were discarded",
                timeout,
            )

    def _shutdown_outbound_bus(self) -> None:
        if not self._outbound.shutdown(timeout=self._outbound_shutdown_timeout):
            timeout = self._outbound_shutdown_timeout
            if timeout is None:  # pragma: no cover - an unbounded join cannot time out
                raise AssertionError("unbounded outbound queue shutdown timed out")
            logger.warning(
                "Outbound Stream Dock command queue did not drain within %.3f seconds; "
                "pending commands were discarded",
                timeout,
            )

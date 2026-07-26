"""API-independent WebSocket transport connector."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import InvalidStateError
from math import isfinite
from threading import Condition, Event, Lock, Thread, current_thread
from time import monotonic
from typing import Any, Protocol, cast

import websocket

from .frames import OutboundFrame, TransportReceipt
from .metrics import WebSocketConnectorMetrics
from .ports import (
    QueueAcceptanceControl,
    RawInboundSink,
    RawOutboundSource,
    SessionEventSink,
    WebSocketConnector,
)
from .queues import TransportQueueClosedError
from .session import Connected, Disconnected, SessionEvent, TransportError

logger = logging.getLogger(__name__)

_DEFAULT_OUTBOUND_SHUTDOWN_TIMEOUT = 5.0
_RECEIVE_POLL_INTERVAL = 0.05
_SENDER_JOIN_GRACE = 0.1


class WebSocketConnectorError(RuntimeError):
    """Base error for connector lifecycle and transport failures."""


class WebSocketConnectorLifecycleError(WebSocketConnectorError):
    """Report an invalid connector lifecycle transition."""


class UnsupportedWebSocketFrameError(WebSocketConnectorError):
    """Report a non-text WebSocket protocol message."""


class WebSocketDisconnectedError(WebSocketConnectorError):
    """Report an outbound frame that could not survive disconnection."""


class WebSocketConnectorClosedError(WebSocketConnectorError):
    """Report an outbound frame discarded during bounded shutdown."""


class _WebSocketApp(Protocol):
    def run_forever(self) -> object: ...

    def send(self, data: str) -> object: ...

    def close(self) -> object: ...


WebSocketAppFactory = Callable[..., _WebSocketApp]


class WebSocketClientConnector(WebSocketConnector):
    """Forward text frames and serialized outbound data over ``websocket-client``.

    The connector owns one outbound sender thread. It understands WebSocket
    lifecycle and text frames only; Stream Dock protocol models and JSON codecs
    remain outside the transport layer.
    """

    def __init__(
        self,
        port: int,
        raw_inbound_sink: RawInboundSink,
        raw_outbound_source: RawOutboundSource,
        session_event_sink: SessionEventSink,
        *,
        outbound_shutdown_timeout: float | None = _DEFAULT_OUTBOUND_SHUTDOWN_TIMEOUT,
        websocket_app_factory: WebSocketAppFactory = websocket.WebSocketApp,
    ) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")

        self._outbound_shutdown_timeout = _validate_timeout(outbound_shutdown_timeout)
        self._raw_inbound = raw_inbound_sink
        self._raw_outbound = raw_outbound_source
        self._session_events = session_event_sink

        self._condition = Condition()
        self._close_lock = Lock()
        self._close_completed = Event()
        self._sender_thread: Thread | None = None
        self._lifecycle_thread: Thread | None = None
        self._started = False
        self._terminal = False
        self._closing = False
        self._connected = False
        self._drain_requested = False
        self._stop_requested = False
        self._sender_stopped = False
        self._in_flight: OutboundFrame | None = None
        self._disconnected_published = False
        self._websocket_close_requested = False

        self._connect_count = 0
        self._disconnect_count = 0
        self._last_close_code: int | None = None
        self._transport_error_count = 0
        self._session_events_rejected = 0
        self._inbound_frames_received = 0
        self._inbound_frames_forwarded = 0
        self._inbound_frames_rejected = 0
        self._binary_frames_rejected = 0
        self._outbound_frames_received = 0
        self._outbound_frames_sent = 0
        self._outbound_send_failures = 0
        self._outbound_drain_timeouts = 0
        self._outbound_discarded_during_shutdown = 0

        self._ws = cast(
            _WebSocketApp,
            websocket_app_factory(
                f"ws://127.0.0.1:{port}",
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            ),
        )

    def run_forever(self) -> None:
        """Run the WebSocket loop once and terminate the owned sender with it."""

        with self._condition:
            if self._started:
                raise WebSocketConnectorLifecycleError("Connector can only be run once")
            if self._terminal or self._closing:
                raise WebSocketConnectorLifecycleError("Connector has already been closed")

            sender = Thread(
                target=self._run_sender,
                name="mirabox-next-websocket-sender",
                daemon=True,
            )
            self._sender_thread = sender
            self._lifecycle_thread = current_thread()
            self._started = True
            try:
                sender.start()
            except Exception:
                self._sender_thread = None
                self._lifecycle_thread = None
                self._started = False
                raise

        try:
            self._ws.run_forever()
        except Exception as exc:
            self._publish_transport_error(exc)
            raise
        finally:
            self._finalize_run()

    def close(self) -> None:
        """Idempotently drain accepted outbound frames and close the socket."""

        with self._close_lock:
            if self._close_completed.is_set():
                return
            if self._closing:
                owns_close = False
            else:
                self._closing = True
                owns_close = True
                with self._condition:
                    self._condition.notify_all()

        if not owns_close:
            with self._condition:
                called_from_worker = current_thread() in (
                    self._sender_thread,
                    self._lifecycle_thread,
                )
            if not called_from_worker:
                self._close_completed.wait()
            return

        try:
            self._close_owned()
        finally:
            self._close_completed.set()

    def metrics(self) -> WebSocketConnectorMetrics:
        """Return an atomic immutable snapshot of connector counters."""

        with self._condition:
            return WebSocketConnectorMetrics(
                connect_count=self._connect_count,
                disconnect_count=self._disconnect_count,
                last_close_code=self._last_close_code,
                transport_error_count=self._transport_error_count,
                session_events_rejected=self._session_events_rejected,
                inbound_frames_received=self._inbound_frames_received,
                inbound_frames_forwarded=self._inbound_frames_forwarded,
                inbound_frames_rejected=self._inbound_frames_rejected,
                binary_frames_rejected=self._binary_frames_rejected,
                outbound_frames_received=self._outbound_frames_received,
                outbound_frames_sent=self._outbound_frames_sent,
                outbound_send_failures=self._outbound_send_failures,
                outbound_drain_timeouts=self._outbound_drain_timeouts,
                outbound_discarded_during_shutdown=(self._outbound_discarded_during_shutdown),
            )

    def _run_sender(self) -> None:
        try:
            while self._wait_until_connected():
                keep_running, frame = self._receive_outbound_frame()
                if not keep_running:
                    return
                if frame is None:
                    continue
                if not self._send_frame(frame):
                    return
        finally:
            with self._condition:
                self._sender_stopped = True
                self._condition.notify_all()

    def _wait_until_connected(self) -> bool:
        with self._condition:
            while not self._connected:
                if self._stop_requested or self._drain_requested:
                    return False
                self._condition.wait()
            return True

    def _receive_outbound_frame(self) -> tuple[bool, OutboundFrame | None]:
        try:
            frame = self._raw_outbound.receive(timeout=_RECEIVE_POLL_INTERVAL)
        except TimeoutError:
            with self._condition:
                keep_running = not (self._stop_requested or self._drain_requested)
            return keep_running, None
        except TransportQueueClosedError:
            return False, None
        except Exception as exc:
            self._publish_transport_error(exc)
            self._abort_transport(
                WebSocketDisconnectedError(f"Raw outbound source failed with {type(exc).__name__}")
            )
            return False, None

        if not isinstance(frame, OutboundFrame):
            error = TypeError("Raw outbound source must return OutboundFrame")
            self._publish_transport_error(error)
            self._abort_transport(WebSocketDisconnectedError(str(error)))
            return False, None
        return True, frame

    def _send_frame(self, frame: OutboundFrame) -> bool:
        with self._condition:
            self._outbound_frames_received += 1
            self._in_flight = frame
            should_send = self._connected and not self._stop_requested
            self._condition.notify_all()

        if not should_send:
            if self._finish_receipt(
                frame.receipt,
                error=WebSocketDisconnectedError("WebSocket disconnected before frame send"),
            ):
                with self._condition:
                    self._outbound_discarded_during_shutdown += 1
            self._clear_in_flight(frame)
            return False

        if not isinstance(frame.payload, str):
            error = TypeError("OutboundFrame payload must be a text frame")
            if self._finish_receipt(frame.receipt, error=error):
                with self._condition:
                    self._outbound_send_failures += 1
            self._publish_transport_error(error)
            self._clear_in_flight(frame)
            return True

        try:
            self._ws.send(frame.payload)
        except Exception as exc:
            if self._finish_receipt(frame.receipt, error=exc):
                with self._condition:
                    self._outbound_send_failures += 1
            self._publish_transport_error(exc)
            self._clear_in_flight(frame)
            self._abort_transport(
                WebSocketDisconnectedError(f"WebSocket send failed with {type(exc).__name__}")
            )
            return False

        if self._finish_receipt(frame.receipt):
            with self._condition:
                self._outbound_frames_sent += 1
        self._clear_in_flight(frame)
        return True

    def _on_open(self, _ws: object) -> None:
        with self._condition:
            if self._closing or self._stop_requested or self._terminal:
                should_close = True
            else:
                should_close = False
                self._connected = True
                self._connect_count += 1
                self._condition.notify_all()

        if should_close:
            self._safe_close_websocket(force=True)
            return
        self._publish_session_event(Connected())

    def _on_message(self, _ws: object, message: Any) -> None:
        with self._condition:
            if self._closing or self._stop_requested or not self._connected:
                self._inbound_frames_rejected += 1
                return
            self._inbound_frames_received += 1

        if not isinstance(message, str):
            with self._condition:
                self._binary_frames_rejected += 1
                self._inbound_frames_rejected += 1
            self._publish_transport_error(
                UnsupportedWebSocketFrameError(
                    "Stream Dock transport supports WebSocket text frames only"
                )
            )
            return

        try:
            accepted = self._raw_inbound.submit(message)
        except Exception as exc:
            with self._condition:
                self._inbound_frames_rejected += 1
            self._publish_transport_error(exc)
            return

        with self._condition:
            if accepted:
                self._inbound_frames_forwarded += 1
            else:
                self._inbound_frames_rejected += 1

    def _on_error(self, _ws: object, error: Any) -> None:
        self._publish_transport_error(_as_exception(error))

    def _on_close(
        self,
        _ws: object,
        status_code: Any,
        reason: Any,
    ) -> None:
        close_code = status_code if type(status_code) is int else None
        close_reason = reason if isinstance(reason, str) else None
        self._handle_disconnect(close_code, close_reason)

    def _close_owned(self) -> None:
        self._stop_accepting(self._raw_inbound, "Raw inbound sink")
        self._stop_accepting(self._raw_outbound, "Raw outbound source")

        with self._condition:
            self._drain_requested = True
            sender = self._sender_thread
            called_from_sender = sender is current_thread()
            self._condition.notify_all()

        drained = sender is None
        if sender is not None and not called_from_sender:
            drained = self._wait_for_sender(self._outbound_shutdown_timeout)

        if not drained:
            with self._condition:
                self._outbound_drain_timeouts += 1
                self._stop_requested = True
                self._connected = False
                self._condition.notify_all()
            error = WebSocketConnectorClosedError(
                "WebSocket connector shutdown timed out before outbound drain"
            )
            self._discard_outbound(error)
        elif called_from_sender:
            with self._condition:
                self._stop_requested = True
                self._connected = False
                self._condition.notify_all()
            self._discard_outbound(
                WebSocketConnectorClosedError("WebSocket connector closed from its sender thread")
            )

        self._safe_close_websocket()

        if sender is not None and not called_from_sender and sender.is_alive():
            sender.join(_SENDER_JOIN_GRACE)

        if sender is None:
            self._discard_outbound(
                WebSocketConnectorClosedError(
                    "WebSocket connector closed before outbound sender started"
                )
            )
            with self._condition:
                self._terminal = True
                self._stop_requested = True
                self._sender_stopped = True
                self._condition.notify_all()

    def _handle_disconnect(
        self,
        status_code: int | None,
        reason: str | None,
    ) -> None:
        with self._condition:
            self._connected = False
            self._stop_requested = True
            publish = not self._disconnected_published
            if publish:
                self._disconnected_published = True
                self._disconnect_count += 1
                self._last_close_code = status_code
            self._condition.notify_all()

        self._stop_accepting(self._raw_inbound, "Raw inbound sink")
        self._stop_accepting(self._raw_outbound, "Raw outbound source")
        self._discard_outbound(
            WebSocketDisconnectedError("WebSocket disconnected before frame send")
        )
        if publish:
            self._publish_session_event(Disconnected(status_code=status_code, reason=reason))

    def _abort_transport(self, error: Exception) -> None:
        with self._condition:
            self._connected = False
            self._stop_requested = True
            self._condition.notify_all()
        self._stop_accepting(self._raw_inbound, "Raw inbound sink")
        self._stop_accepting(self._raw_outbound, "Raw outbound source")
        self._discard_outbound(error)
        self._safe_close_websocket()

    def _finalize_run(self) -> None:
        self._handle_disconnect(None, None)
        sender = self._sender_thread
        if sender is not None and sender is not current_thread() and sender.is_alive():
            sender.join(_SENDER_JOIN_GRACE)

        with self._condition:
            self._terminal = True
            self._connected = False
            self._stop_requested = True
            self._lifecycle_thread = None
            close_in_progress = self._closing
            self._condition.notify_all()
        if not close_in_progress:
            self._close_completed.set()

    def _wait_for_sender(self, timeout: float | None) -> bool:
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while not self._sender_stopped:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _discard_outbound(self, error: Exception) -> None:
        with self._condition:
            in_flight = self._in_flight

        if in_flight is not None and self._finish_receipt(in_flight.receipt, error=error):
            with self._condition:
                self._outbound_discarded_during_shutdown += 1

        while True:
            try:
                frame = self._raw_outbound.receive(timeout=0)
            except (TimeoutError, TransportQueueClosedError):
                return
            except Exception as exc:
                logger.error(
                    "Raw outbound source cleanup failed with %s",
                    type(exc).__name__,
                )
                return

            if not isinstance(frame, OutboundFrame):
                logger.error(
                    "Raw outbound source returned %s during cleanup",
                    type(frame).__name__,
                )
                continue
            if self._finish_receipt(frame.receipt, error=error):
                with self._condition:
                    self._outbound_discarded_during_shutdown += 1

    def _publish_transport_error(self, error: Exception) -> None:
        logger.error("WebSocket transport error (%s)", type(error).__name__)
        with self._condition:
            self._transport_error_count += 1
        self._publish_session_event(TransportError(error))

    def _publish_session_event(self, event: SessionEvent) -> None:
        try:
            accepted = self._session_events.submit(event)
        except Exception as exc:
            logger.error(
                "Session event sink rejected %s with %s",
                type(event).__name__,
                type(exc).__name__,
            )
            with self._condition:
                self._session_events_rejected += 1
            return
        if not accepted:
            with self._condition:
                self._session_events_rejected += 1

    def _safe_close_websocket(self, *, force: bool = False) -> None:
        with self._condition:
            if self._websocket_close_requested and not force:
                return
            self._websocket_close_requested = True
        try:
            self._ws.close()
        except Exception as exc:
            self._publish_transport_error(exc)

    def _clear_in_flight(self, frame: OutboundFrame) -> None:
        with self._condition:
            if self._in_flight is frame:
                self._in_flight = None
            self._condition.notify_all()

    @staticmethod
    def _finish_receipt(
        receipt: TransportReceipt,
        *,
        error: Exception | None = None,
    ) -> bool:
        try:
            receipt._finish(error=error)
        except InvalidStateError:
            return False
        return True

    @staticmethod
    def _stop_accepting(port: object, port_name: str) -> None:
        if not isinstance(port, QueueAcceptanceControl):
            return
        try:
            port.stop_accepting()
        except Exception as exc:
            logger.error(
                "%s shutdown failed with %s",
                port_name,
                type(exc).__name__,
            )


def _as_exception(error: Any) -> Exception:
    if isinstance(error, Exception):
        return error
    return WebSocketConnectorError(f"websocket-client reported {type(error).__name__}")


def _validate_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("outbound_shutdown_timeout must be a non-negative number or None")
    return float(timeout)

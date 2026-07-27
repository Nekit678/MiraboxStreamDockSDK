"""Explicit runtime integration for the experimental Stream Dock boundary.

Nothing in this module is re-exported from :mod:`mirabox_sdk`. Applications
must import it deliberately while the legacy WebSocket connection remains the
default supported implementation.
"""

from __future__ import annotations

import logging
from math import isfinite
from threading import Lock, Thread, current_thread

from ._next.boundary.composition import create_stream_dock_boundary
from ._next.boundary.config import BoundaryQueueConfig, BoundaryShutdownConfig
from ._next.boundary.ports import StreamDockBoundary, WebSocketConnectorFactory
from ._next.messaging.inbound import InboundEventQueueClosedError
from ._next.messaging.inbound import InboundOverflowPolicy as BoundaryInboundOverflowPolicy
from ._next.messaging.models import CommandFuture as BoundaryCommandFuture
from ._next.messaging.outbound import (
    OutboundCommandQueueClosedError as BoundaryCommandQueueClosedError,
)
from ._next.messaging.outbound import OutboundQueueFullError as BoundaryQueueFullError
from ._next.transport.queues import TransportQueueClosedError
from ._next.transport.session import Connected, Disconnected, SessionEvent, TransportError
from .commands import StreamDockCommand
from .inbound import InboundOverflowPolicy
from .outbound import CommandFuture, OutboundCommandBusClosedError, OutboundQueueFullError
from .protocols import StreamDockConnection, StreamDockListener

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_LIMIT = 1024
_DEFAULT_SESSION_QUEUE_LIMIT = 16
_DEFAULT_DISPATCHER_POLL_INTERVAL = 0.05
_DEFAULT_DISPATCHER_SHUTDOWN_TIMEOUT = 5.0


class ExperimentalBoundaryRuntimeError(RuntimeError):
    """Report a failure in the adapter between the current runtime and boundary."""


class _RuntimeCommandFuture(CommandFuture):
    """Expose the current runtime completion API over a boundary completion."""

    __slots__ = ("_completion",)

    def __init__(self, completion: BoundaryCommandFuture) -> None:
        self._completion = completion

    def done(self) -> bool:
        return self._completion.done()

    def wait(self, timeout: float | None = None) -> bool:
        try:
            self._completion.exception(timeout)
        except TimeoutError:
            return False
        return True

    def result(self, timeout: float | None = None) -> None:
        return self._completion.result(timeout)

    def exception(self, timeout: float | None = None) -> Exception | None:
        return self._completion.exception(timeout)


class BoundaryStreamDockConnection(StreamDockConnection):
    """Adapt a typed experimental boundary to the current runtime connection API.

    A single adapter-owned dispatcher consumes both lifecycle and inbound
    typed ports. It finishes the ``Connected`` callback before delivering the
    first protocol event, preserves inbound FIFO, and acknowledges each event
    after its runtime callback returns.
    """

    def __init__(
        self,
        boundary: StreamDockBoundary,
        *,
        dispatcher_poll_interval: float = _DEFAULT_DISPATCHER_POLL_INTERVAL,
        dispatcher_shutdown_timeout: float | None = _DEFAULT_DISPATCHER_SHUTDOWN_TIMEOUT,
    ) -> None:
        if not isinstance(boundary, StreamDockBoundary):
            raise TypeError("boundary must implement StreamDockBoundary")
        self._dispatcher_poll_interval = _validate_timeout(
            dispatcher_poll_interval,
            name="dispatcher_poll_interval",
            allow_none=False,
        )
        self._dispatcher_shutdown_timeout = _validate_timeout(
            dispatcher_shutdown_timeout,
            name="dispatcher_shutdown_timeout",
            allow_none=True,
        )
        self._boundary = boundary
        self._listener: StreamDockListener | None = None
        self._state_lock = Lock()
        self._run_started = False
        self._dispatcher_thread: Thread | None = None
        self._deferred_close_thread: Thread | None = None
        self._dispatcher_error: Exception | None = None

    @property
    def boundary(self) -> StreamDockBoundary:
        """Return the typed facade for experimental metrics and diagnostics."""

        return self._boundary

    def set_listener(self, listener: StreamDockListener) -> None:
        """Attach the current runtime listener before the connection starts."""

        with self._state_lock:
            if self._run_started:
                raise ExperimentalBoundaryRuntimeError(
                    "Cannot replace the Stream Dock listener after the boundary starts"
                )
            self._listener = listener

    def run_forever(self) -> None:
        """Run the typed boundary while dispatching its ports to the listener."""

        with self._state_lock:
            if self._run_started:
                raise ExperimentalBoundaryRuntimeError(
                    "Experimental Stream Dock connection can only be run once"
                )
            dispatcher = Thread(
                target=self._dispatch,
                name="mirabox-experimental-runtime-dispatcher",
                daemon=True,
            )
            self._run_started = True
            self._dispatcher_thread = dispatcher

        try:
            dispatcher.start()
        except Exception:
            self._boundary.close()
            raise

        try:
            self._boundary.run_forever()
        finally:
            dispatcher.join(self._dispatcher_shutdown_timeout)

        if dispatcher.is_alive():
            raise ExperimentalBoundaryRuntimeError(
                "Experimental runtime dispatcher did not stop before the shutdown timeout"
            )
        if self._dispatcher_error is not None:
            raise ExperimentalBoundaryRuntimeError(
                "Experimental runtime failed while handling the connected session event"
            ) from self._dispatcher_error

    def close(self) -> None:
        """Idempotently close the boundary without deadlocking an event callback."""

        with self._state_lock:
            called_from_dispatcher = current_thread() is self._dispatcher_thread
            deferred_close = self._deferred_close_thread
            if called_from_dispatcher and (deferred_close is None or not deferred_close.is_alive()):
                deferred_close = Thread(
                    target=self._boundary.close,
                    name="mirabox-experimental-runtime-close",
                    daemon=True,
                )
                self._deferred_close_thread = deferred_close

        if called_from_dispatcher:
            if deferred_close is not None and not deferred_close.is_alive():
                deferred_close.start()
            return
        self._boundary.close()

    def send(self, command: StreamDockCommand) -> None:
        """Submit one command and wait for boundary serialization and transport."""

        self.send_async(command).result()

    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Submit one command through the typed boundary command port."""

        try:
            completion = self._boundary.commands.send_async(command)
        except BoundaryQueueFullError as exc:
            raise OutboundQueueFullError(str(exc)) from exc
        except BoundaryCommandQueueClosedError as exc:
            raise OutboundCommandBusClosedError(str(exc)) from exc
        return _RuntimeCommandFuture(completion)

    def _dispatch(self) -> None:
        connected = False
        events_closed = False
        session_events_closed = False

        while not (events_closed and session_events_closed):
            if not session_events_closed:
                session_timeout = (
                    0.0 if connected and not events_closed else self._dispatcher_poll_interval
                )
                try:
                    session_event = self._boundary.session_events.receive(timeout=session_timeout)
                except TimeoutError:
                    pass
                except TransportQueueClosedError:
                    session_events_closed = True
                else:
                    connected = self._handle_session_event(session_event) or connected

            if connected and not events_closed:
                try:
                    event = self._boundary.events.receive(timeout=self._dispatcher_poll_interval)
                except TimeoutError:
                    continue
                except InboundEventQueueClosedError:
                    events_closed = True
                    continue
                except Exception as exc:
                    logger.error(
                        "Experimental inbound event source failed with %s",
                        type(exc).__name__,
                    )
                    continue

                try:
                    listener = self._listener
                    if listener is None:
                        logger.warning(
                            "Dropping inbound Stream Dock event %s: no listener is attached",
                            event.event_name,
                        )
                    else:
                        listener.on_stream_dock_event(event)
                except Exception:
                    logger.exception(
                        "Failed to dispatch inbound Stream Dock event %s",
                        event.event_name,
                    )
                finally:
                    self._boundary.events.task_done()
            elif session_events_closed:
                # A conforming connector publishes Connected before accepting
                # text frames. If startup ended earlier, boundary shutdown owns
                # any undelivered event discard and will close this source.
                try:
                    self._boundary.events.receive(timeout=self._dispatcher_poll_interval)
                except TimeoutError:
                    continue
                except InboundEventQueueClosedError:
                    events_closed = True
                except Exception as exc:
                    logger.error(
                        "Experimental inbound event source failed with %s",
                        type(exc).__name__,
                    )
                else:
                    self._boundary.events.task_done()

    def _handle_session_event(self, event: SessionEvent) -> bool:
        if isinstance(event, Connected):
            listener = self._listener
            if listener is None:
                logger.warning("Stream Dock connected without a runtime listener")
                return True
            try:
                listener.on_stream_dock_connected()
            except Exception as exc:
                logger.exception("Failed to initialize the Stream Dock runtime after connection")
                with self._state_lock:
                    if self._dispatcher_error is None:
                        self._dispatcher_error = exc
                self.close()
                return False
            return True
        if isinstance(event, Disconnected):
            logger.info(
                "Stream Dock connection closed: %s %s",
                event.status_code,
                event.reason or "",
            )
            return False
        if isinstance(event, TransportError):
            logger.error("Stream Dock transport error (%s)", type(event.error).__name__)
            return False

        logger.warning("Ignoring unsupported session event %s", type(event).__name__)
        return False


def create_experimental_stream_dock_connection(
    port: int,
    *,
    queue_config: BoundaryQueueConfig | None = None,
    shutdown_config: BoundaryShutdownConfig | None = None,
    inbound_overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
    coalesce_dial_rotations: bool = False,
    coalesce_commands: bool = False,
    connector_factory: WebSocketConnectorFactory | None = None,
    dispatcher_poll_interval: float = _DEFAULT_DISPATCHER_POLL_INTERVAL,
    dispatcher_shutdown_timeout: float | None = _DEFAULT_DISPATCHER_SHUTDOWN_TIMEOUT,
) -> BoundaryStreamDockConnection:
    """Create the opt-in runtime connection backed by the experimental boundary."""

    if queue_config is None:
        queue_config = BoundaryQueueConfig(
            raw_inbound_limit=_DEFAULT_QUEUE_LIMIT,
            inbound_event_limit=_DEFAULT_QUEUE_LIMIT,
            outbound_command_limit=_DEFAULT_QUEUE_LIMIT,
            raw_outbound_limit=_DEFAULT_QUEUE_LIMIT,
            session_event_limit=_DEFAULT_SESSION_QUEUE_LIMIT,
        )
    try:
        boundary_overflow_policy = BoundaryInboundOverflowPolicy(inbound_overflow_policy.value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("inbound_overflow_policy must be an InboundOverflowPolicy") from None

    boundary = create_stream_dock_boundary(
        port,
        queue_config,
        shutdown_config=shutdown_config,
        connector_factory=connector_factory,
        inbound_overflow_policy=boundary_overflow_policy,
        coalesce_dial_rotations=coalesce_dial_rotations,
        coalesce_commands=coalesce_commands,
    )
    return BoundaryStreamDockConnection(
        boundary,
        dispatcher_poll_interval=dispatcher_poll_interval,
        dispatcher_shutdown_timeout=dispatcher_shutdown_timeout,
    )


def _validate_timeout(
    value: float | None,
    *,
    name: str,
    allow_none: bool,
) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} must be a non-negative finite number")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        suffix = " or None" if allow_none else ""
        raise ValueError(f"{name} must be a non-negative finite number{suffix}")
    return float(value)


__all__ = [
    "BoundaryQueueConfig",
    "BoundaryShutdownConfig",
    "BoundaryStreamDockConnection",
    "ExperimentalBoundaryRuntimeError",
    "create_experimental_stream_dock_connection",
]

"""Composition root and typed facade for the internal boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Lock, Thread, current_thread

from ..lifecycle import ShutdownState
from ..messaging.inbound import InboundEventQueue, InboundOverflowPolicy
from ..messaging.outbound import OutboundCommandQueue, WriterReadyOutboundCommandSink
from ..messaging.ports import (
    CommandWriterWorker,
    EventReaderWorker,
    InboundEventQueueControl,
    InboundEventSink,
    InboundEventSource,
    OutboundCommandQueueControl,
    OutboundCommandSink,
    OutboundCommandSource,
)
from ..messaging.reader import EventReader
from ..messaging.writer import CommandWriter
from ..protocol.adapters.event_parser import EventParserAdapter
from ..protocol.decoder import JsonStreamDockEventDecoder
from ..protocol.encoder import JsonStreamDockCommandEncoder
from ..protocol.ports import StreamDockCommandEncoder, StreamDockEventDecoder
from ..transport.ports import (
    RawInboundSource,
    RawOutboundSink,
    SessionEventSource,
    TransportQueueControl,
    WebSocketConnector,
)
from ..transport.queues import RawInboundQueue, RawOutboundQueue, SessionEventQueue
from ..transport.websocket import WebSocketClientConnector
from .config import BoundaryQueueConfig, BoundaryShutdownConfig
from .metrics import StreamDockBoundaryMetrics
from .ports import StreamDockBoundary, WebSocketConnectorFactory

logger = logging.getLogger(__name__)

EventReaderFactory = Callable[
    [RawInboundSource, StreamDockEventDecoder, InboundEventSink],
    EventReaderWorker,
]
CommandWriterFactory = Callable[
    [OutboundCommandSource, StreamDockCommandEncoder, RawOutboundSink],
    CommandWriterWorker,
]


class StreamDockBoundaryLifecycleError(RuntimeError):
    """Report an invalid lifecycle transition of a composed boundary."""


class ComposedStreamDockBoundary(StreamDockBoundary):
    """Own and coordinate every component behind the typed boundary ports."""

    def __init__(
        self,
        *,
        events: InboundEventSource,
        commands: WriterReadyOutboundCommandSink,
        session_events: SessionEventSource,
        connector: WebSocketConnector,
        event_reader: EventReaderWorker,
        command_writer: CommandWriterWorker,
        raw_inbound_queue: TransportQueueControl,
        inbound_event_queue: InboundEventQueueControl,
        outbound_command_queue: OutboundCommandQueueControl,
        raw_outbound_queue: TransportQueueControl,
        session_event_queue: TransportQueueControl,
        shutdown_config: BoundaryShutdownConfig | None = None,
    ) -> None:
        if shutdown_config is not None and not isinstance(shutdown_config, BoundaryShutdownConfig):
            raise TypeError("shutdown_config must be BoundaryShutdownConfig or None")

        self._events = events
        self._commands = commands
        self._session_events = session_events
        self._connector = connector
        self._event_reader = event_reader
        self._command_writer = command_writer
        self._raw_inbound_queue = raw_inbound_queue
        self._inbound_event_queue = inbound_event_queue
        self._outbound_command_queue = outbound_command_queue
        self._raw_outbound_queue = raw_outbound_queue
        self._session_event_queue = session_event_queue
        self._shutdown_config = shutdown_config or BoundaryShutdownConfig()
        self._shutdown = ShutdownState()
        self._failure: Exception | None = None
        self._on_fatal_error: Callable[[Exception], None] | None = None
        for worker in (event_reader, command_writer):
            if isinstance(worker, (EventReader, CommandWriter)):
                worker._on_fatal_error = self._on_worker_failure

        self._state_lock = Lock()
        self._close_lock = Lock()
        self._close_completed = Event()
        self._workers_stopped = Event()
        self._connector_run_finished = Event()
        self._run_started = False
        self._reader_started = False
        self._writer_started = False
        self._closing = False
        self._closed = False
        self._close_owner: Thread | None = None
        self._lifecycle_thread: Thread | None = None

    @property
    def events(self) -> InboundEventSource:
        """Return the typed inbound event source."""

        return self._events

    def _set_fatal_error_callback(self, callback: Callable[[Exception], None]) -> None:
        """Bind worker supervision before starting boundary consumers."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._state_lock:
            if self._run_started:
                raise StreamDockBoundaryLifecycleError("Boundary has already been started")
            self._on_fatal_error = callback

    @property
    def commands(self) -> OutboundCommandSink:
        """Return the typed outbound command sink."""

        return self._commands

    @property
    def session_events(self) -> SessionEventSource:
        """Return the typed transport lifecycle source."""

        return self._session_events

    def run_forever(self) -> None:
        """Start consumers before transport and run the connector exactly once."""

        lifecycle_started = False
        try:
            with self._state_lock:
                if self._run_started:
                    raise StreamDockBoundaryLifecycleError("Boundary can only be run once")
                if self._closing or self._closed:
                    raise StreamDockBoundaryLifecycleError("Boundary has already been closed")

                self._run_started = True
                self._lifecycle_thread = current_thread()
                lifecycle_started = True
                self._commands.begin_starting()
                self._shutdown.worker("Event reader", False)
                self._event_reader.start()
                self._reader_started = True
                self._shutdown.worker("Command writer", False)
                self._command_writer.start()
                self._writer_started = True
                self._commands.mark_ready()

            self._shutdown.worker("WebSocket connector", False)
            self._connector.run_forever()
        finally:
            if lifecycle_started:
                self._shutdown.worker("WebSocket connector", True)
                self._connector_run_finished.set()
                try:
                    self.close()
                finally:
                    with self._state_lock:
                        self._lifecycle_thread = None
        if self._failure is not None:
            raise self._failure

    def _on_worker_failure(self, error: Exception) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = error
                self._shutdown.primary_failure = error
        self._shutdown.begin()
        if self._on_fatal_error is not None:
            self._on_fatal_error(error)
        else:
            Thread(target=self.close, name="mirabox-boundary-fatal-close", daemon=True).start()

    def close(self) -> None:
        """Idempotently drain inbound, then outbound, then session events."""

        self._shutdown.begin()
        with self._close_lock:
            if self._close_completed.is_set():
                return
            if self._closing:
                owns_close = False
            else:
                self._closing = True
                self._close_owner = current_thread()
                owns_close = True

        if not owns_close:
            with self._state_lock:
                called_from_lifecycle = current_thread() is self._lifecycle_thread
                called_from_owner = current_thread() is self._close_owner
            if not called_from_lifecycle and not called_from_owner:
                self._close_completed.wait(self._shutdown.remaining())
            return

        self._shutdown.pending("Boundary cleanup", True)
        thread = Thread(
            target=self._finish_close, name="mirabox-internal-boundary-close", daemon=True
        )
        try:
            thread.start()
        except Exception as exc:
            self._shutdown.failed("Boundary close worker start", exc)
            self._close_completed.set()
        if not self._close_completed.wait(self._shutdown.remaining()):
            self._shutdown.timed_out("Boundary cleanup")

    def _finish_close(self) -> None:
        try:
            self._close_owned()
        except BaseException as exc:
            self._shutdown.failed("Boundary cleanup", exc)
        finally:
            with self._state_lock:
                self._closed = True
                self._close_owner = None
            self._commands.mark_closed()
            self._shutdown.pending("Boundary cleanup", False)
            self._close_completed.set()

    def metrics(self) -> StreamDockBoundaryMetrics:
        """Return one aggregate snapshot without exposing raw queue capabilities."""

        return StreamDockBoundaryMetrics(
            raw_inbound=self._raw_inbound_queue.metrics(),
            event_reader=self._event_reader.metrics(),
            inbound_events=self._inbound_event_queue.metrics(),
            outbound_commands=self._outbound_command_queue.metrics(),
            command_writer=self._command_writer.metrics(),
            raw_outbound=self._raw_outbound_queue.metrics(),
            connector=self._connector.metrics(),
            session_events=self._session_event_queue.metrics(),
        )

    def _close_owned(self) -> None:
        config = self._shutdown_config

        self._safe_stop_accepting(self._raw_inbound_queue, "Raw inbound queue")
        self._safe_drain(
            self._raw_inbound_queue,
            "Raw inbound queue",
            config.raw_inbound_drain_timeout,
        )
        if self._reader_started:
            self._safe_drain(
                self._event_reader,
                "Event reader",
                config.raw_inbound_drain_timeout,
            )
        self._safe_stop(
            self._event_reader,
            "Event reader",
            config.worker_stop_timeout,
        )
        self._safe_shutdown(self._raw_inbound_queue, "Raw inbound queue", 0)

        self._safe_stop_accepting(self._inbound_event_queue, "Inbound event queue")
        self._safe_shutdown(
            self._inbound_event_queue,
            "Inbound event queue",
            config.inbound_event_drain_timeout,
        )

        self._commands.begin_stopping()
        self._safe_stop_accepting(self._outbound_command_queue, "Outbound command queue")
        self._safe_drain(
            self._outbound_command_queue,
            "Outbound command queue",
            config.outbound_command_drain_timeout,
        )
        if self._writer_started:
            self._safe_drain(
                self._command_writer,
                "Command writer",
                config.outbound_command_drain_timeout,
            )
        self._safe_stop(
            self._command_writer,
            "Command writer",
            config.worker_stop_timeout,
        )
        self._safe_shutdown(self._outbound_command_queue, "Outbound command queue", 0)

        self._safe_stop_accepting(self._raw_outbound_queue, "Raw outbound queue")
        self._safe_drain(
            self._raw_outbound_queue,
            "Raw outbound queue",
            config.raw_outbound_drain_timeout,
        )
        self._shutdown.worker("WebSocket sender", False)
        self._safe_call(self._connector.close, "WebSocket connector close")
        if not self._wait_for_connector_run(config.connector_stop_timeout):
            self._safe_stop_accepting(self._session_event_queue, "Session event queue")
            self._wait_for_connector_run(config.worker_stop_timeout)
        self._safe_shutdown(self._raw_outbound_queue, "Raw outbound queue", 0)

        self._safe_stop_accepting(self._session_event_queue, "Session event queue")
        self._safe_shutdown(
            self._session_event_queue,
            "Session event queue",
            config.session_event_drain_timeout,
        )

        workers_stopped = True
        for component, name in (
            (self._event_reader, "Event reader"),
            (self._command_writer, "Command writer"),
        ):
            stopped = component.stop(timeout=None)
            self._shutdown.worker(name, stopped)
            workers_stopped = workers_stopped and stopped
        sender = getattr(self._connector, "_sender_thread", None)
        if sender is not None:
            sender.join()
        self._shutdown.worker("WebSocket sender", True)
        if self._run_started:
            self._connector_run_finished.wait()
        if workers_stopped:
            self._workers_stopped.set()

    def _wait_for_connector_run(self, timeout: float | None) -> bool:
        with self._state_lock:
            run_started = self._run_started
            called_from_lifecycle = current_thread() is self._lifecycle_thread
        if not run_started or called_from_lifecycle:
            return True
        if not self._connector_run_finished.wait(self._shutdown.remaining(timeout)):
            self._shutdown.timed_out("WebSocket connector stop")
            self._shutdown.worker("WebSocket connector", False)
            logger.warning("WebSocket connector did not stop before shutdown timeout")
            return False
        self._shutdown.worker("WebSocket connector", True)
        return True

    def _safe_stop_accepting(self, component: object, name: str) -> None:
        self._safe_call(
            component.stop_accepting,  # type: ignore[attr-defined]
            f"{name} stop_accepting",
        )

    def _safe_drain(self, component: object, name: str, timeout: float | None) -> None:
        try:
            drained = component.drain(timeout=self._shutdown.remaining(timeout))  # type: ignore[attr-defined]
        except Exception as exc:
            self._shutdown.failed(f"{name} drain", exc)
            logger.error("%s drain failed with %s", name, type(exc).__name__)
            return
        if not drained:
            self._shutdown.timed_out(f"{name} drain")
            logger.warning("%s did not drain before shutdown timeout", name)

    def _safe_stop(self, component: object, name: str, timeout: float | None) -> None:
        try:
            stopped = component.stop(timeout=self._shutdown.remaining(timeout))  # type: ignore[attr-defined]
        except Exception as exc:
            self._shutdown.failed(f"{name} stop", exc)
            logger.error("%s stop failed with %s", name, type(exc).__name__)
            return
        self._shutdown.worker(name, stopped)
        if not stopped:
            self._shutdown.timed_out(f"{name} stop")
            logger.warning("%s did not stop before shutdown timeout", name)

    def _safe_shutdown(self, component: object, name: str, timeout: float | None) -> None:
        try:
            drained = component.shutdown(timeout=self._shutdown.remaining(timeout))  # type: ignore[attr-defined]
        except Exception as exc:
            self._shutdown.failed(f"{name} shutdown", exc)
            logger.error("%s shutdown failed with %s", name, type(exc).__name__)
            return
        if not drained:
            self._shutdown.timed_out(f"{name} drain")
            logger.warning("%s did not drain before shutdown timeout", name)

    def _safe_call(self, action: Callable[[], object], name: str) -> None:
        self._shutdown.pending(name, True)
        try:
            action()
        except Exception as exc:
            self._shutdown.failed(name, exc)
            logger.error("%s failed with %s", name, type(exc).__name__)
        finally:
            self._shutdown.pending(name, False)


def create_stream_dock_boundary(
    port: int,
    queue_config: BoundaryQueueConfig,
    *,
    shutdown_config: BoundaryShutdownConfig | None = None,
    shutdown_state: ShutdownState | None = None,
    decoder: StreamDockEventDecoder | None = None,
    encoder: StreamDockCommandEncoder | None = None,
    connector_factory: WebSocketConnectorFactory | None = None,
    event_reader_factory: EventReaderFactory = EventReader,
    command_writer_factory: CommandWriterFactory = CommandWriter,
    inbound_overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
    coalesce_dial_rotations: bool = False,
    coalesce_commands: bool = False,
) -> StreamDockBoundary:
    """Build an unstarted boundary with replaceable protocol and transport edges."""

    if not isinstance(queue_config, BoundaryQueueConfig):
        raise TypeError("queue_config must be BoundaryQueueConfig")
    resolved_shutdown_config = shutdown_config or BoundaryShutdownConfig()
    if not isinstance(resolved_shutdown_config, BoundaryShutdownConfig):
        raise TypeError("shutdown_config must be BoundaryShutdownConfig or None")

    shared_shutdown = shutdown_state or ShutdownState()
    raw_inbound = RawInboundQueue(queue_config.raw_inbound_limit)
    inbound_events = InboundEventQueue(
        queue_config.inbound_event_limit,
        overflow_policy=inbound_overflow_policy,
        coalesce_dial_rotations=coalesce_dial_rotations,
    )
    outbound_commands = OutboundCommandQueue(
        queue_config.outbound_command_limit,
        coalesce_commands=coalesce_commands,
    )
    raw_outbound = RawOutboundQueue(queue_config.raw_outbound_limit)
    session_events = SessionEventQueue(queue_config.session_event_limit)

    resolved_decoder = (
        decoder if decoder is not None else JsonStreamDockEventDecoder(EventParserAdapter())
    )
    resolved_encoder = encoder if encoder is not None else JsonStreamDockCommandEncoder()
    event_reader = event_reader_factory(raw_inbound, resolved_decoder, inbound_events)
    command_writer = command_writer_factory(
        outbound_commands,
        resolved_encoder,
        raw_outbound,
    )

    if connector_factory is None:
        connector: WebSocketConnector = WebSocketClientConnector(
            port,
            raw_inbound,
            raw_outbound,
            session_events,
            outbound_shutdown_timeout=resolved_shutdown_config.raw_outbound_drain_timeout,
            shutdown_remaining=shared_shutdown.remaining,
        )
    else:
        connector = connector_factory(raw_inbound, raw_outbound, session_events)

    if isinstance(connector, WebSocketClientConnector):
        connector._shutdown_remaining = shared_shutdown.remaining
    commands = WriterReadyOutboundCommandSink(outbound_commands)
    boundary = ComposedStreamDockBoundary(
        events=inbound_events,
        commands=commands,
        session_events=session_events,
        connector=connector,
        event_reader=event_reader,
        command_writer=command_writer,
        raw_inbound_queue=raw_inbound,
        inbound_event_queue=inbound_events,
        outbound_command_queue=outbound_commands,
        raw_outbound_queue=raw_outbound,
        session_event_queue=session_events,
        shutdown_config=resolved_shutdown_config,
    )
    boundary._shutdown = shared_shutdown
    return boundary

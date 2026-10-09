"""Supported in-memory helpers for action and application integration tests.

The harness runs the production codecs, queues, session and dispatcher. Only
WebSocket I/O is replaced. No worker starts until ``start()`` or context entry.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from math import isfinite
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic, sleep
from types import TracebackType
from typing import TypeVar

from ._internal.protocol.encoder import JsonStreamDockCommandEncoder
from ._internal.transport.metrics import WebSocketConnectorMetrics
from ._internal.transport.ports import RawInboundSink, RawOutboundSource, SessionEventSink
from ._internal.transport.queues import TransportQueueClosedError
from ._internal.transport.session import Connected, Disconnected
from .commands import StreamDockCommand
from .completion import CommandFuture
from .diagnostics import SdkDiagnostic
from .json_types import JsonObject, JsonValue, clone_json_object
from .protocols import StreamDockActionDependencies, StreamDockSender
from .registration import PluginLaunchArguments
from .runtime.application import StreamDockApplication, _create_stream_dock_application
from .runtime.config import RuntimeDispatcherConfig, StreamDockQueueConfig, StreamDockShutdownConfig
from .runtime.ports import (
    ApplicationContext,
    ApplicationService,
    ApplicationServiceFactory,
    DependencyAwareActionRegistry,
    InboundOverflowPolicy,
    Plugin,
)

DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a non-negative finite number")
    return monotonic() + timeout


class FakeStreamDockSender:
    """Record isolated wire messages and immediately complete typed commands.

    ``send_async()`` reports serialization errors through its ``CommandFuture``;
    ``send()`` raises them. This fake has no session or worker lifecycle.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._messages: list[JsonObject] = []
        self._encoder = JsonStreamDockCommandEncoder()

    @property
    def messages(self) -> tuple[JsonObject, ...]:
        """Return isolated snapshots of successfully serialized commands."""
        with self._lock:
            return tuple(clone_json_object(message) for message in self._messages)

    def send(self, command: StreamDockCommand) -> None:
        self.send_async(command).result()

    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        completion = CommandFuture()
        try:
            message = json.loads(self._encoder.encode(command))
            with self._lock:
                self._messages.append(message)
        except Exception as exc:
            completion._finish(error=exc)
        else:
            completion._finish()
        return completion


class StreamDockHarness:
    """Run one application without a socket, with bounded waits and cleanup.

    Construction accepts the normal registry, dependency, plugin and service
    factories. Context entry starts and connects the session; context exit stops
    it and propagates runtime failures. Use ``start(connect=False)`` to test
    events queued before readiness, then call ``connect()`` and ``wait_ready()``.
    ``wait_for_events()`` counts acknowledged events since startup, including
    unknown events. Malformed JSON is rejected by the real reader and does not
    contribute to that count.
    ``inbound_overflow_policy``, ``coalesce_dial_rotations`` and
    ``coalesce_commands`` use the production queue behavior and defaults.
    """

    def __init__(
        self,
        launch_arguments: PluginLaunchArguments,
        *,
        action_factory: DependencyAwareActionRegistry[DependenciesT],
        action_dependencies_factory: Callable[[ApplicationContext], DependenciesT] | None = None,
        legacy_action_dependencies_factory: Callable[[StreamDockSender], DependenciesT]
        | None = None,
        plugin: Plugin | None = None,
        plugin_factory: Callable[[ApplicationContext], Plugin] | None = None,
        services: Iterable[ApplicationService] = (),
        service_factories: Iterable[ApplicationServiceFactory] = (),
        queue_config: StreamDockQueueConfig | None = None,
        shutdown_config: StreamDockShutdownConfig | None = None,
        runtime_config: RuntimeDispatcherConfig | None = None,
        error_observer: Callable[[SdkDiagnostic], None] | None = None,
        inbound_overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
        coalesce_dial_rotations: bool = False,
        coalesce_commands: bool = False,
    ) -> None:
        self._arguments = launch_arguments
        self._context: ApplicationContext | None = None
        self._connector: _MemoryConnector | None = None
        self._failure: BaseException | None = None
        self._stop_failure: BaseException | None = None
        self._finished = Event()
        self._thread: Thread | None = None
        self._stop_thread: Thread | None = None
        self._application = _create_stream_dock_application(
            launch_arguments,
            action_factory=action_factory,
            action_dependencies_factory=action_dependencies_factory,
            legacy_action_dependencies_factory=legacy_action_dependencies_factory,
            plugin=plugin,
            plugin_factory=plugin_factory,
            services=services,
            service_factories=service_factories,
            queue_config=queue_config,
            shutdown_config=shutdown_config,
            runtime_config=runtime_config,
            error_observer=error_observer,
            inbound_overflow_policy=inbound_overflow_policy,
            coalesce_dial_rotations=coalesce_dial_rotations,
            coalesce_commands=coalesce_commands,
            connector_factory=self._build_connector,
            _context_callback=self._capture_context,
        )

    @property
    def application(self) -> StreamDockApplication:
        """Return the application under test, including settings and metrics."""
        return self._application

    @property
    def context(self) -> ApplicationContext:
        """Return the same context supplied to application factories."""
        assert self._context is not None
        return self._context

    @property
    def frames(self) -> tuple[str, ...]:
        """Return every emitted JSON frame in transport order."""
        assert self._connector is not None
        with self._connector.lock:
            return tuple(self._connector.frames)

    @property
    def messages(self) -> tuple[JsonObject, ...]:
        """Return isolated decoded snapshots, including registration."""
        return tuple(json.loads(frame) for frame in self.frames)

    def start(self, *, connect: bool = True, timeout: float = 2.0) -> None:
        """Start once and optionally wait for mandatory session initialization."""
        deadline = _deadline(timeout)
        if self._thread is not None or self._stop_thread is not None:
            raise RuntimeError("Stream Dock harness can only be started once before stop")
        self._thread = Thread(target=self._run, name="mirabox-harness", daemon=True)
        self._thread.start()
        try:
            assert self._connector is not None
            self._wait(self._connector.started.is_set, deadline, "transport startup")
            if connect:
                self.connect()
                self.wait_ready(timeout=max(0, deadline - monotonic()))
        except BaseException as exc:
            try:
                self.stop()
            except BaseException as cleanup_error:
                exc.add_note(f"Harness cleanup failed: {type(cleanup_error).__name__}")
            raise

    def connect(self) -> None:
        """Publish Connected once; registration uses the real session pipeline."""
        self._require_running()
        assert self._connector is not None
        self._connector.connect()

    def wait_ready(self, *, timeout: float = 2.0) -> None:
        """Wait for registration and the initial settings request, or raise."""
        self._require_running()
        deadline = _deadline(timeout)
        self._wait(
            lambda: self.context.session_readiness.ready or self.context.session_readiness.terminal,
            deadline,
            "session readiness",
        )
        if not self.context.session_readiness.ready:
            failure = self.context.session_readiness.failure
            if failure is not None:
                raise failure
            self._raise_failure()
            raise RuntimeError("Stream Dock session closed before readiness")

    def send_json(self, message: JsonObject | str) -> None:
        """Inject a JSON object or raw text through the production reader."""
        self._require_running()
        if not isinstance(message, (dict, str)):
            raise TypeError("message must be a JSON object or text frame")
        frame = message if isinstance(message, str) else json.dumps(clone_json_object(message))
        assert self._connector is not None
        self._connector.submit(frame)

    def send_event(self, event: str, **fields: JsonValue) -> None:
        """Inject an event name with wire fields such as action/context/payload."""
        if not isinstance(event, str) or not event:
            raise ValueError("event must be a non-empty string")
        self.send_json({"event": event, **fields})

    def receive(self, *, timeout: float = 2.0) -> JsonObject:
        """Consume the next emitted wire message, raising on timeout or failure."""
        deadline = _deadline(timeout)
        assert self._connector is not None
        outbound = self._connector.outbound
        while True:
            try:
                frame = outbound.get_nowait()
            except Empty:
                self._wait(lambda: not outbound.empty(), deadline, "outbound message")
            else:
                return json.loads(frame)

    def wait_for_events(self, count: int, *, timeout: float = 2.0) -> None:
        """Wait for at least count acknowledged events since startup."""
        if type(count) is not int or count < 0:
            raise ValueError("count must be a non-negative integer")
        self._require_running()
        self._wait(
            lambda: self.application.metrics().event_pump.events_acknowledged >= count,
            _deadline(timeout),
            f"{count} acknowledged events",
        )

    def assert_registration(self) -> None:
        """Assert that the first emitted message registers the launch UUID."""
        expected = {
            "event": self._arguments.register_event,
            "uuid": self._arguments.plugin_uuid,
        }
        if not self.messages or self.messages[0] != expected:
            raise AssertionError(f"Expected registration {expected!r}; received {self.messages!r}")

    def assert_command(
        self,
        event: str,
        *,
        action: str | None = None,
        context: str | None = None,
        payload: JsonObject | None = None,
    ) -> None:
        """Assert a command matches the supplied action UUID, context and payload."""
        for message in self.messages:
            if message.get("event") != event:
                continue
            if action is not None and message.get("action") != action:
                continue
            if context is not None and message.get("context") != context:
                continue
            if payload is not None and message.get("payload") != payload:
                continue
            return
        raise AssertionError(f"No matching {event!r} command; received {self.messages!r}")

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop idempotently, join owned threads, and propagate runtime failures."""
        deadline = _deadline(timeout)
        if self._stop_thread is None:
            self._stop_thread = Thread(target=self._stop, name="mirabox-harness-stop", daemon=True)
            self._stop_thread.start()
        self._stop_thread.join(max(0, deadline - monotonic()))
        if self._thread is not None:
            self._thread.join(max(0, deadline - monotonic()))
        if self._stop_thread.is_alive() or (self._thread is not None and self._thread.is_alive()):
            raise TimeoutError("Stream Dock harness did not stop before the timeout")
        self._raise_failure()
        if self._stop_failure is not None:
            raise self._stop_failure

    def __enter__(self) -> StreamDockHarness:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.stop()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"Harness cleanup failed: {type(cleanup_error).__name__}")

    def _run(self) -> None:
        try:
            self.application.run()
        except BaseException as exc:
            self._failure = exc
        finally:
            self._finished.set()

    def _stop(self) -> None:
        try:
            self.application.stop()
        except BaseException as exc:
            self._stop_failure = exc

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _require_running(self) -> None:
        self._raise_failure()
        if self._thread is None or self._finished.is_set() or self._stop_thread is not None:
            raise RuntimeError("Stream Dock harness is not running")

    def _wait(self, predicate: Callable[[], bool], deadline: float, description: str) -> None:
        while True:
            self._raise_failure()
            if predicate():
                return
            if self._finished.is_set():
                raise RuntimeError(
                    f"Stream Dock application stopped while waiting for {description}"
                )
            if monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for {description}")
            sleep(0.001)

    def _capture_context(self, context: ApplicationContext) -> None:
        self._context = context

    def _build_connector(
        self,
        raw_inbound_sink: RawInboundSink,
        raw_outbound_source: RawOutboundSource,
        session_event_sink: SessionEventSink,
    ) -> _MemoryConnector:
        self._connector = _MemoryConnector(
            raw_inbound_sink, raw_outbound_source, session_event_sink
        )
        return self._connector


class _MemoryConnector:
    def __init__(
        self, inbound: RawInboundSink, outbound: RawOutboundSource, session: SessionEventSink
    ) -> None:
        self._inbound = inbound
        self._outbound = outbound
        self._session = session
        self._stop = Event()
        self.started = Event()
        self.lock = Lock()
        self.frames: list[str] = []
        self.outbound: Queue[str] = Queue()
        self._connected = False
        self._received = 0
        self._forwarded = 0
        self._disconnected = False

    def run_forever(self) -> None:
        self.started.set()
        try:
            while not self._stop.is_set():
                try:
                    frame = self._outbound.receive(timeout=0.01)
                except TimeoutError:
                    continue
                except TransportQueueClosedError:
                    self._stop.wait(0.01)
                    continue
                with self.lock:
                    self.frames.append(frame.payload)
                    self.outbound.put(frame.payload)
                    frame.receipt._finish()
        finally:
            with self.lock:
                self._disconnected = True
            self._session.submit(Disconnected(status_code=1000, reason="harness closed"), timeout=0)

    def connect(self) -> None:
        with self.lock:
            if self._connected:
                raise RuntimeError("Stream Dock harness session is already connected")
            if not self._session.submit(Connected(), timeout=0):
                raise RuntimeError("Stream Dock harness session queue rejected Connected")
            self._connected = True

    def submit(self, frame: str) -> None:
        with self.lock:
            self._received += 1
            if not self._inbound.submit(frame, timeout=0):
                raise RuntimeError("Stream Dock harness inbound queue rejected the frame")
            self._forwarded += 1

    def close(self) -> None:
        self._stop.set()

    def metrics(self) -> WebSocketConnectorMetrics:
        with self.lock:
            return WebSocketConnectorMetrics(
                connect_count=int(self._connected),
                disconnect_count=int(self._disconnected),
                last_close_code=1000 if self._disconnected else None,
                transport_error_count=0,
                session_events_rejected=0,
                inbound_frames_received=self._received,
                inbound_frames_forwarded=self._forwarded,
                inbound_frames_rejected=self._received - self._forwarded,
                binary_frames_rejected=0,
                outbound_frames_received=len(self.frames),
                outbound_frames_sent=len(self.frames),
                outbound_send_failures=0,
                outbound_drain_timeouts=0,
                outbound_discarded_during_shutdown=0,
            )


__all__ = ["FakeStreamDockSender", "StreamDockHarness"]

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from threading import Event, Lock, Thread, current_thread
from threading import enumerate as enumerate_threads
from time import monotonic, sleep

from mirabox_sdk import (
    ActionRegistry,
    ApplicationContext,
    KeyDownEvent,
    LogMessageCommand,
    OutboundCommandBusClosedError,
    OutboundCommandBusNotReadyError,
    RuntimeDispatcherConfig,
    StreamDockCommand,
    StreamDockEvent,
    UnknownStreamDockEvent,
)
from mirabox_sdk._internal.boundary.composition import (
    StreamDockBoundaryLifecycleError,
    create_stream_dock_boundary,
)
from mirabox_sdk._internal.boundary.config import (
    BoundaryQueueConfig,
    BoundaryShutdownConfig,
)
from mirabox_sdk._internal.boundary.ports import StreamDockBoundary
from mirabox_sdk._internal.messaging.reader import EventReader
from mirabox_sdk._internal.messaging.writer import CommandWriter
from mirabox_sdk._internal.protocol.ports import (
    StreamDockCommandEncoder,
    StreamDockEventDecoder,
)
from mirabox_sdk._internal.runtime.composition import create_stream_dock_runtime
from mirabox_sdk._internal.transport.frames import OutboundFrame
from mirabox_sdk._internal.transport.metrics import WebSocketConnectorMetrics
from mirabox_sdk._internal.transport.ports import (
    RawInboundSink,
    RawOutboundSource,
    SessionEventSink,
    WebSocketConnector,
)
from mirabox_sdk._internal.transport.queues import (
    TransportMessageTooLargeError,
    TransportQueueClosedError,
)
from mirabox_sdk._internal.transport.session import Connected, Disconnected
from mirabox_sdk._internal.transport.websocket import WebSocketClientConnector
from mirabox_sdk.runtime.application import _create_stream_dock_application
from tests.internal.runtime.fakes import RecordingActionFactory
from tests.internal.runtime.test_composition import _launch_arguments
from tests.internal.test_websocket_connector import _FakeWebSocketFactory

from .wire_fixtures import known_event_envelopes


def _queue_config(limit: int = 8) -> BoundaryQueueConfig:
    return BoundaryQueueConfig(
        raw_inbound_limit=limit,
        inbound_event_limit=limit,
        outbound_command_limit=limit,
        raw_outbound_limit=limit,
        session_event_limit=limit,
    )


def _shutdown_config(timeout: float = 0.2) -> BoundaryShutdownConfig:
    return BoundaryShutdownConfig(
        raw_inbound_drain_timeout=timeout,
        inbound_event_drain_timeout=timeout,
        outbound_command_drain_timeout=timeout,
        raw_outbound_drain_timeout=timeout,
        session_event_drain_timeout=timeout,
        worker_stop_timeout=timeout,
        connector_stop_timeout=timeout,
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition was not reached before test timeout")
        sleep(0.005)


class _FakeConnector(WebSocketConnector):
    def __init__(
        self,
        raw_inbound: RawInboundSink,
        raw_outbound: RawOutboundSource,
        session_events: SessionEventSink,
        *,
        consume_outbound: bool,
        startup_error: Exception | None,
        startup_gate: Event | None = None,
    ) -> None:
        self._raw_inbound = raw_inbound
        self._raw_outbound = raw_outbound
        self._session_events = session_events
        self._consume_outbound = consume_outbound
        self._startup_error = startup_error
        self._startup_gate = startup_gate
        self._stop_requested = Event()
        self.started = Event()
        self._lock = Lock()
        self.close_calls = 0
        self.sent: list[tuple[str, str]] = []
        self._connect_count = 0
        self._disconnect_count = 0
        self._outbound_received = 0
        self._outbound_sent = 0

    def run_forever(self) -> None:
        self.started.set()
        if self._startup_gate is not None and not self._startup_gate.wait(1):
            raise TimeoutError("fake connector startup gate was not released")
        if self._startup_error is not None:
            raise self._startup_error

        if self._session_events.submit(Connected(), timeout=0):
            with self._lock:
                self._connect_count += 1

        try:
            while not self._stop_requested.is_set():
                if not self._consume_outbound:
                    self._stop_requested.wait(0.01)
                    continue
                try:
                    frame = self._raw_outbound.receive(timeout=0.01)
                except TimeoutError:
                    continue
                except TransportQueueClosedError:
                    self._stop_requested.wait(0.01)
                    continue
                self._send(frame)
        finally:
            if self._session_events.submit(
                Disconnected(status_code=1000, reason="fake closed"),
                timeout=0,
            ):
                with self._lock:
                    self._disconnect_count += 1

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
        self._stop_requested.set()

    def disconnect(self) -> None:
        self._stop_requested.set()

    def emit(self, frame: str) -> bool:
        return self._raw_inbound.submit(frame, timeout=0)

    def metrics(self) -> WebSocketConnectorMetrics:
        with self._lock:
            return WebSocketConnectorMetrics(
                connect_count=self._connect_count,
                disconnect_count=self._disconnect_count,
                last_close_code=1000 if self._disconnect_count else None,
                transport_error_count=0,
                session_events_rejected=0,
                inbound_frames_received=0,
                inbound_frames_forwarded=0,
                inbound_frames_rejected=0,
                binary_frames_rejected=0,
                outbound_frames_received=self._outbound_received,
                outbound_frames_sent=self._outbound_sent,
                outbound_send_failures=0,
                outbound_drain_timeouts=0,
                outbound_discarded_during_shutdown=0,
            )

    def _send(self, frame: OutboundFrame) -> None:
        with self._lock:
            self._outbound_received += 1
            self.sent.append((frame.payload, current_thread().name))
            self._outbound_sent += 1
        frame.receipt._finish()


class _FakeConnectorFactory:
    def __init__(
        self,
        *,
        consume_outbound: bool = True,
        startup_error: Exception | None = None,
        startup_gate: Event | None = None,
    ) -> None:
        self._consume_outbound = consume_outbound
        self._startup_error = startup_error
        self._startup_gate = startup_gate
        self.connector: _FakeConnector | None = None

    def __call__(
        self,
        raw_inbound_sink: RawInboundSink,
        raw_outbound_source: RawOutboundSource,
        session_event_sink: SessionEventSink,
    ) -> WebSocketConnector:
        self.connector = _FakeConnector(
            raw_inbound_sink,
            raw_outbound_source,
            session_event_sink,
            consume_outbound=self._consume_outbound,
            startup_error=self._startup_error,
            startup_gate=self._startup_gate,
        )
        return self.connector


class _FalseyDecoder:
    def __init__(self) -> None:
        self.frames: list[str] = []

    def __bool__(self) -> bool:
        return False

    def decode(self, frame: str) -> StreamDockEvent:
        self.frames.append(frame)
        return UnknownStreamDockEvent(event="injectedDecoder", data={"frame": frame})


class _FalseyEncoder:
    def __init__(self) -> None:
        self.commands: list[StreamDockCommand] = []

    def __bool__(self) -> bool:
        return False

    def encode(self, command: StreamDockCommand) -> str:
        self.commands.append(command)
        return "encoded by injected encoder"


class _BoundaryHarness:
    def __init__(
        self,
        *,
        queue_config: BoundaryQueueConfig | None = None,
        shutdown_config: BoundaryShutdownConfig | None = None,
        decoder: StreamDockEventDecoder | None = None,
        encoder: StreamDockCommandEncoder | None = None,
        consume_outbound: bool = True,
        startup_error: Exception | None = None,
        startup_gate: Event | None = None,
    ) -> None:
        self.factory = _FakeConnectorFactory(
            consume_outbound=consume_outbound,
            startup_error=startup_error,
            startup_gate=startup_gate,
        )
        self.boundary = create_stream_dock_boundary(
            12345,
            queue_config or _queue_config(),
            shutdown_config=shutdown_config or _shutdown_config(),
            decoder=decoder,
            encoder=encoder,
            connector_factory=self.factory,
        )
        assert self.factory.connector is not None
        self.connector = self.factory.connector
        self.errors: list[Exception] = []
        self.thread = Thread(target=self._run, name="test-boundary-lifecycle")

    def start(self) -> None:
        self.thread.start()
        if not self.connector.started.wait(1):
            raise AssertionError("fake connector did not start")

    def join(self) -> None:
        self.thread.join(1)
        if self.thread.is_alive():
            raise AssertionError("boundary lifecycle thread did not finish")

    def _run(self) -> None:
        try:
            self.boundary.run_forever()
        except Exception as exc:
            self.errors.append(exc)


class StreamDockBoundaryPipelineTests(unittest.TestCase):
    def test_byte_limits_are_wired_and_oversized_command_completes_with_failure(self) -> None:
        config = replace(
            _queue_config(),
            max_message_bytes=128,
            raw_inbound_byte_limit=512,
            inbound_event_byte_limit=1024,
            outbound_command_byte_limit=2048,
            raw_outbound_byte_limit=512,
        )
        harness = _BoundaryHarness(queue_config=config)
        harness.start()
        try:
            self.assertFalse(harness.connector._raw_inbound.submit("x" * 129))
            completion = harness.boundary.commands.send_async(LogMessageCommand("x" * 256))
            with self.assertRaises(TransportMessageTooLargeError):
                completion.result(timeout=1)
            succeeded = harness.boundary.commands.send_async(LogMessageCommand("valid"))
            succeeded.result(timeout=1)
            metrics = harness.boundary.metrics()
            self.assertEqual(metrics.raw_inbound.byte_limit, 512)
            self.assertEqual(metrics.inbound_events.byte_limit, 1024)
            self.assertEqual(metrics.outbound_commands.byte_limit, 2048)
            self.assertEqual(metrics.raw_outbound.byte_limit, 512)
            self.assertEqual(metrics.raw_inbound.rejected_oversized, 1)
            self.assertEqual(metrics.raw_outbound.rejected_oversized, 1)
            self.assertEqual(metrics.command_writer.completion_failures, 1)
        finally:
            harness.boundary.close()
            harness.join()

    def test_commands_fail_fast_before_writer_start_and_after_close(self) -> None:
        harness = _BoundaryHarness()

        for send in (
            lambda: harness.boundary.commands.send(LogMessageCommand("before run")),
            lambda: harness.boundary.commands.send_async(LogMessageCommand("before run")),
        ):
            with self.assertRaises(OutboundCommandBusNotReadyError):
                send()
        self.assertEqual(harness.boundary.metrics().outbound_commands.submitted, 0)

        harness.boundary.close()
        with self.assertRaises(OutboundCommandBusClosedError):
            harness.boundary.commands.send_async(LogMessageCommand("after close"))

    def test_uses_injected_decoder_and_encoder_even_when_they_are_falsey(self) -> None:
        decoder = _FalseyDecoder()
        encoder = _FalseyEncoder()
        harness = _BoundaryHarness(decoder=decoder, encoder=encoder)
        harness.start()

        try:
            self.assertTrue(harness.connector.emit("opaque frame"))
            event = harness.boundary.events.receive(timeout=1)
            self.assertEqual(event.event_name, "injectedDecoder")
            harness.boundary.events.task_done()

            command = LogMessageCommand("hello")
            completion = harness.boundary.commands.send_async(command)
            self.assertIsNone(completion.result(timeout=1))
            self.assertEqual(decoder.frames, ["opaque frame"])
            self.assertEqual(encoder.commands, [command])
            self.assertEqual(harness.connector.sent[0][0], "encoded by injected encoder")
        finally:
            harness.connector.disconnect()
            harness.join()
        self.assertEqual(harness.errors, [])

    def test_composes_full_inbound_outbound_and_session_pipelines(self) -> None:
        harness = _BoundaryHarness()
        harness.start()
        self.assertIsInstance(harness.boundary, StreamDockBoundary)
        self.assertIsInstance(harness.boundary.session_events.receive(timeout=1), Connected)

        frame = json.dumps(known_event_envelopes()["keyDown"])
        self.assertTrue(harness.connector.emit(frame))
        event = harness.boundary.events.receive(timeout=1)
        self.assertIsInstance(event, KeyDownEvent)
        self.assertEqual(event.context, "button")
        harness.boundary.events.task_done()

        completion = harness.boundary.commands.send_async(LogMessageCommand("hello"))
        self.assertIsNone(completion.result(timeout=1))
        self.assertEqual(
            json.loads(harness.connector.sent[0][0]),
            {"event": "logMessage", "payload": {"message": "hello"}},
        )
        self.assertEqual(
            harness.connector.sent[0][1],
            "test-boundary-lifecycle",
        )

        metrics = harness.boundary.metrics()
        self.assertEqual(metrics.raw_inbound.enqueued, 1)
        self.assertEqual(metrics.event_reader.decoded, 1)
        self.assertEqual(metrics.inbound_events.dequeued, 1)
        self.assertEqual(metrics.outbound_commands.enqueued, 1)
        self.assertEqual(metrics.command_writer.frames_enqueued, 1)
        self.assertEqual(metrics.connector.outbound_frames_sent, 1)
        with self.assertRaises(FrozenInstanceError):
            metrics.raw_inbound = metrics.raw_inbound  # type: ignore[misc]

        harness.connector.disconnect()
        self.assertIsInstance(
            harness.boundary.session_events.receive(timeout=1),
            Disconnected,
        )
        harness.join()
        self.assertEqual(harness.errors, [])
        self.assertEqual(harness.connector.close_calls, 1)

    def test_facade_exposes_only_typed_ports_and_boundary_operations(self) -> None:
        harness = _BoundaryHarness()
        boundary = harness.boundary

        public_names = {name for name in dir(boundary) if not name.startswith("_")}
        self.assertEqual(
            public_names,
            {"close", "commands", "events", "metrics", "run_forever", "session_events"},
        )
        self.assertIs(boundary.events, boundary.events)
        self.assertIs(boundary.commands, boundary.commands)
        self.assertIs(boundary.session_events, boundary.session_events)
        self.assertNotIn("raw_inbound", public_names)
        self.assertNotIn("raw_outbound", public_names)
        boundary.close()


class StreamDockBoundaryLifecycleTests(unittest.TestCase):
    def test_hung_sender_returns_incomplete_outcome_without_closing_service_resources(self) -> None:
        factory = _FakeWebSocketFactory(block_sends=True)
        service_stopped = Event()

        class Service:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                service_stopped.set()

        def connector_factory(raw_inbound: object, raw_outbound: object, session: object) -> object:
            return WebSocketClientConnector(
                12345,
                raw_inbound,
                raw_outbound,
                session,
                websocket_app_factory=factory,
            )

        application = _create_stream_dock_application(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
            services=(Service(),),
            connector_factory=connector_factory,
            runtime_config=RuntimeDispatcherConfig(shutdown_timeout=0.05),
        )
        socket = factory.app
        self.assertIsNotNone(socket)
        # Socket close ends the receive loop but cannot unblock the external send.
        socket.close = socket.loop_finished.set
        errors: list[BaseException] = []

        def run() -> None:
            try:
                application.run()
            except BaseException as exc:
                errors.append(exc)

        runner = Thread(target=run)
        self.addCleanup(socket.release_sends.set)
        runner.start()
        self.assertTrue(socket.send_started.wait(1))
        start = monotonic()
        application.stop()
        runner.join(0.3)
        self.assertLess(monotonic() - start, 0.4)
        self.assertFalse(runner.is_alive())
        self.assertFalse(application.shutdown_outcome.complete)
        self.assertFalse(application.shutdown_outcome.workers_stopped)
        self.assertFalse(service_stopped.is_set())
        socket.release_sends.set()
        self.assertTrue(service_stopped.wait(1))
        _wait_until(lambda: application.shutdown_outcome.complete)

    def test_reader_and_writer_source_exit_reaches_runtime_supervisor(self) -> None:
        for stage in ("reader", "writer"):
            for cause in (OSError("source failed"), SystemExit(3)):
                with self.subTest(stage=stage, cause=type(cause).__name__):
                    self._assert_source_failure(stage, cause)

    def _assert_source_failure(self, stage: str, cause: BaseException) -> None:
        class FailedSource:
            def receive(self, *, timeout: float | None = None) -> object:
                raise cause

        class FailedReader(EventReader):
            def __init__(self, source: object, decoder: object, sink: object) -> None:
                super().__init__(FailedSource(), decoder, sink)

        class FailedWriter(CommandWriter):
            def __init__(self, source: object, encoder: object, sink: object) -> None:
                super().__init__(FailedSource(), encoder, sink)

        boundary = create_stream_dock_boundary(
            12345,
            _queue_config(),
            connector_factory=_FakeConnectorFactory(),
            event_reader_factory=FailedReader if stage == "reader" else EventReader,
            command_writer_factory=FailedWriter if stage == "writer" else CommandWriter,
        )
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=RecordingActionFactory(boundary.commands),
            config=RuntimeDispatcherConfig(shutdown_timeout=0.2),
        )
        start = monotonic()
        with (
            self.assertLogs("mirabox_sdk", level="WARNING"),
            self.assertRaises(Exception) as raised,
        ):
            runtime.run_forever()
        self.assertLess(monotonic() - start, 0.8)
        observed = raised.exception
        self.assertIs(observed if isinstance(cause, Exception) else observed.__cause__, cause)
        self.assertIs(runtime.shutdown_outcome.primary_failure, observed)

    def test_command_after_writer_start_finishes_when_connector_startup_fails(self) -> None:
        startup_error = RuntimeError("fake connector startup failed")
        release_startup = Event()
        harness = _BoundaryHarness(
            shutdown_config=_shutdown_config(0),
            startup_error=startup_error,
            startup_gate=release_startup,
        )
        harness.start()

        completion = harness.boundary.commands.send_async(LogMessageCommand("queued"))
        release_startup.set()
        harness.join()

        self.assertEqual(harness.errors, [startup_error])
        self.assertTrue(completion.done())
        self.assertIsNotNone(completion.exception(timeout=0))

    def test_close_waits_for_in_flight_handler_before_closing_outbound(self) -> None:
        harness = _BoundaryHarness(shutdown_config=_shutdown_config(1))
        harness.start()
        self.assertIsInstance(harness.boundary.session_events.receive(timeout=1), Connected)

        handler_started = Event()
        release_handler = Event()
        handler_finished = Event()
        completions = []

        def handle_one_event() -> None:
            harness.boundary.events.receive(timeout=1)
            handler_started.set()
            try:
                if not release_handler.wait(1):
                    raise AssertionError("timed out waiting to release handler")
                completions.append(
                    harness.boundary.commands.send_async(LogMessageCommand("from handler"))
                )
            finally:
                harness.boundary.events.task_done()
                handler_finished.set()

        handler = Thread(target=handle_one_event, name="test-inbound-handler")
        handler.start()
        frame = json.dumps(known_event_envelopes()["keyDown"])
        self.assertTrue(harness.connector.emit(frame))
        self.assertTrue(handler_started.wait(1))

        closer = Thread(target=harness.boundary.close, name="test-boundary-close")
        closer.start()
        _wait_until(lambda: harness.boundary.metrics().inbound_events.in_flight == 1)
        self.assertTrue(closer.is_alive())

        release_handler.set()
        self.assertTrue(handler_finished.wait(1))
        handler.join(1)
        self.assertIsInstance(
            harness.boundary.session_events.receive(timeout=1),
            Disconnected,
        )
        closer.join(1)
        harness.join()

        self.assertFalse(handler.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(completions), 1)
        self.assertIsNone(completions[0].result(timeout=0))
        self.assertEqual(
            json.loads(harness.connector.sent[0][0]),
            {"event": "logMessage", "payload": {"message": "from handler"}},
        )
        self.assertEqual(harness.boundary.metrics().inbound_events.acknowledged, 1)

    def test_startup_failure_cleans_up_started_workers_and_closes_ports(self) -> None:
        startup_error = RuntimeError("fake connector startup failed")
        harness = _BoundaryHarness(
            shutdown_config=_shutdown_config(0),
            startup_error=startup_error,
        )

        harness.start()
        harness.join()

        self.assertEqual(harness.errors, [startup_error])
        self.assertEqual(harness.connector.close_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "no longer accepting commands"):
            harness.boundary.commands.send_async(LogMessageCommand("late"))
        with self.assertRaises(StreamDockBoundaryLifecycleError):
            harness.boundary.run_forever()
        self.assertFalse(
            any(
                thread.name
                in {
                    "mirabox-internal-event-reader",
                    "mirabox-internal-command-writer",
                }
                and thread.is_alive()
                for thread in enumerate_threads()
            )
        )

    def test_busy_queues_without_consumers_shutdown_without_silent_pending_work(
        self,
    ) -> None:
        queue_config = BoundaryQueueConfig(
            raw_inbound_limit=2,
            inbound_event_limit=2,
            outbound_command_limit=3,
            raw_outbound_limit=1,
            session_event_limit=2,
        )
        harness = _BoundaryHarness(
            queue_config=queue_config,
            shutdown_config=_shutdown_config(0),
            consume_outbound=False,
        )
        harness.start()

        frame = json.dumps(known_event_envelopes()["keyDown"])
        self.assertTrue(harness.connector.emit(frame))
        self.assertTrue(harness.connector.emit(frame))

        completions = [harness.boundary.commands.send_async(LogMessageCommand("0"))]
        _wait_until(lambda: harness.boundary.metrics().raw_outbound.current_depth == 1)
        completions.append(harness.boundary.commands.send_async(LogMessageCommand("1")))
        _wait_until(lambda: harness.boundary.metrics().command_writer.commands_received == 2)
        completions.append(harness.boundary.commands.send_async(LogMessageCommand("2")))

        harness.boundary.close()
        harness.join()

        for completion in completions:
            self.assertTrue(completion.done())
            self.assertIsNotNone(completion.exception(timeout=0))
        metrics = harness.boundary.metrics()
        self.assertGreater(
            metrics.inbound_events.discarded_during_shutdown + metrics.event_reader.rejected,
            0,
        )
        self.assertGreater(
            metrics.outbound_commands.discarded_during_shutdown
            + metrics.command_writer.discarded_during_shutdown
            + metrics.raw_outbound.discarded_during_shutdown,
            0,
        )
        self.assertGreater(metrics.session_events.discarded_during_shutdown, 0)
        self.assertEqual(harness.connector.close_calls, 1)

    def test_concurrent_close_is_idempotent(self) -> None:
        harness = _BoundaryHarness()
        harness.start()
        self.assertIsInstance(harness.boundary.session_events.receive(timeout=1), Connected)
        closers = [Thread(target=harness.boundary.close) for _ in range(6)]

        for closer in closers:
            closer.start()
        for closer in closers:
            closer.join(1)
            self.assertFalse(closer.is_alive())
        harness.join()

        self.assertEqual(harness.connector.close_calls, 1)
        harness.boundary.close()
        self.assertEqual(harness.connector.close_calls, 1)

    def test_rejects_invalid_shutdown_configuration(self) -> None:
        field_names = (
            "raw_inbound_drain_timeout",
            "inbound_event_drain_timeout",
            "outbound_command_drain_timeout",
            "raw_outbound_drain_timeout",
            "session_event_drain_timeout",
            "worker_stop_timeout",
            "connector_stop_timeout",
        )
        defaults = dict.fromkeys(field_names, 0.1)
        for field_name in field_names:
            for invalid in (-1, True, float("inf"), float("nan"), "1"):
                with (
                    self.subTest(field_name=field_name, invalid=invalid),
                    self.assertRaisesRegex(
                        ValueError,
                        f"^{field_name} must be a non-negative finite number or None$",
                    ),
                ):
                    BoundaryShutdownConfig(
                        **{**defaults, field_name: invalid}  # type: ignore[arg-type]
                    )


if __name__ == "__main__":
    unittest.main()

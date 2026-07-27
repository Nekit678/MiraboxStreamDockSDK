from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from threading import Event, Lock, Thread, current_thread
from threading import enumerate as enumerate_threads
from time import monotonic, sleep

from mirabox_sdk import KeyDownEvent, LogMessageCommand
from mirabox_sdk._next.boundary.composition import (
    StreamDockBoundaryLifecycleError,
    create_stream_dock_boundary,
)
from mirabox_sdk._next.boundary.config import (
    BoundaryQueueConfig,
    BoundaryShutdownConfig,
)
from mirabox_sdk._next.boundary.ports import StreamDockBoundary
from mirabox_sdk._next.transport.frames import OutboundFrame
from mirabox_sdk._next.transport.metrics import WebSocketConnectorMetrics
from mirabox_sdk._next.transport.ports import (
    RawInboundSink,
    RawOutboundSource,
    SessionEventSink,
    WebSocketConnector,
)
from mirabox_sdk._next.transport.queues import TransportQueueClosedError
from mirabox_sdk._next.transport.session import Connected, Disconnected

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
    ) -> None:
        self._raw_inbound = raw_inbound
        self._raw_outbound = raw_outbound
        self._session_events = session_events
        self._consume_outbound = consume_outbound
        self._startup_error = startup_error
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
    ) -> None:
        self._consume_outbound = consume_outbound
        self._startup_error = startup_error
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
        )
        return self.connector


class _BoundaryHarness:
    def __init__(
        self,
        *,
        queue_config: BoundaryQueueConfig | None = None,
        shutdown_config: BoundaryShutdownConfig | None = None,
        consume_outbound: bool = True,
        startup_error: Exception | None = None,
    ) -> None:
        self.factory = _FakeConnectorFactory(
            consume_outbound=consume_outbound,
            startup_error=startup_error,
        )
        self.boundary = create_stream_dock_boundary(
            12345,
            queue_config or _queue_config(),
            shutdown_config=shutdown_config or _shutdown_config(),
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
                    "mirabox-next-event-reader",
                    "mirabox-next-command-writer",
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

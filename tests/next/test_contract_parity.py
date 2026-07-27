"""End-to-end behavioral contracts shared by legacy and experimental boundaries."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic, sleep
from unittest.mock import patch

from mirabox_sdk import (
    DialRotateEvent,
    JsonObject,
    LogMessageCommand,
    SetTitleCommand,
    StreamDockCommand,
    StreamDockEvent,
    UnknownStreamDockEvent,
    WebSocketStreamDockConnection,
)
from mirabox_sdk import (
    InboundOverflowPolicy as LegacyInboundOverflowPolicy,
)
from mirabox_sdk._next.boundary.composition import create_stream_dock_boundary
from mirabox_sdk._next.boundary.config import BoundaryQueueConfig, BoundaryShutdownConfig
from mirabox_sdk._next.boundary.metrics import StreamDockBoundaryMetrics
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

from .wire_fixtures import known_command_wire_fixtures, known_event_envelopes


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition was not reached before test timeout")
        sleep(0.005)


def _queue_config(limit: int = 16) -> BoundaryQueueConfig:
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
        session_event_drain_timeout=0,
        worker_stop_timeout=timeout,
        connector_stop_timeout=timeout,
    )


class _LegacyWebSocket:
    """Record legacy writes without opening a real WebSocket connection."""

    def __init__(
        self,
        *,
        send_hook: Callable[[str], None] | None = None,
        close_hook: Callable[[], None] | None = None,
    ) -> None:
        self._send_hook = send_hook
        self._close_hook = close_hook
        self._lock = Lock()
        self.sent: list[str] = []
        self.close_calls = 0

    def send(self, payload: str) -> None:
        if self._send_hook is not None:
            self._send_hook(payload)
        with self._lock:
            self.sent.append(payload)

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
        if self._close_hook is not None:
            self._close_hook()


class _LegacyListener:
    def __init__(self, event_hook: Callable[[StreamDockEvent], None] | None = None) -> None:
        self._event_hook = event_hook
        self.events: Queue[StreamDockEvent] = Queue()

    def on_stream_dock_connected(self) -> None:
        pass

    def on_stream_dock_event(self, event: StreamDockEvent) -> None:
        if self._event_hook is not None:
            self._event_hook(event)
        self.events.put(event)


class _LegacyHarness:
    """Expose the legacy connection through the parity test operations."""

    def __init__(
        self,
        *,
        queue_limit: int = 16,
        outbound_shutdown_timeout: float | None = 0.2,
        send_hook: Callable[[str], None] | None = None,
        close_hook: Callable[[], None] | None = None,
        event_hook: Callable[[StreamDockEvent], None] | None = None,
        coalesce_dial_rotations: bool = False,
        coalesce_commands: bool = False,
    ) -> None:
        self.socket = _LegacyWebSocket(send_hook=send_hook, close_hook=close_hook)
        with patch(
            "mirabox_sdk.connection.websocket.WebSocketApp",
            return_value=self.socket,
        ):
            self.connection = WebSocketStreamDockConnection(
                12345,
                inbound_queue_limit=queue_limit,
                inbound_worker_count=1,
                overflow_policy=LegacyInboundOverflowPolicy.DROP_NEWEST,
                coalesce_dial_rotations=coalesce_dial_rotations,
                inbound_shutdown_timeout=outbound_shutdown_timeout,
                outbound_queue_limit=queue_limit,
                coalesce_outbound_commands=coalesce_commands,
                outbound_shutdown_timeout=outbound_shutdown_timeout,
            )
        self.listener = _LegacyListener(event_hook)
        self.connection.set_listener(self.listener)
        self.connection._inbound.start()
        self._closed = False

    def emit(self, frame: str) -> None:
        self.connection._on_message(self.socket, frame)

    def receive_event(self, timeout: float = 1) -> StreamDockEvent:
        try:
            return self.listener.events.get(timeout=timeout)
        except Empty as exc:
            raise TimeoutError("Timed out waiting for legacy event") from exc

    def send_async(self, command: StreamDockCommand):
        return self.connection.send_async(command)

    @property
    def sent(self) -> list[str]:
        return self.socket.sent

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.connection.close()


class _ParityConnector(WebSocketConnector):
    """Fake API-independent connector used to drive a composed boundary."""

    def __init__(
        self,
        raw_inbound: RawInboundSink,
        raw_outbound: RawOutboundSource,
        session_events: SessionEventSink,
        *,
        consume_outbound: bool,
        send_hook: Callable[[str], None] | None,
    ) -> None:
        self._raw_inbound = raw_inbound
        self._raw_outbound = raw_outbound
        self._session_events = session_events
        self._consume_outbound = consume_outbound
        self._send_hook = send_hook
        self._stop_requested = Event()
        self.started = Event()
        self._lock = Lock()
        self.close_calls = 0
        self.sent: list[str] = []
        self._connect_count = 0
        self._disconnect_count = 0
        self._outbound_received = 0
        self._outbound_sent = 0

    def run_forever(self) -> None:
        self.started.set()
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
                Disconnected(status_code=1000, reason="parity connector closed"),
                timeout=0,
            ):
                with self._lock:
                    self._disconnect_count += 1

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
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
        if self._send_hook is not None:
            self._send_hook(frame.payload)
        with self._lock:
            self.sent.append(frame.payload)
            self._outbound_sent += 1
        frame.receipt._finish()


class _ParityConnectorFactory:
    def __init__(
        self,
        *,
        consume_outbound: bool,
        send_hook: Callable[[str], None] | None,
    ) -> None:
        self._consume_outbound = consume_outbound
        self._send_hook = send_hook
        self.connector: _ParityConnector | None = None

    def __call__(
        self,
        raw_inbound_sink: RawInboundSink,
        raw_outbound_source: RawOutboundSource,
        session_event_sink: SessionEventSink,
    ) -> WebSocketConnector:
        self.connector = _ParityConnector(
            raw_inbound_sink,
            raw_outbound_source,
            session_event_sink,
            consume_outbound=self._consume_outbound,
            send_hook=self._send_hook,
        )
        return self.connector


class _NextHarness:
    """Run the `_next` boundary with a deterministic fake connector."""

    def __init__(
        self,
        *,
        queue_limit: int = 16,
        shutdown_timeout: float = 0.2,
        consume_outbound: bool = True,
        send_hook: Callable[[str], None] | None = None,
        coalesce_dial_rotations: bool = False,
        coalesce_commands: bool = False,
    ) -> None:
        self.factory = _ParityConnectorFactory(
            consume_outbound=consume_outbound,
            send_hook=send_hook,
        )
        self.boundary = create_stream_dock_boundary(
            12345,
            _queue_config(queue_limit),
            shutdown_config=_shutdown_config(shutdown_timeout),
            connector_factory=self.factory,
            coalesce_dial_rotations=coalesce_dial_rotations,
            coalesce_commands=coalesce_commands,
        )
        assert self.factory.connector is not None
        self.connector = self.factory.connector
        self.errors: list[Exception] = []
        self._closed = False
        self._thread = Thread(target=self._run, name="test-contract-parity-boundary")
        self._thread.start()
        if not self.connector.started.wait(1):
            raise AssertionError("parity connector did not start")
        self.boundary.session_events.receive(timeout=1)

    def emit(self, frame: str) -> bool:
        return self.connector.emit(frame)

    def receive_event(self, timeout: float = 1) -> StreamDockEvent:
        event = self.boundary.events.receive(timeout=timeout)
        self.boundary.events.task_done()
        return event

    def send_async(self, command: StreamDockCommand):
        return self.boundary.commands.send_async(command)

    @property
    def sent(self) -> list[str]:
        return self.connector.sent

    def metrics(self) -> StreamDockBoundaryMetrics:
        return self.boundary.metrics()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.boundary.close()
        self._thread.join(1)
        if self._thread.is_alive():
            raise AssertionError("boundary lifecycle thread did not finish")
        if self.errors:
            raise self.errors[0]

    def _run(self) -> None:
        try:
            self.boundary.run_forever()
        except Exception as exc:
            self.errors.append(exc)


class StreamDockBoundaryContractParityTests(unittest.TestCase):
    """Execute the same observable contracts against legacy and `_next`."""

    def setUp(self) -> None:
        self._closers: list[Callable[[], None]] = []

    def tearDown(self) -> None:
        for close in reversed(self._closers):
            close()

    def _legacy(self, **kwargs: object) -> _LegacyHarness:
        harness = _LegacyHarness(**kwargs)  # type: ignore[arg-type]
        self._closers.append(harness.close)
        return harness

    def _next(self, **kwargs: object) -> _NextHarness:
        harness = _NextHarness(**kwargs)  # type: ignore[arg-type]
        self._closers.append(harness.close)
        return harness

    def test_all_known_commands_have_identical_end_to_end_wire_output(self) -> None:
        legacy = self._legacy()
        next_boundary = self._next()
        expected_frames = [
            json.dumps(expected, ensure_ascii=False, allow_nan=False)
            for _command, expected in known_command_wire_fixtures()
        ]

        for command, _expected in known_command_wire_fixtures():
            with self.subTest(command=type(command).__name__):
                legacy.send_async(command).result(timeout=1)
                next_boundary.send_async(command).result(timeout=1)

        self.assertEqual(legacy.sent, expected_frames)
        self.assertEqual(next_boundary.sent, expected_frames)

    def test_malformed_and_unknown_frames_have_matching_observable_behavior(self) -> None:
        legacy = self._legacy()
        next_boundary = self._next()
        unknown = json.dumps(
            {
                "event": "futureEvent",
                "payload": {"version": 2, "features": ["typed"]},
            }
        )
        malformed_frames = (
            '{"event":"keyDown","payload":}',
            json.dumps(
                {
                    "event": "keyDown",
                    "action": "action-uuid",
                    "context": "button",
                    "device": "device-uuid",
                    "payload": {"settings": {}, "coordinates": {"column": 0, "row": 0}},
                }
            ),
            '{"event":"futureEvent","payload":{"value":NaN}}',
        )

        for frame in (unknown, *malformed_frames):
            legacy.emit(frame)
            self.assertTrue(next_boundary.emit(frame))

        legacy_event = legacy.receive_event()
        next_event = next_boundary.receive_event()
        self.assertIsInstance(legacy_event, UnknownStreamDockEvent)
        self.assertEqual(legacy_event, next_event)
        with self.assertRaises(TimeoutError):
            legacy.receive_event(timeout=0.05)
        with self.assertRaises(TimeoutError):
            next_boundary.receive_event(timeout=0.05)

        legacy_metrics = legacy.connection.inbound_queue_metrics
        next_metrics = next_boundary.metrics()
        self.assertEqual((legacy_metrics.received, legacy_metrics.dispatched), (1, 1))
        self.assertEqual(next_metrics.raw_inbound.enqueued, 4)
        self.assertEqual(next_metrics.event_reader.frames_received, 4)
        self.assertEqual(next_metrics.event_reader.decoded, 1)
        self.assertEqual(next_metrics.event_reader.protocol_failures, 3)
        self.assertEqual(next_metrics.event_reader.unknown_events, 1)
        self.assertEqual(next_metrics.inbound_events.dequeued, 1)

    def test_inbound_and_outbound_ordering_normalize_to_the_same_queue_metrics(self) -> None:
        legacy = self._legacy()
        next_boundary = self._next()
        frames = []
        for sequence in range(3):
            envelope = known_event_envelopes()["keyDown"]
            payload = envelope["payload"]
            assert isinstance(payload, dict)
            payload["settings"] = {"sequence": sequence}
            frames.append(json.dumps(envelope))

        for frame in frames:
            legacy.emit(frame)
            self.assertTrue(next_boundary.emit(frame))

        legacy_events = [legacy.receive_event() for _ in frames]
        next_events = [next_boundary.receive_event() for _ in frames]
        self.assertEqual(legacy_events, next_events)

        commands = tuple(LogMessageCommand(f"message-{index}") for index in range(3))
        legacy_completions = [legacy.send_async(command) for command in commands]
        next_completions = [next_boundary.send_async(command) for command in commands]
        for completion in (*legacy_completions, *next_completions):
            completion.result(timeout=1)

        self.assertEqual(legacy.sent, next_boundary.sent)
        legacy_inbound = legacy.connection.inbound_queue_metrics
        legacy_outbound = legacy.connection.outbound_queue_metrics
        next_metrics = next_boundary.metrics()
        self.assertEqual(
            (legacy_inbound.received, legacy_inbound.enqueued, legacy_inbound.dispatched),
            (
                next_metrics.event_reader.frames_received,
                next_metrics.inbound_events.enqueued,
                next_metrics.inbound_events.dequeued,
            ),
        )
        self.assertEqual(
            (legacy_outbound.enqueued, legacy_outbound.serialized, legacy_outbound.sent),
            (
                next_metrics.outbound_commands.enqueued,
                next_metrics.command_writer.serialized,
                next_metrics.connector.outbound_frames_sent,
            ),
        )
        self.assertLessEqual(legacy_inbound.peak_depth, legacy_inbound.queue_limit)
        self.assertLessEqual(legacy_outbound.peak_depth, legacy_outbound.queue_limit)
        for queue_metrics in (
            next_metrics.raw_inbound,
            next_metrics.inbound_events,
            next_metrics.outbound_commands,
            next_metrics.raw_outbound,
        ):
            self.assertLessEqual(queue_metrics.peak_depth, queue_metrics.queue_limit)

    def test_coalescing_and_queue_rejections_have_matching_metrics(self) -> None:
        results = []
        for implementation in ("legacy", "next"):
            with self.subTest(implementation=implementation):
                serialization_started = Event()
                release_serialization = Event()
                harness = (
                    self._legacy(queue_limit=1, coalesce_commands=True)
                    if implementation == "legacy"
                    else self._next(queue_limit=1, coalesce_commands=True)
                )
                blocker = harness.send_async(
                    _BlockingCommand(serialization_started, release_serialization)
                )
                self.assertTrue(serialization_started.wait(1))
                old_title = harness.send_async(SetTitleCommand("button", "old", target=1, state=2))
                new_title = harness.send_async(SetTitleCommand("button", "new", target=1, state=2))
                with self.assertRaises(RuntimeError):
                    harness.send_async(LogMessageCommand("queue full"))

                self.assertIsNot(old_title, new_title)
                if implementation == "legacy":
                    self.assertIs(old_title._state, new_title._state)
                else:
                    self.assertIs(old_title._future, new_title._future)

                if implementation == "legacy":
                    queued_metrics = harness.connection.outbound_queue_metrics
                else:
                    queued_metrics = harness.metrics().outbound_commands
                self.assertEqual(
                    (
                        queued_metrics.submitted,
                        queued_metrics.enqueued,
                        queued_metrics.coalesced,
                        queued_metrics.rejected_full,
                    ),
                    (4, 2, 1, 1),
                )

                release_serialization.set()
                for completion in (blocker, old_title, new_title):
                    completion.result(timeout=1)
                results.append(harness.sent.copy())

        self.assertEqual(results[0], results[1])
        self.assertEqual(
            [json.loads(frame) for frame in results[0]],
            [
                {"event": "logMessage", "payload": {"message": "blocker"}},
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "new", "target": 1, "state": 2},
                },
            ],
        )

    def test_dial_coalescing_has_matching_events_and_metrics(self) -> None:
        frames = [
            json.dumps(known_event_envelopes()["keyDown"]),
            _dial_rotate_frame(1),
            _dial_rotate_frame(2),
        ]
        callback_started = Event()
        release_callback = Event()

        def block_first_callback(event: StreamDockEvent) -> None:
            if event.event_name != "keyDown":
                return
            callback_started.set()
            if not release_callback.wait(1):
                raise AssertionError("timed out waiting to release legacy callback")

        legacy = self._legacy(
            queue_limit=3,
            event_hook=block_first_callback,
            coalesce_dial_rotations=True,
        )
        legacy.emit(frames[0])
        self.assertTrue(callback_started.wait(1))
        legacy.emit(frames[1])
        legacy.emit(frames[2])
        _wait_until(lambda: legacy.connection.inbound_queue_metrics.coalesced == 1)
        release_callback.set()
        legacy_events = [legacy.receive_event() for _ in range(2)]

        next_boundary = self._next(queue_limit=3, coalesce_dial_rotations=True)
        for frame in frames:
            self.assertTrue(next_boundary.emit(frame))
        _wait_until(lambda: next_boundary.metrics().event_reader.frames_received == 3)
        next_events = [next_boundary.receive_event() for _ in range(2)]

        self.assertEqual(legacy_events, next_events)
        self.assertIsInstance(legacy_events[1], DialRotateEvent)
        self.assertEqual(legacy_events[1].ticks, 3)
        legacy_metrics = legacy.connection.inbound_queue_metrics
        next_metrics = next_boundary.metrics()
        self.assertEqual(
            (legacy_metrics.received, legacy_metrics.enqueued, legacy_metrics.coalesced),
            (
                next_metrics.event_reader.frames_received,
                next_metrics.inbound_events.enqueued,
                next_metrics.inbound_events.coalesced,
            ),
        )

    def test_successful_shutdown_drains_an_in_flight_command(self) -> None:
        for implementation in ("legacy", "next"):
            with self.subTest(implementation=implementation):
                send_started = Event()
                release_send = Event()
                block_send = _blocking_send_hook(send_started, release_send)

                harness = (
                    self._legacy(send_hook=block_send, outbound_shutdown_timeout=1)
                    if implementation == "legacy"
                    else self._next(send_hook=block_send, shutdown_timeout=1)
                )
                completion = harness.send_async(LogMessageCommand("drain me"))
                self.assertTrue(send_started.wait(1))
                closer = Thread(target=harness.close)
                closer.start()
                sleep(0.02)
                self.assertTrue(closer.is_alive())
                self.assertFalse(completion.done())

                release_send.set()
                closer.join(1)
                self.assertFalse(closer.is_alive())
                self.assertIsNone(completion.result(timeout=0))
                self.assertEqual(
                    [json.loads(frame) for frame in harness.sent],
                    [{"event": "logMessage", "payload": {"message": "drain me"}}],
                )

    def test_shutdown_fails_stalled_accepted_commands_and_rejects_late_ones(self) -> None:
        legacy_write_started = Event()
        release_legacy_write = Event()

        def block_legacy_write(_payload: str) -> None:
            legacy_write_started.set()
            if not release_legacy_write.wait(1):
                raise AssertionError("timed out waiting to release legacy write")

        legacy = self._legacy(
            outbound_shutdown_timeout=0,
            send_hook=block_legacy_write,
            close_hook=release_legacy_write.set,
        )
        legacy_completion = legacy.send_async(LogMessageCommand("stalled"))
        self.assertTrue(legacy_write_started.wait(1))
        legacy.close()
        self._closers.remove(legacy.close)

        next_boundary = self._next(
            queue_limit=1,
            shutdown_timeout=0,
            consume_outbound=False,
        )
        next_completion = next_boundary.send_async(LogMessageCommand("stalled"))
        _wait_until(lambda: next_boundary.metrics().raw_outbound.current_depth == 1)
        next_boundary.close()
        self._closers.remove(next_boundary.close)

        for completion in (legacy_completion, next_completion):
            self.assertTrue(completion.done())
            self.assertIsNotNone(completion.exception(timeout=0))
        with self.assertRaises(RuntimeError):
            legacy.send_async(LogMessageCommand("late"))
        with self.assertRaises(RuntimeError):
            next_boundary.send_async(LogMessageCommand("late"))

        legacy_metrics = legacy.connection.outbound_queue_metrics
        next_metrics = next_boundary.metrics()
        self.assertEqual(legacy_metrics.rejected_after_shutdown, 1)
        self.assertEqual(next_metrics.outbound_commands.rejected_after_shutdown, 1)
        self.assertGreaterEqual(next_metrics.raw_outbound.discarded_during_shutdown, 1)
        self.assertEqual(legacy.socket.close_calls, 1)
        self.assertEqual(next_boundary.connector.close_calls, 1)


class _BlockingCommand(StreamDockCommand):
    def __init__(self, started: Event, release: Event) -> None:
        self._started = started
        self._release = release

    def to_wire(self) -> JsonObject:
        self._started.set()
        if not self._release.wait(1):
            raise AssertionError("timed out waiting to release command serialization")
        return {"event": "logMessage", "payload": {"message": "blocker"}}


def _dial_rotate_frame(ticks: int) -> str:
    envelope = known_event_envelopes()["dialRotate"]
    payload = envelope["payload"]
    assert isinstance(payload, dict)
    payload["ticks"] = ticks
    return json.dumps(envelope)


def _blocking_send_hook(started: Event, release: Event) -> Callable[[str], None]:
    def block_send(_payload: str) -> None:
        started.set()
        if not release.wait(1):
            raise AssertionError("timed out waiting to release transport send")

    return block_send


if __name__ == "__main__":
    unittest.main()

"""Benchmark contract-relevant legacy and experimental boundary scenarios.

Run from a source checkout with ``PYTHONPATH=src``. The script deliberately
uses fake transports so results measure SDK behavior without a Stream Dock
installation or network variance.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import platform
import statistics
import sys
import tracemalloc
from collections.abc import Callable
from dataclasses import asdict, dataclass
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import perf_counter, sleep
from unittest.mock import patch

from mirabox_sdk import (
    DialRotateEvent,
    LogMessageCommand,
    SetImageCommand,
    SetTitleCommand,
    StreamDockCommand,
    StreamDockEvent,
)
from mirabox_sdk._next.boundary.composition import create_stream_dock_boundary
from mirabox_sdk._next.boundary.config import BoundaryQueueConfig, BoundaryShutdownConfig
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
from mirabox_sdk.connection import WebSocketStreamDockConnection
from mirabox_sdk.inbound import InboundOverflowPolicy as LegacyInboundOverflowPolicy


@dataclass(frozen=True, slots=True)
class BenchmarkMeasurement:
    """Median measurements from one implementation/scenario pair."""

    latency_ms: float
    throughput_per_second: float
    net_allocation_blocks: float
    net_allocated_bytes: float
    peak_traced_bytes: float
    peak_queue_depth: float


@dataclass(frozen=True, slots=True)
class BenchmarkBudget:
    """Accepted `_next` overhead relative to same-run legacy measurements."""

    max_latency_ratio: float
    max_peak_traced_bytes_ratio: float
    reason: str


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """Ratios and guardrail result for one scenario."""

    latency_ratio: float
    throughput_ratio: float
    peak_traced_bytes_ratio: float
    peak_queue_depth_ratio: float
    max_latency_ratio: float
    max_peak_traced_bytes_ratio: float
    within_budget: bool
    violations: tuple[str, ...]
    budget_reason: str


class _LegacySocket:
    def __init__(
        self,
        *,
        send_delay: float = 0,
        send_gate: Event | None = None,
        send_started: Event | None = None,
        close_hook: Callable[[], None] | None = None,
    ) -> None:
        self._send_delay = send_delay
        self._send_gate = send_gate
        self._send_started = send_started
        self._close_hook = close_hook
        self._lock = Lock()
        self._sent_count = 0

    def send(self, payload: str) -> None:
        if self._send_started is not None:
            self._send_started.set()
        if self._send_gate is not None and not self._send_gate.wait(1):
            raise RuntimeError("benchmark legacy transport was not released")
        if self._send_delay:
            sleep(self._send_delay)
        with self._lock:
            self._sent_count += 1

    def close(self) -> None:
        if self._close_hook is not None:
            self._close_hook()

    def sent_count(self) -> int:
        with self._lock:
            return self._sent_count


class _LegacyListener:
    def __init__(self, *, event_delay: float = 0) -> None:
        self._event_delay = event_delay
        self.events: Queue[StreamDockEvent] = Queue()

    def on_stream_dock_connected(self) -> None:
        pass

    def on_stream_dock_event(self, event: StreamDockEvent) -> None:
        if self._event_delay:
            sleep(self._event_delay)
        self.events.put(event)


class _LegacyBoundaryHarness:
    def __init__(
        self,
        *,
        queue_limit: int,
        coalesce_dial_rotations: bool = False,
        send_delay: float = 0,
        event_delay: float = 0,
        send_gate: Event | None = None,
        send_started: Event | None = None,
        close_hook: Callable[[], None] | None = None,
        shutdown_timeout: float | None = 1,
    ) -> None:
        self.socket = _LegacySocket(
            send_delay=send_delay,
            send_gate=send_gate,
            send_started=send_started,
            close_hook=close_hook,
        )
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
                inbound_shutdown_timeout=shutdown_timeout,
                outbound_queue_limit=queue_limit,
                outbound_shutdown_timeout=shutdown_timeout,
            )
        self.listener = _LegacyListener(event_delay=event_delay)
        self.connection.set_listener(self.listener)
        self.connection._inbound.start()
        self._closed = False

    def emit(self, frame: str) -> None:
        self.connection._on_message(self.socket, frame)

    def receive_event(self, timeout: float = 2) -> StreamDockEvent:
        try:
            return self.listener.events.get(timeout=timeout)
        except Empty as exc:
            raise TimeoutError("benchmark legacy event timed out") from exc

    def send_async(self, command: StreamDockCommand):
        return self.connection.send_async(command)

    def peak_inbound_depth(self) -> int:
        return self.connection.inbound_queue_metrics.peak_depth

    def peak_outbound_depth(self) -> int:
        return self.connection.outbound_queue_metrics.peak_depth

    def sent_count(self) -> int:
        return self.socket.sent_count()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.connection.close()


class _BenchmarkConnector(WebSocketConnector):
    def __init__(
        self,
        raw_inbound: RawInboundSink,
        raw_outbound: RawOutboundSource,
        session_events: SessionEventSink,
        *,
        consume_outbound: bool,
        send_delay: float,
    ) -> None:
        self._raw_inbound = raw_inbound
        self._raw_outbound = raw_outbound
        self._session_events = session_events
        self._consume_outbound = consume_outbound
        self._send_delay = send_delay
        self._stop_requested = Event()
        self.started = Event()
        self._lock = Lock()
        self._connect_count = 0
        self._disconnect_count = 0
        self._inbound_received = 0
        self._inbound_forwarded = 0
        self._inbound_rejected = 0
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
                Disconnected(status_code=1000, reason="benchmark connector closed"),
                timeout=0,
            ):
                with self._lock:
                    self._disconnect_count += 1

    def close(self) -> None:
        self._stop_requested.set()

    def emit(self, frame: str) -> bool:
        with self._lock:
            self._inbound_received += 1
        accepted = self._raw_inbound.submit(frame)
        with self._lock:
            if accepted:
                self._inbound_forwarded += 1
            else:
                self._inbound_rejected += 1
        return accepted

    def metrics(self) -> WebSocketConnectorMetrics:
        with self._lock:
            return WebSocketConnectorMetrics(
                connect_count=self._connect_count,
                disconnect_count=self._disconnect_count,
                last_close_code=1000 if self._disconnect_count else None,
                transport_error_count=0,
                session_events_rejected=0,
                inbound_frames_received=self._inbound_received,
                inbound_frames_forwarded=self._inbound_forwarded,
                inbound_frames_rejected=self._inbound_rejected,
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
        if self._send_delay:
            sleep(self._send_delay)
        with self._lock:
            self._outbound_sent += 1
        frame.receipt._finish()


class _BenchmarkConnectorFactory:
    def __init__(self, *, consume_outbound: bool, send_delay: float) -> None:
        self._consume_outbound = consume_outbound
        self._send_delay = send_delay
        self.connector: _BenchmarkConnector | None = None

    def __call__(
        self,
        raw_inbound_sink: RawInboundSink,
        raw_outbound_source: RawOutboundSource,
        session_event_sink: SessionEventSink,
    ) -> WebSocketConnector:
        self.connector = _BenchmarkConnector(
            raw_inbound_sink,
            raw_outbound_source,
            session_event_sink,
            consume_outbound=self._consume_outbound,
            send_delay=self._send_delay,
        )
        return self.connector


class _NextBoundaryHarness:
    def __init__(
        self,
        *,
        queue_limit: int,
        coalesce_dial_rotations: bool = False,
        consume_outbound: bool = True,
        send_delay: float = 0,
        shutdown_timeout: float = 1,
    ) -> None:
        factory = _BenchmarkConnectorFactory(
            consume_outbound=consume_outbound,
            send_delay=send_delay,
        )
        queue_config = BoundaryQueueConfig(
            raw_inbound_limit=queue_limit,
            inbound_event_limit=queue_limit,
            outbound_command_limit=queue_limit,
            raw_outbound_limit=queue_limit,
            session_event_limit=queue_limit,
        )
        shutdown_config = BoundaryShutdownConfig(
            raw_inbound_drain_timeout=shutdown_timeout,
            inbound_event_drain_timeout=shutdown_timeout,
            outbound_command_drain_timeout=shutdown_timeout,
            raw_outbound_drain_timeout=shutdown_timeout,
            session_event_drain_timeout=0,
            worker_stop_timeout=shutdown_timeout,
            connector_stop_timeout=shutdown_timeout,
        )
        self.boundary = create_stream_dock_boundary(
            12345,
            queue_config,
            shutdown_config=shutdown_config,
            connector_factory=factory,
            coalesce_dial_rotations=coalesce_dial_rotations,
        )
        assert factory.connector is not None
        self.connector = factory.connector
        self.errors: list[Exception] = []
        self._closed = False
        self._thread = Thread(target=self._run, name="benchmark-next-boundary")
        self._thread.start()
        if not self.connector.started.wait(2):
            raise RuntimeError("benchmark connector did not start")
        self.boundary.session_events.receive(timeout=2)

    def emit(self, frame: str) -> bool:
        return self.connector.emit(frame)

    def receive_event(self, timeout: float = 2) -> StreamDockEvent:
        event = self.boundary.events.receive(timeout=timeout)
        try:
            return event
        finally:
            self.boundary.events.task_done()

    def send_async(self, command: StreamDockCommand):
        return self.boundary.commands.send_async(command)

    def peak_inbound_depth(self) -> int:
        metrics = self.boundary.metrics()
        return max(metrics.raw_inbound.peak_depth, metrics.inbound_events.peak_depth)

    def peak_outbound_depth(self) -> int:
        metrics = self.boundary.metrics()
        return max(metrics.outbound_commands.peak_depth, metrics.raw_outbound.peak_depth)

    def sent_count(self) -> int:
        return self.connector.metrics().outbound_frames_sent

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.boundary.close()
        self._thread.join(2)
        if self._thread.is_alive():
            raise RuntimeError("benchmark boundary thread did not stop")
        if self.errors:
            raise self.errors[0]

    def _run(self) -> None:
        try:
            self.boundary.run_forever()
        except Exception as exc:
            self.errors.append(exc)


def _key_frame(event_name: str, sequence: int) -> str:
    return json.dumps(
        {
            "event": event_name,
            "action": "action-uuid",
            "context": "button",
            "device": "device-uuid",
            "payload": {
                "settings": {"sequence": sequence},
                "coordinates": {"column": 0, "row": 0},
                "isInMultiAction": False,
            },
        }
    )


def _dial_rotate_frame(sequence: int) -> str:
    return json.dumps(
        {
            "event": "dialRotate",
            "action": "action-uuid",
            "context": "dial",
            "device": "device-uuid",
            "payload": {
                "settings": {"sequence": sequence},
                "coordinates": {"column": 0, "row": 0},
                "ticks": 1,
                "pressed": False,
            },
        }
    )


def _new_harness(
    implementation: str,
    *,
    queue_limit: int,
    coalesce_dial_rotations: bool = False,
    consume_outbound: bool = True,
    send_delay: float = 0,
    event_delay: float = 0,
    shutdown_timeout: float | None = 1,
):
    if implementation == "legacy":
        return _LegacyBoundaryHarness(
            queue_limit=queue_limit,
            coalesce_dial_rotations=coalesce_dial_rotations,
            send_delay=send_delay,
            event_delay=event_delay,
            shutdown_timeout=shutdown_timeout,
        )
    return _NextBoundaryHarness(
        queue_limit=queue_limit,
        coalesce_dial_rotations=coalesce_dial_rotations,
        consume_outbound=consume_outbound,
        send_delay=send_delay,
        shutdown_timeout=1 if shutdown_timeout is None else shutdown_timeout,
    )


def _burst_key_transitions(implementation: str, messages: int, _image_bytes: int) -> int:
    harness = _new_harness(implementation, queue_limit=messages * 2 + 1)
    frames = [
        frame
        for sequence in range(messages)
        for frame in (_key_frame("keyDown", sequence), _key_frame("keyUp", sequence))
    ]
    try:
        for frame in frames:
            harness.emit(frame)
        events = [harness.receive_event() for _ in frames]
        expected = [
            (event_name, sequence)
            for sequence in range(messages)
            for event_name in ("keyDown", "keyUp")
        ]
        actual = [(event.event_name, event.settings["sequence"]) for event in events]
        if actual != expected:
            raise RuntimeError("key transition benchmark produced incorrect event order")
        return harness.peak_inbound_depth()
    finally:
        harness.close()


def _intensive_dial_rotations(implementation: str, messages: int, _image_bytes: int) -> int:
    harness = _new_harness(
        implementation,
        queue_limit=messages + 1,
        coalesce_dial_rotations=True,
    )
    try:
        for sequence in range(messages):
            harness.emit(_dial_rotate_frame(sequence))
        if implementation == "legacy":
            _wait_until(
                lambda: (
                    harness.connection.inbound_queue_metrics.dispatched
                    == harness.connection.inbound_queue_metrics.enqueued
                ),
                "legacy dial rotation queue did not drain",
            )
            event_count = harness.connection.inbound_queue_metrics.dispatched
        else:
            _wait_until(
                lambda: harness.boundary.metrics().event_reader.submitted == messages,
                "next dial rotation reader did not process every frame",
            )
            event_count = harness.boundary.metrics().inbound_events.current_depth
        events = [harness.receive_event() for _ in range(event_count)]
        tick_sum = sum(event.ticks for event in events if isinstance(event, DialRotateEvent))
        if tick_sum != messages:
            raise RuntimeError("dial rotation benchmark changed the total tick count")
        return harness.peak_inbound_depth()
    finally:
        harness.close()


def _large_set_image(implementation: str, messages: int, image_bytes: int) -> int:
    command_count = max(1, messages // 8)
    image = "data:image/png;base64," + ("A" * image_bytes)
    harness = _new_harness(implementation, queue_limit=command_count + 1)
    try:
        completions = [
            harness.send_async(SetImageCommand("button", image, state=index % 2))
            for index in range(command_count)
        ]
        for completion in completions:
            completion.result(timeout=5)
        if harness.sent_count() != command_count:
            raise RuntimeError("setImage benchmark did not send every accepted command")
        return harness.peak_outbound_depth()
    finally:
        harness.close()


def _concurrent_set_title(implementation: str, messages: int, _image_bytes: int) -> int:
    harness = _new_harness(implementation, queue_limit=messages + 1)
    completions = []
    completions_lock = Lock()
    producer_errors: list[Exception] = []

    def submit(index: int) -> None:
        try:
            completion = harness.send_async(SetTitleCommand("button", f"title-{index}"))
        except Exception as exc:
            with completions_lock:
                producer_errors.append(exc)
        else:
            with completions_lock:
                completions.append(completion)

    try:
        threads = [Thread(target=submit, args=(index,)) for index in range(messages)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            if thread.is_alive():
                raise RuntimeError("concurrent benchmark producer did not finish")
        if producer_errors:
            raise ExceptionGroup("concurrent benchmark producers failed", producer_errors)
        if len(completions) != messages:
            raise RuntimeError("concurrent benchmark lost command completions")
        for completion in completions:
            completion.result(timeout=5)
        if harness.sent_count() != messages:
            raise RuntimeError("concurrent benchmark did not send every accepted command")
        return harness.peak_outbound_depth()
    finally:
        harness.close()


def _slow_consumer(implementation: str, messages: int, _image_bytes: int) -> int:
    delay = 0.001
    harness = _new_harness(
        implementation,
        queue_limit=4,
        event_delay=delay if implementation == "legacy" else 0,
    )
    frames = [_key_frame("keyDown", sequence) for sequence in range(messages)]
    producer_error: list[Exception] = []

    def produce() -> None:
        try:
            for frame in frames:
                harness.emit(frame)
        except Exception as exc:
            producer_error.append(exc)

    try:
        producer = Thread(target=produce)
        producer.start()
        events = []
        for _ in frames:
            if implementation == "next":
                sleep(delay)
            events.append(harness.receive_event(timeout=5))
        producer.join(5)
        if producer.is_alive():
            raise RuntimeError("slow-consumer benchmark producer did not finish")
        if producer_error:
            raise producer_error[0]
        if [event.settings["sequence"] for event in events] != list(range(messages)):
            raise RuntimeError("slow-consumer benchmark changed event order")
        return harness.peak_inbound_depth()
    finally:
        harness.close()


def _slow_websocket_send(implementation: str, messages: int, _image_bytes: int) -> int:
    harness = _new_harness(
        implementation,
        queue_limit=messages + 1,
        send_delay=0.001,
    )
    try:
        completions = [
            harness.send_async(LogMessageCommand(f"message-{index}")) for index in range(messages)
        ]
        for completion in completions:
            completion.result(timeout=5)
        if harness.sent_count() != messages:
            raise RuntimeError("slow-send benchmark did not send every accepted command")
        return harness.peak_outbound_depth()
    finally:
        harness.close()


def _disconnect_with_filled_queues(implementation: str, messages: int, _image_bytes: int) -> int:
    if implementation == "legacy":
        send_gate = Event()
        send_started = Event()
        harness = _LegacyBoundaryHarness(
            queue_limit=messages + 1,
            send_gate=send_gate,
            send_started=send_started,
            close_hook=send_gate.set,
            shutdown_timeout=0,
        )
        try:
            completions = [harness.send_async(LogMessageCommand("first"))]
            if not send_started.wait(2):
                raise RuntimeError("legacy disconnect benchmark did not start sending")
            completions.extend(
                harness.send_async(LogMessageCommand(f"queued-{index}"))
                for index in range(messages - 1)
            )
            _wait_for_depth(harness.peak_outbound_depth)
            harness.close()
            for completion in completions:
                completion.exception(timeout=2)
            return harness.peak_outbound_depth()
        finally:
            harness.close()

    harness = _NextBoundaryHarness(
        queue_limit=messages + 1,
        consume_outbound=False,
        shutdown_timeout=0,
    )
    try:
        completions = [
            harness.send_async(LogMessageCommand(f"queued-{index}")) for index in range(messages)
        ]
        _wait_for_depth(harness.peak_outbound_depth)
        harness.close()
        for completion in completions:
            completion.exception(timeout=2)
        return harness.peak_outbound_depth()
    finally:
        harness.close()


def _wait_for_depth(depth: Callable[[], int]) -> None:
    _wait_until(lambda: depth() > 0, "benchmark queue did not receive work")


def _wait_until(predicate: Callable[[], bool], error: str) -> None:
    deadline = perf_counter() + 2
    while not predicate():
        if perf_counter() >= deadline:
            raise RuntimeError(error)
        sleep(0.005)


Scenario = Callable[[str, int, int], int]

SCENARIOS: dict[str, Scenario] = {
    "burst_key_down_up": _burst_key_transitions,
    "intensive_dial_rotate": _intensive_dial_rotations,
    "large_set_image": _large_set_image,
    "concurrent_set_title": _concurrent_set_title,
    "slow_consumer": _slow_consumer,
    "slow_websocket_send": _slow_websocket_send,
    "disconnect_with_filled_queues": _disconnect_with_filled_queues,
}

BENCHMARK_BUDGETS: dict[str, BenchmarkBudget] = {
    "burst_key_down_up": BenchmarkBudget(
        1.5,
        1.5,
        "one additional raw-to-typed inbound handoff",
    ),
    "intensive_dial_rotate": BenchmarkBudget(
        1.5,
        1.75,
        "typed coalescing follows sequential frame decoding",
    ),
    "large_set_image": BenchmarkBudget(
        1.5,
        3.0,
        "the raw queue can temporarily retain encoded image frames",
    ),
    "concurrent_set_title": BenchmarkBudget(
        1.75,
        2.0,
        "concurrent submissions retain per-command completion state",
    ),
    "slow_consumer": BenchmarkBudget(
        1.3,
        1.5,
        "bounded raw backpressure isolates the WebSocket reader",
    ),
    "slow_websocket_send": BenchmarkBudget(
        1.3,
        3.0,
        "pending TransportReceipt objects make slow sends observable",
    ),
    "disconnect_with_filled_queues": BenchmarkBudget(
        5.5,
        3.5,
        "every typed and raw queue is drained or failed explicitly",
    ),
}


def _measure(work: Callable[[], int], *, operations: int) -> BenchmarkMeasurement:
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    started = perf_counter()
    peak_queue_depth = work()
    elapsed = perf_counter() - started
    after = tracemalloc.take_snapshot()
    _current, peak_traced_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    differences = after.compare_to(before, "lineno")
    net_allocated_bytes = sum(statistic.size_diff for statistic in differences)
    net_allocation_blocks = sum(statistic.count_diff for statistic in differences)
    return BenchmarkMeasurement(
        latency_ms=elapsed * 1_000,
        throughput_per_second=operations / elapsed if elapsed else float("inf"),
        net_allocation_blocks=net_allocation_blocks,
        net_allocated_bytes=net_allocated_bytes,
        peak_traced_bytes=peak_traced_bytes,
        peak_queue_depth=peak_queue_depth,
    )


def _median_measurement(samples: list[BenchmarkMeasurement]) -> BenchmarkMeasurement:
    return BenchmarkMeasurement(
        **{
            field: statistics.median(getattr(sample, field) for sample in samples)
            for field in BenchmarkMeasurement.__dataclass_fields__
        }
    )


def _compare_measurements(
    legacy: BenchmarkMeasurement,
    next_boundary: BenchmarkMeasurement,
    budget: BenchmarkBudget,
) -> BenchmarkComparison:
    latency_ratio = _ratio(next_boundary.latency_ms, legacy.latency_ms)
    peak_traced_bytes_ratio = _ratio(
        next_boundary.peak_traced_bytes,
        legacy.peak_traced_bytes,
    )
    violations = []
    if latency_ratio > budget.max_latency_ratio:
        violations.append(
            f"latency ratio {latency_ratio:.3f} exceeds {budget.max_latency_ratio:.3f}"
        )
    if peak_traced_bytes_ratio > budget.max_peak_traced_bytes_ratio:
        violations.append(
            "peak traced bytes ratio "
            f"{peak_traced_bytes_ratio:.3f} exceeds {budget.max_peak_traced_bytes_ratio:.3f}"
        )
    return BenchmarkComparison(
        latency_ratio=latency_ratio,
        throughput_ratio=_ratio(
            next_boundary.throughput_per_second,
            legacy.throughput_per_second,
        ),
        peak_traced_bytes_ratio=peak_traced_bytes_ratio,
        peak_queue_depth_ratio=_ratio(
            next_boundary.peak_queue_depth,
            legacy.peak_queue_depth,
        ),
        max_latency_ratio=budget.max_latency_ratio,
        max_peak_traced_bytes_ratio=budget.max_peak_traced_bytes_ratio,
        within_budget=not violations,
        violations=tuple(violations),
        budget_reason=budget.reason,
    )


def _ratio(candidate: float, baseline: float) -> float:
    if baseline == 0:
        return 1.0 if candidate == 0 else float("inf")
    return candidate / baseline


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--messages", type=int, default=128)
    parser.add_argument("--image-bytes", type=int, default=256 * 1024)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), action="append")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit unsuccessfully when a same-run legacy budget is exceeded",
    )
    arguments = parser.parse_args()
    for name in ("iterations", "messages", "image_bytes"):
        if getattr(arguments, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be a positive integer")
    return arguments


def main() -> int:
    arguments = _parse_arguments()
    logging.disable(logging.CRITICAL)
    scenario_names = arguments.scenario or tuple(SCENARIOS)
    operations_by_scenario = {
        "burst_key_down_up": arguments.messages * 2,
        "intensive_dial_rotate": arguments.messages,
        "large_set_image": max(1, arguments.messages // 8),
        "concurrent_set_title": arguments.messages,
        "slow_consumer": arguments.messages,
        "slow_websocket_send": arguments.messages,
        "disconnect_with_filled_queues": arguments.messages,
    }
    results: dict[str, dict[str, object]] = {}
    budget_failures = []
    for scenario_name in scenario_names:
        scenario = SCENARIOS[scenario_name]
        samples: dict[str, list[BenchmarkMeasurement]] = {"legacy": [], "next": []}
        for iteration in range(arguments.iterations):
            implementation_order = ("legacy", "next") if iteration % 2 == 0 else ("next", "legacy")
            for implementation in implementation_order:
                samples[implementation].append(
                    _measure(
                        lambda scenario=scenario, implementation=implementation: scenario(
                            implementation,
                            arguments.messages,
                            arguments.image_bytes,
                        ),
                        operations=operations_by_scenario[scenario_name],
                    )
                )
        measurements = {
            implementation: _median_measurement(implementation_samples)
            for implementation, implementation_samples in samples.items()
        }
        comparison = _compare_measurements(
            measurements["legacy"],
            measurements["next"],
            BENCHMARK_BUDGETS[scenario_name],
        )
        if not comparison.within_budget:
            budget_failures.append(scenario_name)
        results[scenario_name] = {
            "legacy": asdict(measurements["legacy"]),
            "next": asdict(measurements["next"]),
            "comparison": asdict(comparison),
        }

    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "iterations": arguments.iterations,
                "messages": arguments.messages,
                "image_bytes": arguments.image_bytes,
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if arguments.check and budget_failures:
        print(
            "benchmark budgets exceeded: " + ", ".join(budget_failures),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

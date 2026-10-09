"""Measure mixed typed-event scheduling and sustained memory use for QA-01.

Real inbound queues, pumps and schedulers dispatch synthetic callbacks. Parsing,
action management, commands, sockets and devices are excluded. Memory runs use
tracemalloc separately from timing runs; RSS samples are available on Linux.
Only ordering, completion and queue bounds are checked, with no timing or memory
growth budget. Each soak retains one runtime throughout warmup and measurement.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import tracemalloc
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from math import isfinite
from pathlib import Path
from threading import Lock
from time import perf_counter, sleep

from mirabox_sdk import (
    Controller,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    KeyUpEvent,
    StreamDockEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from mirabox_sdk._internal.messaging.inbound import InboundEventQueue
from mirabox_sdk._internal.runtime.keyed_scheduler import KeyedSerialHandlerScheduler
from mirabox_sdk._internal.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._internal.runtime.pumps import RuntimeEventPump
from mirabox_sdk._internal.runtime.scheduler import SequentialHandlerScheduler
from scripts.benchmark_runtime_scheduler import _key_down, _percentile, _require_positive_integer


@dataclass(frozen=True, slots=True)
class WorkloadMeasurement:
    scheduler: str
    callback_delay_seconds: float
    batches: int
    event_count: int
    duration_seconds: float
    throughput_per_second: float
    callback_start_p95_ms: float | None
    callback_start_p99_ms: float | None
    barriers_processed: int
    ordering_violations: int
    source: dict[str, int]
    scheduler_metrics: dict[str, int]
    passed: bool


@dataclass(frozen=True, slots=True)
class MemorySample:
    elapsed_seconds: float
    acknowledged_events: int
    traced_retained_bytes: int
    traced_peak_bytes: int
    rss_bytes: int | None
    source_retained_bytes: int
    scheduler_pending_bytes: int


@dataclass(frozen=True, slots=True)
class MemorySoakMeasurement:
    workload: WorkloadMeasurement
    warmup_events: int
    traced_growth_bytes: int
    rss_growth_bytes: int | None
    samples: tuple[MemorySample, ...]


@dataclass(frozen=True, slots=True)
class _Submission:
    sequence: int
    segment: int
    context_sequence: int
    is_barrier: bool
    submitted_at: float


class _MixedDispatcher:
    def __init__(self, callback_delay: float, *, record_latency: bool) -> None:
        self.callback_delay = callback_delay
        self.record_latency = record_latency
        self.lock = Lock()
        self.submissions: dict[int, _Submission] = {}
        self.submitted_by_context: dict[str, int] = {}
        self.finished_by_context: dict[str, int] = {}
        self.active_contexts: set[str] = set()
        self.barrier_active = False
        self.submitted = 0
        self.submitted_barriers = 0
        self.completed = 0
        self.completed_barriers = 0
        self.ordering_violations = 0
        self.latencies: list[float] = []

    def mark_submitted(self, event: StreamDockEvent, *, is_barrier: bool) -> None:
        context = getattr(event, "context", "")
        with self.lock:
            ordinal = self.submitted_by_context.get(context, 0)
            self.submissions[id(event)] = _Submission(
                self.submitted, self.submitted_barriers, ordinal, is_barrier, perf_counter()
            )
            self.submitted += 1
            if is_barrier:
                self.submitted_barriers += 1
            else:
                self.submitted_by_context[context] = ordinal + 1

    def dispatch(self, event: StreamDockEvent) -> DispatchResult:
        context = getattr(event, "context", "")
        with self.lock:
            submission = self.submissions.pop(id(event))
            valid = submission.segment == self.completed_barriers and not self.barrier_active
            if submission.is_barrier:
                valid &= not self.active_contexts and self.completed == submission.sequence
                self.barrier_active = True
            else:
                valid &= context not in self.active_contexts
                valid &= submission.context_sequence == self.finished_by_context.get(context, 0)
                self.active_contexts.add(context)
            self.ordering_violations += int(not valid)
            if self.record_latency:
                self.latencies.append((perf_counter() - submission.submitted_at) * 1000)
        if self.callback_delay:
            sleep(self.callback_delay)
        with self.lock:
            self.completed += 1
            if submission.is_barrier:
                self.completed_barriers += 1
                self.barrier_active = False
            else:
                self.active_contexts.discard(context)
                self.finished_by_context[context] = submission.context_sequence + 1
        return DispatchResult(DispatchOutcome.HANDLED)


def _mixed_events(
    events_per_segment: int, payload_sizes: tuple[int, ...]
) -> Iterator[tuple[StreamDockEvent, bool]]:
    """Four input segments: 80% hot context, cold contexts, five global barriers."""

    template = _key_down("lifecycle")
    yield (
        WillAppearEvent(
            action=template.action,
            context=template.context,
            device=template.device,
            settings=template.settings,
            coordinates=template.coordinates,
            controller=Controller.KEYPAD,
            is_in_multi_action=template.is_in_multi_action,
        ),
        True,
    )
    barriers = (
        DidReceiveGlobalSettingsEvent(settings={"count": 1}),
        UnknownStreamDockEvent(event="futureEvent", data={"event": "futureEvent"}),
        DidReceiveGlobalSettingsEvent(settings={"count": 2}),
        WillDisappearEvent(
            action=template.action,
            context=template.context,
            device=template.device,
            settings=template.settings,
            coordinates=template.coordinates,
            controller=Controller.KEYPAD,
            is_in_multi_action=template.is_in_multi_action,
        ),
    )
    for segment, barrier in enumerate(barriers):
        for index in range(events_per_segment):
            context = "hot" if index % 5 < 4 else f"cold-{index % 16}"
            size = payload_sizes[(segment * events_per_segment + index) % len(payload_sizes)]
            key = replace(_key_down(context), settings={"payload": "x" * size})
            if index % 3 == 0:
                event: StreamDockEvent = key
            elif index % 3 == 1:
                event = KeyUpEvent(
                    action=key.action,
                    context=context,
                    device=key.device,
                    settings=key.settings,
                    coordinates=key.coordinates,
                    controller=key.controller,
                    is_in_multi_action=False,
                )
            else:
                event = DidReceiveSettingsEvent(
                    action=key.action,
                    context=context,
                    device=key.device,
                    settings=key.settings,
                    coordinates=key.coordinates,
                    is_in_multi_action=False,
                )
            yield event, False
        yield barrier, True


class _Workload:
    def __init__(
        self,
        scheduler_kind: str,
        *,
        callback_delay: float,
        worker_count: int,
        pending_limit: int,
        source_limit: int,
        record_latency: bool,
    ) -> None:
        if scheduler_kind not in ("sequential", "keyed_serial"):
            raise ValueError("unknown scheduler kind")
        self.dispatcher = _MixedDispatcher(callback_delay, record_latency=record_latency)
        self.source = InboundEventQueue(source_limit)
        self.scheduler = (
            SequentialHandlerScheduler(self.dispatcher)
            if scheduler_kind == "sequential"
            else KeyedSerialHandlerScheduler(
                self.dispatcher, worker_count=worker_count, pending_limit=pending_limit
            )
        )
        self.pump = RuntimeEventPump(self.source, self.scheduler, poll_interval=0.001)
        self.kind = scheduler_kind
        self.pending_limit = pending_limit
        self.batches = 0

    def start(self) -> None:
        self.scheduler.start()
        self.pump.start()

    def batch(self, events_per_segment: int, payload_sizes: tuple[int, ...]) -> None:
        for event, is_barrier in _mixed_events(events_per_segment, payload_sizes):
            self.dispatcher.mark_submitted(event, is_barrier=is_barrier)
            if not self.source.submit(event, timeout=5):
                raise RuntimeError("mixed workload source rejected an event")
        if not self.source.drain(timeout=10) or not self.scheduler.drain(timeout=10):
            raise TimeoutError("mixed workload did not drain")
        if self.pump.failure is not None:
            raise RuntimeError("mixed workload pump failed") from self.pump.failure
        self.batches += 1

    def stop(self) -> None:
        self.source.stop_accepting()
        pump_stopped = self.pump.stop(timeout=5)
        scheduler_stopped = self.scheduler.stop(timeout=5)
        if not pump_stopped or not scheduler_stopped:
            raise TimeoutError("mixed workload workers did not stop")

    def measurement(self, duration: float) -> WorkloadMeasurement:
        source = self.source.metrics()
        scheduler = self.scheduler.metrics()
        dispatcher = self.dispatcher
        passed = (
            dispatcher.ordering_violations == 0
            and source.acknowledged == dispatcher.submitted == scheduler.completed
            and scheduler.barriers_processed == dispatcher.submitted_barriers
            and not dispatcher.submissions
            and not source.dropped
            and source.current_depth == source.in_flight == source.current_bytes == 0
            and scheduler.current_pending == scheduler.current_pending_bytes == 0
            and scheduler.current_active_callbacks == scheduler.callback_failures == 0
            and source.peak_depth <= source.queue_limit
            and source.peak_bytes <= source.byte_limit
            and scheduler.peak_pending <= self.pending_limit
            and scheduler.peak_pending_bytes <= scheduler.pending_byte_limit
            and self.pump.failure is None
        )
        return WorkloadMeasurement(
            self.kind,
            dispatcher.callback_delay,
            self.batches,
            dispatcher.submitted,
            duration,
            dispatcher.submitted / duration,
            _percentile(dispatcher.latencies, 95) if dispatcher.latencies else None,
            _percentile(dispatcher.latencies, 99) if dispatcher.latencies else None,
            scheduler.barriers_processed,
            dispatcher.ordering_violations,
            asdict(source),
            asdict(scheduler),
            passed,
        )


def _validate_configuration(
    *,
    events_per_segment: int,
    worker_count: int,
    pending_limit: int,
    source_limit: int,
    callback_delay: float,
    payload_sizes: tuple[int, ...],
) -> None:
    for name, value in (
        ("events_per_segment", events_per_segment),
        ("worker_count", worker_count),
        ("pending_limit", pending_limit),
        ("source_limit", source_limit),
    ):
        _require_positive_integer(name, value)
    _require_finite_number("callback_delay", callback_delay, allow_zero=True)
    if not payload_sizes:
        raise ValueError("payload_sizes must not be empty")
    for size in payload_sizes:
        _require_positive_integer("payload size", size)


def _require_finite_number(name: str, value: float, *, allow_zero: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
        or (value == 0 and not allow_zero)
    ):
        raise ValueError(
            f"{name} must be a {'non-negative' if allow_zero else 'positive'} finite number"
        )


def measure_mixed_load(
    scheduler_kind: str,
    *,
    batches: int = 10,
    events_per_segment: int = 64,
    callback_delay: float = 0.0001,
    worker_count: int = 4,
    pending_limit: int = 64,
    source_limit: int = 256,
    payload_sizes: tuple[int, ...] = (64, 4096, 65536),
) -> WorkloadMeasurement:
    _require_positive_integer("batches", batches)
    _validate_configuration(
        events_per_segment=events_per_segment,
        worker_count=worker_count,
        pending_limit=pending_limit,
        source_limit=source_limit,
        callback_delay=callback_delay,
        payload_sizes=payload_sizes,
    )
    workload = _Workload(
        scheduler_kind,
        callback_delay=callback_delay,
        worker_count=worker_count,
        pending_limit=pending_limit,
        source_limit=source_limit,
        record_latency=True,
    )
    try:
        workload.start()
        started = perf_counter()
        for _ in range(batches):
            workload.batch(events_per_segment, payload_sizes)
        return workload.measurement(perf_counter() - started)
    finally:
        workload.stop()


def _memory_sample(workload: _Workload, started: float) -> MemorySample:
    gc.collect()
    retained, peak = tracemalloc.get_traced_memory()
    rss = None
    if sys.platform == "linux":
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        rss = resident_pages * os.sysconf("SC_PAGE_SIZE")
    return MemorySample(
        elapsed_seconds=perf_counter() - started,
        acknowledged_events=workload.source.metrics().acknowledged,
        traced_retained_bytes=retained,
        traced_peak_bytes=peak,
        rss_bytes=rss,
        source_retained_bytes=workload.source.metrics().current_bytes,
        scheduler_pending_bytes=workload.scheduler.metrics().current_pending_bytes,
    )


def measure_memory_soak(
    scheduler_kind: str,
    *,
    duration_seconds: float = 120,
    sample_interval: float = 5,
    events_per_segment: int = 64,
    worker_count: int = 4,
    pending_limit: int = 64,
    source_limit: int = 256,
    payload_sizes: tuple[int, ...] = (64, 4096, 65536),
) -> MemorySoakMeasurement:
    _require_finite_number("duration_seconds", duration_seconds)
    _require_finite_number("sample_interval", sample_interval)
    _validate_configuration(
        events_per_segment=events_per_segment,
        worker_count=worker_count,
        pending_limit=pending_limit,
        source_limit=source_limit,
        callback_delay=0,
        payload_sizes=payload_sizes,
    )
    workload = _Workload(
        scheduler_kind,
        callback_delay=0,
        worker_count=worker_count,
        pending_limit=pending_limit,
        source_limit=source_limit,
        record_latency=False,
    )
    tracemalloc.start()
    try:
        workload.start()
        run_started = perf_counter()
        workload.batch(events_per_segment, payload_sizes)
        started = perf_counter()
        samples = [_memory_sample(workload, started)]
        next_sample = started + sample_interval
        while perf_counter() - started < duration_seconds:
            workload.batch(events_per_segment, payload_sizes)
            if perf_counter() >= next_sample:
                samples.append(_memory_sample(workload, started))
                next_sample = perf_counter() + sample_interval
        samples.append(_memory_sample(workload, started))
        measurement = workload.measurement(perf_counter() - run_started)
        first, last = samples[0], samples[-1]
        return MemorySoakMeasurement(
            workload=measurement,
            warmup_events=first.acknowledged_events,
            traced_growth_bytes=last.traced_retained_bytes - first.traced_retained_bytes,
            rss_growth_bytes=last.rss_bytes - first.rss_bytes
            if last.rss_bytes is not None and first.rss_bytes is not None
            else None,
            samples=tuple(samples),
        )
    finally:
        try:
            workload.stop()
        finally:
            tracemalloc.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mixed", "soak"), default="mixed")
    parser.add_argument("--batches", type=int, default=10)
    parser.add_argument("--events-per-segment", type=int, default=64)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pending-limit", type=int, default=64)
    parser.add_argument("--source-limit", type=int, default=256)
    parser.add_argument("--soak-seconds", type=float, default=120)
    parser.add_argument("--sample-interval", type=float, default=5)
    args = parser.parse_args()
    configuration = {
        "events_per_segment": args.events_per_segment,
        "worker_count": args.workers,
        "pending_limit": args.pending_limit,
        "source_limit": args.source_limit,
        "payload_sizes": (64, 4096, 65536),
    }
    try:
        if args.mode == "mixed":
            mixed_measurements = [
                measure_mixed_load(
                    kind, batches=args.batches, callback_delay=delay, **configuration
                )
                for delay in (0, 0.0001)
                for kind in ("sequential", "keyed_serial")
            ]
            passed = all(value.passed for value in mixed_measurements)
            measurements = [asdict(value) for value in mixed_measurements]
        else:
            soak_measurements = [
                measure_memory_soak(
                    kind,
                    duration_seconds=args.soak_seconds,
                    sample_interval=args.sample_interval,
                    **configuration,
                )
                for kind in ("sequential", "keyed_serial")
            ]
            passed = all(value.workload.passed for value in soak_measurements)
            measurements = [asdict(value) for value in soak_measurements]
    except ValueError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                },
                "configuration": {**vars(args), **configuration},
                "measurements": measurements,
                "invariants_passed": passed,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not passed:
        print("runtime workload ordering, completion or queue bounds failed", file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

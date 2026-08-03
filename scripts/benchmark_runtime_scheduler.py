"""Measure sequential/keyed runtime scheduling and prefetch coalescing impact.

Run from a source checkout with ``PYTHONPATH=src``. All scenarios use typed
events and in-process fakes, so results exclude WebSocket and device variance.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from threading import Event, Lock
from time import perf_counter, sleep

from mirabox_sdk import (
    Controller,
    Coordinates,
    DialRotateEvent,
    KeyDownEvent,
    StreamDockEvent,
)
from mirabox_sdk._next.messaging.inbound import InboundEventQueue
from mirabox_sdk._next.runtime.keyed_scheduler import KeyedSerialHandlerScheduler
from mirabox_sdk._next.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._next.runtime.pumps import RuntimeEventPump
from mirabox_sdk._next.runtime.scheduler import SequentialHandlerScheduler


@dataclass(frozen=True, slots=True)
class SchedulerBenchmarkMeasurement:
    scenario: str
    scheduler: str
    event_count: int
    duration_seconds: float
    throughput_per_second: float
    callback_start_p50_ms: float
    callback_start_p95_ms: float
    callback_start_p99_ms: float
    peak_pending: float
    peak_active_callbacks: float
    admission_backpressure: float


@dataclass(frozen=True, slots=True)
class CoalescingMeasurement:
    pending_limit: int
    submitted_rotations: int
    coalesced_rotations: int
    coalescing_ratio: float
    dispatched_events: int
    peak_boundary_depth: int
    peak_scheduler_pending: int
    scheduler_backpressure: int


@dataclass(frozen=True, slots=True)
class SchedulerPerformanceBudget:
    """Accepted keyed throughput relative to the same-run sequential baseline."""

    min_throughput_ratio: float
    max_callback_start_p95_ms: float
    reason: str


@dataclass(frozen=True, slots=True)
class SchedulerPerformanceComparison:
    """One scenario result evaluated against its same-run performance budget."""

    scenario: str
    keyed_to_sequential_throughput_ratio: float
    min_throughput_ratio: float
    callback_start_p95_ms: float
    max_callback_start_p95_ms: float
    within_budget: bool
    violations: tuple[str, ...]
    budget_reason: str


@dataclass(frozen=True, slots=True)
class CoalescingPerformanceBudget:
    """Minimum retained boundary-coalescing opportunity for one prefetch limit."""

    min_coalescing_ratio: float
    reason: str


@dataclass(frozen=True, slots=True)
class CoalescingPerformanceComparison:
    """One coalescing measurement evaluated against its explicit guardrail."""

    pending_limit: int
    coalescing_ratio: float
    min_coalescing_ratio: float
    within_budget: bool
    violations: tuple[str, ...]
    budget_reason: str


SCHEDULER_PERFORMANCE_BUDGETS: dict[str, SchedulerPerformanceBudget] = {
    "single_context": SchedulerPerformanceBudget(
        0.55,
        30.0,
        "keyed scheduling preserves serial context order and pays worker handoff overhead",
    ),
    "contexts_4": SchedulerPerformanceBudget(
        1.75,
        15.0,
        "four workers should overlap callbacks across four independent contexts",
    ),
    "contexts_16": SchedulerPerformanceBudget(
        1.75,
        10.0,
        "bounded prefetch should keep four workers supplied across sixteen contexts",
    ),
    "contexts_64": SchedulerPerformanceBudget(
        1.75,
        10.0,
        "bounded prefetch should keep four workers supplied across sixty-four contexts",
    ),
}

COALESCING_PERFORMANCE_BUDGETS: dict[int, CoalescingPerformanceBudget] = {
    1: CoalescingPerformanceBudget(
        0.99,
        "minimal scheduler prefetch leaves nearly the entire rotation burst at the boundary",
    ),
    4: CoalescingPerformanceBudget(
        0.98,
        "small prefetch trades a few retained rotations for worker availability",
    ),
    16: CoalescingPerformanceBudget(
        0.95,
        "moderate prefetch may retain one bounded window of individual rotations",
    ),
    64: CoalescingPerformanceBudget(
        0.85,
        "the largest measured prefetch remains bounded and preserves most burst coalescing",
    ),
}


class _LatencyDispatcher:
    def __init__(self, *, callback_delay: float) -> None:
        self._callback_delay = callback_delay
        self._lock = Lock()
        self._submitted_at: dict[int, float] = {}
        self._latencies: list[float] = []

    def mark_submitted(self, event: StreamDockEvent) -> None:
        with self._lock:
            self._submitted_at[id(event)] = perf_counter()

    def dispatch(self, event: StreamDockEvent) -> DispatchResult:
        started = perf_counter()
        with self._lock:
            submitted = self._submitted_at.pop(id(event))
            self._latencies.append(started - submitted)
        if self._callback_delay:
            sleep(self._callback_delay)
        return DispatchResult(DispatchOutcome.HANDLED)

    def latencies(self) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._latencies)


class _CoalescingDispatcher:
    def __init__(self) -> None:
        self.blocker_started = Event()
        self.release_blocker = Event()
        self._lock = Lock()
        self._dispatched = 0

    def dispatch(self, event: StreamDockEvent) -> DispatchResult:
        if getattr(event, "context", None) == "blocker":
            self.blocker_started.set()
            if not self.release_blocker.wait(5):
                raise TimeoutError("coalescing benchmark blocker was not released")
        with self._lock:
            self._dispatched += 1
        return DispatchResult(DispatchOutcome.HANDLED)

    def dispatched(self) -> int:
        with self._lock:
            return self._dispatched


def benchmark_scheduler_matrix(
    *,
    event_count: int,
    repeats: int,
    callback_delay: float,
    worker_count: int,
    pending_limit: int,
) -> tuple[SchedulerBenchmarkMeasurement, ...]:
    """Return median measurements for sequential and keyed context matrices."""

    _require_positive_integer("event_count", event_count)
    _require_positive_integer("repeats", repeats)
    _require_non_negative_number("callback_delay", callback_delay)
    _require_positive_integer("worker_count", worker_count)
    _require_positive_integer("pending_limit", pending_limit)

    measurements: list[SchedulerBenchmarkMeasurement] = []
    for scenario, context_count in (
        ("single_context", 1),
        ("contexts_4", 4),
        ("contexts_16", 16),
        ("contexts_64", 64),
    ):
        for scheduler_kind in ("sequential", "keyed_serial"):
            samples = tuple(
                _measure_scheduler(
                    scenario=scenario,
                    scheduler_kind=scheduler_kind,
                    event_count=event_count,
                    context_count=context_count,
                    callback_delay=callback_delay,
                    worker_count=worker_count,
                    pending_limit=pending_limit,
                )
                for _ in range(repeats)
            )
            measurements.append(_median_scheduler_measurement(samples))
    return tuple(measurements)


def measure_prefetch_coalescing(
    *,
    pending_limit: int,
    rotation_count: int,
) -> CoalescingMeasurement:
    """Measure how many same-context rotations remain in the boundary queue.

    One callback is held active while exactly ``pending_limit`` rotations are
    prefetched into the scheduler and one more blocks on admission. The fixed
    remaining burst can then coalesce only while it stays at the boundary.
    """

    _require_positive_integer("pending_limit", pending_limit)
    _require_positive_integer("rotation_count", rotation_count)
    if rotation_count <= pending_limit + 1:
        raise ValueError("rotation_count must be greater than pending_limit + 1")

    source = InboundEventQueue(
        rotation_count + 1,
        coalesce_dial_rotations=True,
    )
    dispatcher = _CoalescingDispatcher()
    scheduler = KeyedSerialHandlerScheduler(
        dispatcher,
        worker_count=1,
        pending_limit=pending_limit,
    )
    pump = RuntimeEventPump(source, scheduler, poll_interval=0.001)
    scheduler.start()
    pump.start()
    try:
        source.submit(_key_down("blocker"))
        if not dispatcher.blocker_started.wait(2):
            raise TimeoutError("coalescing benchmark callback did not start")

        for index in range(pending_limit):
            source.submit(_dial_rotate("dial", index + 1))
            _wait_until(
                lambda index=index: scheduler.metrics().current_pending >= index + 1,
                message="scheduler pending limit was not reached",
            )

        source.submit(_dial_rotate("dial", pending_limit + 1))
        expected_in_flight = pending_limit + 2
        _wait_until(
            lambda: source.metrics().in_flight >= expected_in_flight,
            message="event pump did not block on scheduler admission",
        )

        remaining = rotation_count - pending_limit - 1
        for index in range(remaining):
            source.submit(_dial_rotate("dial", pending_limit + index + 2))
        source.stop_accepting()
        dispatcher.release_blocker.set()

        if not pump.drain(timeout=5):
            raise TimeoutError("coalescing benchmark event pump did not drain")
        if not scheduler.drain(timeout=5):
            raise TimeoutError("coalescing benchmark scheduler did not drain")

        boundary_metrics = source.metrics()
        scheduler_metrics = scheduler.metrics()
        return CoalescingMeasurement(
            pending_limit=pending_limit,
            submitted_rotations=rotation_count,
            coalesced_rotations=boundary_metrics.coalesced,
            coalescing_ratio=boundary_metrics.coalesced / rotation_count,
            dispatched_events=dispatcher.dispatched(),
            peak_boundary_depth=boundary_metrics.peak_depth,
            peak_scheduler_pending=scheduler_metrics.peak_pending,
            scheduler_backpressure=scheduler_metrics.admission_backpressure,
        )
    finally:
        source.stop_accepting()
        dispatcher.release_blocker.set()
        pump.stop(timeout=1)
        scheduler.stop(timeout=1)


def _measure_scheduler(
    *,
    scenario: str,
    scheduler_kind: str,
    event_count: int,
    context_count: int,
    callback_delay: float,
    worker_count: int,
    pending_limit: int,
) -> SchedulerBenchmarkMeasurement:
    dispatcher = _LatencyDispatcher(callback_delay=callback_delay)
    if scheduler_kind == "sequential":
        scheduler = SequentialHandlerScheduler(dispatcher)
    else:
        scheduler = KeyedSerialHandlerScheduler(
            dispatcher,
            worker_count=worker_count,
            pending_limit=pending_limit,
        )
    events = tuple(_key_down(f"context-{index % context_count}") for index in range(event_count))
    scheduler.start()
    started = perf_counter()
    try:
        completions = []
        for event in events:
            dispatcher.mark_submitted(event)
            completions.append(scheduler.submit(event))
        if not scheduler.drain(timeout=60):
            raise TimeoutError(f"{scheduler_kind} scheduler benchmark did not drain")
        for completion in completions:
            completion.result(0)
        duration = perf_counter() - started
        latencies_ms = tuple(value * 1000 for value in dispatcher.latencies())
        metrics = scheduler.metrics()
        return SchedulerBenchmarkMeasurement(
            scenario=scenario,
            scheduler=scheduler_kind,
            event_count=event_count,
            duration_seconds=duration,
            throughput_per_second=event_count / duration,
            callback_start_p50_ms=_percentile(latencies_ms, 50),
            callback_start_p95_ms=_percentile(latencies_ms, 95),
            callback_start_p99_ms=_percentile(latencies_ms, 99),
            peak_pending=float(metrics.peak_pending),
            peak_active_callbacks=float(metrics.peak_active_callbacks),
            admission_backpressure=float(metrics.admission_backpressure),
        )
    finally:
        scheduler.stop(timeout=5)


def _median_scheduler_measurement(
    samples: Sequence[SchedulerBenchmarkMeasurement],
) -> SchedulerBenchmarkMeasurement:
    first = samples[0]
    return SchedulerBenchmarkMeasurement(
        scenario=first.scenario,
        scheduler=first.scheduler,
        event_count=first.event_count,
        duration_seconds=statistics.median(sample.duration_seconds for sample in samples),
        throughput_per_second=statistics.median(sample.throughput_per_second for sample in samples),
        callback_start_p50_ms=statistics.median(sample.callback_start_p50_ms for sample in samples),
        callback_start_p95_ms=statistics.median(sample.callback_start_p95_ms for sample in samples),
        callback_start_p99_ms=statistics.median(sample.callback_start_p99_ms for sample in samples),
        peak_pending=statistics.median(sample.peak_pending for sample in samples),
        peak_active_callbacks=statistics.median(sample.peak_active_callbacks for sample in samples),
        admission_backpressure=statistics.median(
            sample.admission_backpressure for sample in samples
        ),
    )


def evaluate_scheduler_performance(
    measurements: Sequence[SchedulerBenchmarkMeasurement],
    *,
    pending_limit: int,
) -> tuple[SchedulerPerformanceComparison, ...]:
    """Compare keyed throughput to the same-run sequential reference."""

    _require_positive_integer("pending_limit", pending_limit)
    indexed = {(value.scenario, value.scheduler): value for value in measurements}
    comparisons: list[SchedulerPerformanceComparison] = []
    for scenario, budget in SCHEDULER_PERFORMANCE_BUDGETS.items():
        try:
            sequential = indexed[(scenario, "sequential")]
            keyed = indexed[(scenario, "keyed_serial")]
        except KeyError as exc:
            raise ValueError(f"missing scheduler measurement for {exc.args[0]!r}") from None

        throughput_ratio = _ratio(
            keyed.throughput_per_second,
            sequential.throughput_per_second,
        )
        violations = []
        if throughput_ratio < budget.min_throughput_ratio:
            violations.append(
                f"throughput ratio {throughput_ratio:.3f} is below "
                f"{budget.min_throughput_ratio:.3f}"
            )
        if keyed.callback_start_p95_ms > budget.max_callback_start_p95_ms:
            violations.append(
                f"callback-start p95 {keyed.callback_start_p95_ms:.3f}ms exceeds "
                f"{budget.max_callback_start_p95_ms:.3f}ms"
            )
        if keyed.peak_pending > pending_limit:
            violations.append(
                f"peak pending {keyed.peak_pending:.0f} exceeds configured limit {pending_limit}"
            )
        comparisons.append(
            SchedulerPerformanceComparison(
                scenario=scenario,
                keyed_to_sequential_throughput_ratio=throughput_ratio,
                min_throughput_ratio=budget.min_throughput_ratio,
                callback_start_p95_ms=keyed.callback_start_p95_ms,
                max_callback_start_p95_ms=budget.max_callback_start_p95_ms,
                within_budget=not violations,
                violations=tuple(violations),
                budget_reason=budget.reason,
            )
        )
    return tuple(comparisons)


def evaluate_coalescing_performance(
    measurements: Sequence[CoalescingMeasurement],
) -> tuple[CoalescingPerformanceComparison, ...]:
    """Evaluate the canonical pending-limit matrix against coalescing budgets."""

    indexed = {value.pending_limit: value for value in measurements}
    comparisons: list[CoalescingPerformanceComparison] = []
    for pending_limit, budget in COALESCING_PERFORMANCE_BUDGETS.items():
        try:
            measurement = indexed[pending_limit]
        except KeyError:
            raise ValueError(
                f"missing coalescing measurement for pending limit {pending_limit}"
            ) from None
        violations = []
        if measurement.coalescing_ratio < budget.min_coalescing_ratio:
            violations.append(
                f"coalescing ratio {measurement.coalescing_ratio:.3f} is below "
                f"{budget.min_coalescing_ratio:.3f}"
            )
        if measurement.peak_scheduler_pending > pending_limit:
            violations.append(
                "peak scheduler pending "
                f"{measurement.peak_scheduler_pending} exceeds configured limit {pending_limit}"
            )
        comparisons.append(
            CoalescingPerformanceComparison(
                pending_limit=pending_limit,
                coalescing_ratio=measurement.coalescing_ratio,
                min_coalescing_ratio=budget.min_coalescing_ratio,
                within_budget=not violations,
                violations=tuple(violations),
                budget_reason=budget.reason,
            )
        )
    return tuple(comparisons)


def _ratio(candidate: float, baseline: float) -> float:
    if baseline == 0:
        return 1.0 if candidate == 0 else float("inf")
    return candidate / baseline


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _key_down(context: str) -> KeyDownEvent:
    return KeyDownEvent(
        action="benchmark.action",
        context=context,
        device="benchmark.device",
        settings={},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def _dial_rotate(context: str, ticks: int) -> DialRotateEvent:
    return DialRotateEvent(
        action="benchmark.action",
        context=context,
        device="benchmark.device",
        settings={},
        coordinates=Coordinates(0, 0),
        ticks=ticks,
        pressed=False,
    )


def _wait_until(
    predicate,
    *,
    message: str,
    timeout: float = 2,
) -> None:
    deadline = perf_counter() + timeout
    while not predicate():
        if perf_counter() >= deadline:
            raise TimeoutError(message)
        sleep(0.001)


def _require_positive_integer(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_non_negative_number(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")


def _parse_pending_limits(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item) for item in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "pending limits must be comma-separated integers"
        ) from None
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("pending limits must be positive integers")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--callback-delay", type=float, default=0.0001)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pending-limit", type=int, default=64)
    parser.add_argument("--coalescing-events", type=int, default=512)
    parser.add_argument(
        "--coalescing-pending-limits",
        type=_parse_pending_limits,
        default=(1, 4, 16, 64),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit unsuccessfully when a scheduler or coalescing budget is exceeded",
    )
    arguments = parser.parse_args()
    if arguments.check and set(arguments.coalescing_pending_limits) != set(
        COALESCING_PERFORMANCE_BUDGETS
    ):
        parser.error("--check requires the canonical coalescing pending-limit matrix")

    scheduler_measurements = benchmark_scheduler_matrix(
        event_count=arguments.events,
        repeats=arguments.repeats,
        callback_delay=arguments.callback_delay,
        worker_count=arguments.workers,
        pending_limit=arguments.pending_limit,
    )
    coalescing_measurements = tuple(
        measure_prefetch_coalescing(
            pending_limit=pending_limit,
            rotation_count=arguments.coalescing_events,
        )
        for pending_limit in arguments.coalescing_pending_limits
    )
    scheduler_comparisons = evaluate_scheduler_performance(
        scheduler_measurements,
        pending_limit=arguments.pending_limit,
    )
    coalescing_comparisons = (
        evaluate_coalescing_performance(coalescing_measurements)
        if set(arguments.coalescing_pending_limits) == set(COALESCING_PERFORMANCE_BUDGETS)
        else ()
    )
    violations = tuple(
        violation
        for comparison in (*scheduler_comparisons, *coalescing_comparisons)
        for violation in comparison.violations
    )

    if arguments.json:
        print(
            json.dumps(
                {
                    "environment": {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                    },
                    "scheduler": [asdict(value) for value in scheduler_measurements],
                    "coalescing": [asdict(value) for value in coalescing_measurements],
                    "performance_gate": {
                        "passed": not violations,
                        "scheduler": [asdict(value) for value in scheduler_comparisons],
                        "coalescing": [asdict(value) for value in coalescing_comparisons],
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("Scheduler throughput and callback-start latency")
        for value in scheduler_measurements:
            print(
                f"{value.scenario:14} {value.scheduler:12} "
                f"throughput={value.throughput_per_second:10.1f}/s "
                f"p50={value.callback_start_p50_ms:8.3f}ms "
                f"p95={value.callback_start_p95_ms:8.3f}ms "
                f"p99={value.callback_start_p99_ms:8.3f}ms "
                f"peak_pending={value.peak_pending:.0f}"
            )
        print("\nBoundary coalescing under scheduler prefetch")
        for value in coalescing_measurements:
            print(
                f"pending_limit={value.pending_limit:4} "
                f"coalesced={value.coalesced_rotations:4}/{value.submitted_rotations} "
                f"ratio={value.coalescing_ratio:.3f} "
                f"peak_scheduler_pending={value.peak_scheduler_pending}"
            )
        print("\nPerformance gate")
        for value in scheduler_comparisons:
            print(
                f"{value.scenario:14} throughput_ratio="
                f"{value.keyed_to_sequential_throughput_ratio:.3f} "
                f"minimum={value.min_throughput_ratio:.3f} "
                f"p95={value.callback_start_p95_ms:.3f}ms "
                f"p95_max={value.max_callback_start_p95_ms:.3f}ms "
                f"passed={value.within_budget}"
            )
        for value in coalescing_comparisons:
            print(
                f"pending_limit={value.pending_limit:4} "
                f"coalescing_ratio={value.coalescing_ratio:.3f} "
                f"minimum={value.min_coalescing_ratio:.3f} "
                f"passed={value.within_budget}"
            )
    if arguments.check and violations:
        print("runtime scheduler performance budgets exceeded:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

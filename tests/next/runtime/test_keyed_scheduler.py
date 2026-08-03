from __future__ import annotations

import unittest
from collections import defaultdict
from collections.abc import Callable
from threading import Event, Lock, Thread

from mirabox_sdk import StreamDockEvent, SystemDidWakeUpEvent, UnknownStreamDockEvent
from mirabox_sdk._next.runtime.keyed_scheduler import KeyedSerialHandlerScheduler
from mirabox_sdk._next.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._next.runtime.ports import DispatchCompletion, HandlerScheduler
from mirabox_sdk._next.runtime.scheduler import HandlerSchedulerLifecycleError

from .fakes import (
    FakeRuntimeEventDispatcher,
    key_down_event,
    will_appear_event,
    will_disappear_event,
)


def _scheduler(
    dispatch: Callable[[StreamDockEvent], DispatchResult],
    *,
    worker_count: int = 2,
    pending_limit: int = 8,
) -> KeyedSerialHandlerScheduler:
    return KeyedSerialHandlerScheduler(
        FakeRuntimeEventDispatcher(dispatch),
        worker_count=worker_count,
        pending_limit=pending_limit,
    )


class KeyedSerialHandlerSchedulerTests(unittest.TestCase):
    def test_validates_dependencies_and_bounded_capacity(self) -> None:
        with self.assertRaisesRegex(TypeError, "RuntimeEventDispatcher"):
            KeyedSerialHandlerScheduler(  # type: ignore[arg-type]
                lambda _event: DispatchResult(DispatchOutcome.HANDLED),
                worker_count=2,
                pending_limit=8,
            )

        dispatcher = FakeRuntimeEventDispatcher(
            lambda _event: DispatchResult(DispatchOutcome.HANDLED)
        )
        for field_name, values in (
            ("worker_count", (0, -1, True, 1.5)),
            ("pending_limit", (0, -1, True, 1.5)),
        ):
            for invalid in values:
                kwargs = {"worker_count": 2, "pending_limit": 8, field_name: invalid}
                with (
                    self.subTest(field_name=field_name, invalid=invalid),
                    self.assertRaisesRegex(
                        ValueError,
                        f"^{field_name} must be a positive integer$",
                    ),
                ):
                    KeyedSerialHandlerScheduler(dispatcher, **kwargs)  # type: ignore[arg-type]

    def test_same_context_is_strictly_serial(self) -> None:
        first_started = Event()
        second_started = Event()
        release_first = Event()
        first_event = key_down_event(context="button")
        second_event = key_down_event(context="button")

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if event is first_event:
                first_started.set()
                self.assertTrue(release_first.wait(1))
            else:
                second_started.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        self.addCleanup(release_first.set)
        self.addCleanup(lambda: scheduler.stop(timeout=1))
        scheduler.start()

        first = scheduler.submit(first_event)
        self.assertTrue(first_started.wait(1))
        second = scheduler.submit(second_event)

        self.assertFalse(second_started.wait(0.05))
        release_first.set()
        self.assertIs(first.result(1).outcome, DispatchOutcome.HANDLED)
        self.assertIs(second.result(1).outcome, DispatchOutcome.HANDLED)
        self.assertTrue(scheduler.drain(timeout=1))
        self.assertEqual(scheduler.metrics().peak_active_callbacks, 1)

    def test_different_contexts_can_overlap(self) -> None:
        slow_started = Event()
        fast_finished = Event()
        release_slow = Event()

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if getattr(event, "context", None) == "slow":
                slow_started.set()
                self.assertTrue(release_slow.wait(1))
            else:
                fast_finished.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        self.addCleanup(release_slow.set)
        self.addCleanup(lambda: scheduler.stop(timeout=1))
        scheduler.start()

        slow = scheduler.submit(key_down_event(context="slow"))
        self.assertTrue(slow_started.wait(1))
        fast = scheduler.submit(key_down_event(context="fast"))

        self.assertTrue(fast_finished.wait(1))
        self.assertIs(fast.result(0).outcome, DispatchOutcome.HANDLED)
        self.assertFalse(slow.done())
        self.assertGreaterEqual(scheduler.metrics().peak_active_callbacks, 2)
        release_slow.set()
        self.assertIs(slow.result(1).outcome, DispatchOutcome.HANDLED)

    def test_callback_failure_is_terminal_and_does_not_stop_other_contexts(self) -> None:
        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if getattr(event, "context", None) == "failing":
                return DispatchResult(
                    DispatchOutcome.CALLBACK_FAILED,
                    RuntimeError("application callback failed"),
                )
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        self.addCleanup(lambda: scheduler.stop(timeout=1))
        scheduler.start()

        failing = scheduler.submit(key_down_event(context="failing"))
        healthy = scheduler.submit(key_down_event(context="healthy"))

        self.assertIs(failing.result(1).outcome, DispatchOutcome.CALLBACK_FAILED)
        self.assertIs(healthy.result(1).outcome, DispatchOutcome.HANDLED)
        self.assertTrue(scheduler.drain(timeout=1))
        self.assertEqual(scheduler.metrics().callback_failures, 1)

    def test_lifecycle_broadcast_and_unknown_events_are_global_barriers(self) -> None:
        barriers = (
            will_appear_event(context="lifecycle"),
            will_disappear_event(context="lifecycle"),
            SystemDidWakeUpEvent(),
            UnknownStreamDockEvent(event="futureEvent", data={"event": "futureEvent"}),
        )
        for barrier in barriers:
            with self.subTest(event_name=barrier.event_name):
                self._assert_global_barrier(barrier)

    def test_pending_limit_blocks_admission_and_shutdown_unblocks_submitter(self) -> None:
        active_started = Event()
        release_active = Event()
        blocked_submit_finished = Event()
        blocked_completion: list[DispatchCompletion] = []

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if getattr(event, "context", None) == "active":
                active_started.set()
                self.assertTrue(release_active.wait(1))
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch, worker_count=1, pending_limit=1)
        self.addCleanup(release_active.set)
        self.addCleanup(lambda: scheduler.stop(timeout=1))
        scheduler.start()
        active = scheduler.submit(key_down_event(context="active"))
        self.assertTrue(active_started.wait(1))
        pending = scheduler.submit(key_down_event(context="pending"))

        def submit_blocked() -> None:
            blocked_completion.append(scheduler.submit(key_down_event(context="blocked")))
            blocked_submit_finished.set()

        submitter = Thread(target=submit_blocked)
        submitter.start()
        self.addCleanup(lambda: submitter.join(1))
        self.assertTrue(_wait_until(lambda: scheduler.metrics().admission_backpressure == 1))
        self.assertFalse(blocked_submit_finished.is_set())
        self.assertEqual(scheduler.metrics().current_pending, 1)
        self.assertEqual(scheduler.metrics().peak_pending, 1)

        scheduler.stop_accepting()

        self.assertTrue(blocked_submit_finished.wait(1))
        self.assertIs(
            blocked_completion[0].result(0).outcome,
            DispatchOutcome.DISCARDED_DURING_SHUTDOWN,
        )
        release_active.set()
        self.assertIs(active.result(1).outcome, DispatchOutcome.HANDLED)
        self.assertIs(pending.result(1).outcome, DispatchOutcome.HANDLED)
        self.assertTrue(scheduler.stop(timeout=1))
        metrics = scheduler.metrics()
        self.assertEqual(metrics.accepted, 2)
        self.assertEqual(metrics.completed, 2)
        self.assertEqual(metrics.discarded_during_shutdown, 1)

    def test_timeout_diagnostics_count_active_once_and_discard_stranded_pending(self) -> None:
        active_started = Event()
        release_active = Event()

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if getattr(event, "context", None) == "active":
                active_started.set()
                self.assertTrue(release_active.wait(1))
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch, worker_count=1, pending_limit=1)
        self.addCleanup(release_active.set)
        self.addCleanup(lambda: scheduler.stop(timeout=1))
        scheduler.start()
        active = scheduler.submit(key_down_event(context="active"))
        self.assertTrue(active_started.wait(1))
        pending = scheduler.submit(key_down_event(context="pending"))

        with self.assertLogs(
            "mirabox_sdk._next.runtime.keyed_scheduler",
            level="WARNING",
        ) as logs:
            self.assertFalse(scheduler.drain(timeout=0))
            self.assertFalse(scheduler.drain(timeout=0))
            self.assertFalse(scheduler.stop(timeout=0))

        self.assertEqual(scheduler.metrics().callback_timeouts, 1)
        self.assertEqual(
            sum("event_name=keyDown context=active" in line for line in logs.output),
            1,
        )
        self.assertIs(
            pending.result(0).outcome,
            DispatchOutcome.DISCARDED_DURING_SHUTDOWN,
        )
        release_active.set()
        self.assertIs(active.result(1).outcome, DispatchOutcome.HANDLED)
        self.assertTrue(scheduler.stop(timeout=1))
        metrics = scheduler.metrics()
        self.assertEqual(metrics.accepted, 2)
        self.assertEqual(metrics.completed, 2)
        self.assertEqual(metrics.discarded_during_shutdown, 1)

    def test_concurrency_stress_preserves_every_context_sequence(self) -> None:
        context_count = 8
        events_per_context = 40
        active_contexts: set[str] = set()
        observed: dict[str, list[int]] = defaultdict(list)
        violations: list[str] = []
        event_metadata: dict[int, tuple[str, int]] = {}
        submitted_events: list[StreamDockEvent] = []
        lock = Lock()
        pause = Event()

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            context, sequence = event_metadata[id(event)]
            with lock:
                if context in active_contexts:
                    violations.append(context)
                active_contexts.add(context)
            pause.wait(0.0001)
            with lock:
                observed[context].append(sequence)
                active_contexts.remove(context)
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch, worker_count=4, pending_limit=16)
        self.addCleanup(lambda: scheduler.stop(timeout=1))
        scheduler.start()
        completions: list[DispatchCompletion] = []
        for sequence in range(events_per_context):
            for context_index in range(context_count):
                context = f"button-{context_index}"
                event = key_down_event(context=context)
                submitted_events.append(event)
                event_metadata[id(event)] = (context, sequence)
                completions.append(scheduler.submit(event))

        self.assertTrue(scheduler.drain(timeout=5))
        self.assertTrue(all(completion.done() for completion in completions))
        self.assertEqual(violations, [])
        for context_index in range(context_count):
            self.assertEqual(
                observed[f"button-{context_index}"],
                list(range(events_per_context)),
            )
        metrics = scheduler.metrics()
        self.assertEqual(metrics.accepted, context_count * events_per_context)
        self.assertEqual(metrics.completed, context_count * events_per_context)
        self.assertLessEqual(metrics.peak_pending, 16)
        self.assertGreater(metrics.admission_backpressure, 0)

    def test_submit_before_start_and_restart_after_stop_are_rejected(self) -> None:
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        self.assertIsInstance(scheduler, HandlerScheduler)
        with self.assertRaisesRegex(HandlerSchedulerLifecycleError, "not been started"):
            scheduler.submit(key_down_event())

        self.assertTrue(scheduler.stop(timeout=0))
        with self.assertRaisesRegex(HandlerSchedulerLifecycleError, "already been stopped"):
            scheduler.start()

    def _assert_global_barrier(self, barrier: StreamDockEvent) -> None:
        before_started = {"before-a": Event(), "before-b": Event()}
        release_before = Event()
        barrier_started = Event()
        release_barrier = Event()
        after_started = Event()
        history: list[str] = []
        lock = Lock()

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            context = getattr(event, "context", None)
            if context in before_started:
                with lock:
                    history.append(context)
                before_started[context].set()
                self.assertTrue(release_before.wait(1))
            elif event is barrier:
                with lock:
                    history.append("barrier")
                barrier_started.set()
                self.assertTrue(release_barrier.wait(1))
            else:
                with lock:
                    history.append("after")
                after_started.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch, worker_count=3, pending_limit=4)
        scheduler.start()
        try:
            before = tuple(
                scheduler.submit(key_down_event(context=context)) for context in before_started
            )
            self.assertTrue(all(started.wait(1) for started in before_started.values()))
            barrier_completion = scheduler.submit(barrier)
            after = scheduler.submit(key_down_event(context="after"))

            self.assertFalse(barrier_started.wait(0.05))
            self.assertFalse(after_started.is_set())
            release_before.set()
            self.assertTrue(barrier_started.wait(1))
            self.assertTrue(all(completion.done() for completion in before))
            self.assertFalse(after_started.is_set())
            release_barrier.set()

            self.assertIs(barrier_completion.result(1).outcome, DispatchOutcome.HANDLED)
            self.assertIs(after.result(1).outcome, DispatchOutcome.HANDLED)
            self.assertEqual(history[-2:], ["barrier", "after"])
            self.assertEqual(scheduler.metrics().barriers_processed, 1)
        finally:
            release_before.set()
            release_barrier.set()
            scheduler.stop(timeout=1)


def _wait_until(predicate: Callable[[], bool], *, attempts: int = 200) -> bool:
    wait = Event()
    for _ in range(attempts):
        if predicate():
            return True
        wait.wait(0.005)
    return predicate()


if __name__ == "__main__":
    unittest.main()

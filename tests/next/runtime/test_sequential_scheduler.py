from __future__ import annotations

import unittest
from collections.abc import Callable
from threading import Event, Thread

from mirabox_sdk import StreamDockEvent, SystemDidWakeUpEvent
from mirabox_sdk._next.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._next.runtime.ports import DispatchCompletion, HandlerScheduler
from mirabox_sdk._next.runtime.scheduler import (
    HandlerSchedulerLifecycleError,
    SequentialHandlerScheduler,
)

from .fakes import FakeRuntimeEventDispatcher, key_down_event


def _scheduler(
    dispatch: Callable[[StreamDockEvent], DispatchResult],
) -> SequentialHandlerScheduler:
    return SequentialHandlerScheduler(FakeRuntimeEventDispatcher(dispatch))


class SequentialHandlerSchedulerTests(unittest.TestCase):
    def test_requires_runtime_event_dispatcher_port(self) -> None:
        with self.assertRaisesRegex(TypeError, "RuntimeEventDispatcher"):
            SequentialHandlerScheduler(  # type: ignore[arg-type]
                lambda _event: DispatchResult(DispatchOutcome.HANDLED)
            )

    def test_dispatches_fifo_without_a_pending_queue(self) -> None:
        dispatched: list[StreamDockEvent] = []

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            dispatched.append(event)
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        self.assertIsInstance(scheduler, HandlerScheduler)
        scheduler.start()
        events = (key_down_event(context="one"), key_down_event(context="two"))

        completions = [scheduler.submit(event) for event in events]

        self.assertEqual(dispatched, list(events))
        self.assertTrue(all(isinstance(value, DispatchCompletion) for value in completions))
        self.assertTrue(all(value.done() for value in completions))
        self.assertTrue(
            all(value.result().outcome is DispatchOutcome.HANDLED for value in completions)
        )
        metrics = scheduler.metrics()
        self.assertEqual(metrics.accepted, 2)
        self.assertEqual(metrics.completed, 2)
        self.assertEqual(metrics.current_pending, 0)
        self.assertEqual(metrics.peak_pending, 0)
        self.assertEqual(metrics.peak_active_callbacks, 1)

    def test_callback_failure_is_terminal_and_does_not_stop_later_work(self) -> None:
        calls = 0

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return DispatchResult(
                    DispatchOutcome.CALLBACK_FAILED,
                    RuntimeError("application callback failed"),
                )
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        scheduler.start()

        first = scheduler.submit(key_down_event())
        second = scheduler.submit(key_down_event())

        self.assertIs(first.result().outcome, DispatchOutcome.CALLBACK_FAILED)
        self.assertIs(second.result().outcome, DispatchOutcome.HANDLED)
        self.assertEqual(scheduler.metrics().callback_failures, 1)

    def test_completion_callback_observes_one_terminal_result(self) -> None:
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        scheduler.start()
        completion = scheduler.submit(key_down_event())
        observed: list[DispatchResult] = []

        completion.add_done_callback(lambda value: observed.append(value.result()))

        self.assertEqual(observed, [DispatchResult(DispatchOutcome.HANDLED)])

    def test_stop_accepting_returns_explicit_discarded_completion(self) -> None:
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        scheduler.start()
        scheduler.stop_accepting()

        completion = scheduler.submit(key_down_event())

        self.assertIs(
            completion.result().outcome,
            DispatchOutcome.DISCARDED_DURING_SHUTDOWN,
        )
        metrics = scheduler.metrics()
        self.assertEqual(metrics.accepted, 0)
        self.assertEqual(metrics.completed, 0)
        self.assertEqual(metrics.discarded_during_shutdown, 1)

    def test_stop_from_active_callback_is_non_blocking(self) -> None:
        returned = Event()
        scheduler: SequentialHandlerScheduler

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            self.assertTrue(scheduler.is_dispatch_thread())
            self.assertTrue(scheduler.stop(timeout=0))
            returned.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        scheduler.start()

        completion = scheduler.submit(key_down_event())

        self.assertTrue(returned.is_set())
        self.assertIs(completion.result().outcome, DispatchOutcome.HANDLED)
        self.assertTrue(scheduler.drain(timeout=0))

    def test_metrics_expose_active_callback_without_pending_work(self) -> None:
        started = Event()
        release = Event()

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            started.set()
            self.assertTrue(release.wait(1))
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        scheduler.start()
        worker = Thread(target=lambda: scheduler.submit(key_down_event()))
        worker.start()
        self.assertTrue(started.wait(1))

        metrics = scheduler.metrics()
        self.assertEqual(metrics.current_active_callbacks, 1)
        self.assertEqual(metrics.active_contexts, 1)
        self.assertEqual(metrics.current_pending, 0)
        self.assertFalse(scheduler.drain(timeout=0))

        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_concurrent_submitters_apply_direct_backpressure(self) -> None:
        first_started = Event()
        release_first = Event()
        second_finished = Event()

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if getattr(event, "context", None) == "first":
                first_started.set()
                self.assertTrue(release_first.wait(1))
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        scheduler.start()
        first = Thread(target=lambda: scheduler.submit(key_down_event(context="first")))
        second = Thread(
            target=lambda: (
                scheduler.submit(key_down_event(context="second")),
                second_finished.set(),
            )
        )

        first.start()
        self.assertTrue(first_started.wait(1))
        second.start()
        self.assertFalse(second_finished.wait(0.05))
        release_first.set()
        first.join(1)
        second.join(1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_finished.is_set())
        self.assertEqual(scheduler.metrics().admission_backpressure, 1)
        self.assertEqual(scheduler.metrics().peak_pending, 0)

    def test_active_callback_timeout_is_counted_once_per_callback(self) -> None:
        started = Event()
        release = Event()

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            started.set()
            self.assertTrue(release.wait(1))
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        scheduler.start()
        worker = Thread(target=lambda: scheduler.submit(key_down_event()))
        worker.start()
        self.assertTrue(started.wait(1))

        with self.assertLogs("mirabox_sdk._next.runtime.scheduler", level="WARNING") as logs:
            self.assertFalse(scheduler.drain(timeout=0))
            self.assertFalse(scheduler.drain(timeout=0))
        self.assertEqual(scheduler.metrics().callback_timeouts, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("event_name=keyDown", logs.output[0])
        self.assertIn("context=button", logs.output[0])

        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_barrier_metrics_follow_runtime_route_ordering(self) -> None:
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        scheduler.start()

        scheduler.submit(SystemDidWakeUpEvent())
        scheduler.submit(key_down_event())

        self.assertEqual(scheduler.metrics().barriers_processed, 1)

    def test_submit_before_start_and_restart_after_stop_are_rejected(self) -> None:
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        with self.assertRaisesRegex(HandlerSchedulerLifecycleError, "not been started"):
            scheduler.submit(key_down_event())

        self.assertTrue(scheduler.stop(timeout=0))
        with self.assertRaisesRegex(HandlerSchedulerLifecycleError, "already been stopped"):
            scheduler.start()


if __name__ == "__main__":
    unittest.main()

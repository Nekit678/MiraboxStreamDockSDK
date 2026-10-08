from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import replace
from threading import Event
from time import monotonic

from mirabox_sdk import StreamDockEvent, SystemDidWakeUpEvent, UnknownStreamDockEvent
from mirabox_sdk._internal.messaging.inbound import InboundEventQueue
from mirabox_sdk._internal.runtime.keyed_scheduler import KeyedSerialHandlerScheduler
from mirabox_sdk._internal.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._internal.runtime.ports import RuntimeEventPumpWorker
from mirabox_sdk._internal.runtime.pumps import RuntimeEventPump
from mirabox_sdk._internal.runtime.scheduler import SequentialHandlerScheduler

from .fakes import (
    FakeInboundEventSource,
    FakeRuntimeEventDispatcher,
    key_down_event,
    will_appear_event,
    will_disappear_event,
)


def _scheduler(
    dispatch: Callable[[StreamDockEvent], DispatchResult],
) -> SequentialHandlerScheduler:
    return SequentialHandlerScheduler(FakeRuntimeEventDispatcher(dispatch))


class RuntimeEventPumpTests(unittest.TestCase):
    def test_selective_admission_preserves_lifecycle_broadcast_and_unknown_barriers(self) -> None:
        for barrier in (
            will_appear_event(),
            will_disappear_event(),
            SystemDidWakeUpEvent(),
            UnknownStreamDockEvent(event="futureEvent", data={"event": "futureEvent"}),
        ):
            with self.subTest(barrier=barrier.event_name):
                self._assert_selective_barrier(barrier)

    def _assert_selective_barrier(self, barrier: StreamDockEvent) -> None:
        hot_started, release_hot = Event(), Event()
        barrier_started, release_barrier = Event(), Event()
        before_finished, after_finished = Event(), Event()
        hot_events = [
            replace(key_down_event(context="hot"), settings={"sequence": index})
            for index in range(6)
        ]
        before = key_down_event(context="cold")
        after = key_down_event(context="cold")
        history: list[StreamDockEvent] = []

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if event is hot_events[0]:
                hot_started.set()
                release_hot.wait()
            if event is barrier:
                barrier_started.set()
                release_barrier.wait()
            history.append(event)
            if event is before:
                before_finished.set()
            if event is after:
                after_finished.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        source = InboundEventQueue(16)
        scheduler = KeyedSerialHandlerScheduler(
            FakeRuntimeEventDispatcher(dispatch), worker_count=4, pending_limit=4
        )
        pump = RuntimeEventPump(source, scheduler)
        scheduler.start()
        pump.start()
        try:
            source.submit(hot_events[0])
            self.assertTrue(hot_started.wait(1))
            for event in (*hot_events[1:], before, barrier, after):
                self.assertTrue(source.submit(event, timeout=0))
            source.stop_accepting()
            self.assertTrue(before_finished.wait(1))
            self.assertFalse(barrier_started.wait(0.02))
            self.assertFalse(after_finished.is_set())
            release_hot.set()
            self.assertTrue(barrier_started.wait(1))
            self.assertEqual(history, [before, *hot_events])
            self.assertFalse(after_finished.is_set())
            release_barrier.set()
            self.assertTrue(pump.drain(timeout=2))
            self.assertEqual(history, [before, *hot_events, barrier, after])
            self.assertEqual(scheduler.metrics().barriers_processed, 1)
            self.assertEqual(source.metrics().acknowledged, len(history))
        finally:
            source.stop_accepting()
            release_hot.set()
            release_barrier.set()
            scheduler.stop(timeout=1)
            pump.stop(timeout=1)

    def test_capacity_wakeup_resumes_a_context_without_waiting_for_poll_timeout(self) -> None:
        first_started, release_first, second_finished = Event(), Event(), Event()
        first, second = key_down_event(), key_down_event()

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if event is first:
                first_started.set()
                release_first.wait()
            else:
                second_finished.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        source = InboundEventQueue(2)
        scheduler = KeyedSerialHandlerScheduler(
            FakeRuntimeEventDispatcher(dispatch), worker_count=4, pending_limit=1
        )
        pump = RuntimeEventPump(source, scheduler, poll_interval=5)
        scheduler.start()
        pump.start()
        try:
            source.submit(first)
            self.assertTrue(first_started.wait(1))
            source.submit(second)
            deadline = monotonic() + 1
            while not scheduler.metrics().admission_backpressure:
                self.assertLess(monotonic(), deadline)
                Event().wait(0.001)
            release_first.set()
            self.assertTrue(second_finished.wait(1))
            source.stop_accepting()
            self.assertTrue(pump.drain(timeout=1))
        finally:
            source.stop_accepting()
            release_first.set()
            scheduler.stop(timeout=1)
            pump.stop(timeout=1)

    def test_scheduler_shutdown_wakes_and_acknowledges_deferred_source_events(self) -> None:
        started, release = Event(), Event()

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            started.set()
            release.wait()
            return DispatchResult(DispatchOutcome.HANDLED)

        source = InboundEventQueue(2)
        scheduler = KeyedSerialHandlerScheduler(
            FakeRuntimeEventDispatcher(dispatch), worker_count=4, pending_limit=1
        )
        pump = RuntimeEventPump(source, scheduler, poll_interval=5)
        scheduler.start()
        pump.start()
        try:
            source.submit(key_down_event())
            self.assertTrue(started.wait(1))
            source.submit(key_down_event())
            source.stop_accepting()
            scheduler.stop_accepting()
            deadline = monotonic() + 1
            while not pump.metrics().discarded_during_shutdown:
                self.assertLess(monotonic(), deadline)
                Event().wait(0.001)
            self.assertEqual(source.metrics().acknowledged, 1)
            release.set()
            self.assertTrue(pump.drain(timeout=1))
            self.assertEqual(source.metrics().acknowledged, 2)
        finally:
            release.set()
            source.stop_accepting()
            scheduler.stop(timeout=1)
            pump.stop(timeout=1)

    def test_hot_context_does_not_block_admission_of_an_independent_context(self) -> None:
        for pending_limit in (1, 4, 64):
            with self.subTest(pending_limit=pending_limit):
                self._assert_hot_context_admission(pending_limit)

    def _assert_hot_context_admission(self, pending_limit: int) -> None:
        hot_started, release_hot, cold_finished = Event(), Event(), Event()
        hot_events = [
            replace(key_down_event(context="hot"), settings={"sequence": index})
            for index in range(pending_limit + 2)
        ]
        cold_event = key_down_event(context="cold")
        observed: list[StreamDockEvent] = []

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            if event is hot_events[0]:
                hot_started.set()
                release_hot.wait()
            observed.append(event)
            if event is cold_event:
                cold_finished.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        source = InboundEventQueue(len(hot_events) + 1)
        scheduler = KeyedSerialHandlerScheduler(
            FakeRuntimeEventDispatcher(dispatch), worker_count=4, pending_limit=pending_limit
        )
        pump = RuntimeEventPump(source, scheduler)
        scheduler.start()
        pump.start()
        try:
            self.assertTrue(source.submit(hot_events[0]))
            self.assertTrue(hot_started.wait(1))
            for event in (*hot_events[1:], cold_event):
                self.assertTrue(source.submit(event, timeout=0))
            source.stop_accepting()

            self.assertTrue(cold_finished.wait(0.2), "cold context is starved by hot burst")
            self.assertEqual(observed, [cold_event])
            self.assertGreater(source.metrics().current_depth, 0)
            self.assertLessEqual(scheduler.metrics().peak_pending, pending_limit)
            self.assertLessEqual(source.metrics().peak_depth, len(hot_events) + 1)
            self.assertLessEqual(pump.metrics().peak_owned, pending_limit + 4 + 1)
            release_hot.set()
            self.assertTrue(pump.drain(timeout=2))
            self.assertEqual(observed[1:], hot_events)
            self.assertEqual(source.metrics().acknowledged, len(hot_events) + 1)
            self.assertEqual(pump.metrics().current_owned, 0)
        finally:
            source.stop_accepting()
            release_hot.set()
            scheduler.stop(timeout=1)
            pump.stop(timeout=1)

    def test_preserves_fifo_and_acknowledges_every_terminal_result_once(self) -> None:
        events = tuple(key_down_event(context=f"button-{index}") for index in range(3))
        source = FakeInboundEventSource(events)
        source.close()
        dispatched: list[StreamDockEvent] = []
        scheduler = _scheduler(
            lambda event: (
                dispatched.append(event),
                DispatchResult(DispatchOutcome.HANDLED),
            )[1]
        )
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)
        self.assertIsInstance(pump, RuntimeEventPumpWorker)
        scheduler.start()

        pump.start()

        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(dispatched, list(events))
        self.assertEqual(source.received, list(events))
        self.assertEqual(source.acknowledged, list(events))
        metrics = pump.metrics()
        self.assertEqual(metrics.events_received, 3)
        self.assertEqual(metrics.submitted_to_scheduler, 3)
        self.assertEqual(metrics.events_acknowledged, 3)
        self.assertEqual(metrics.acknowledgement_failures, 0)
        self.assertEqual(metrics.source_closed, 1)
        self.assertEqual(metrics.current_owned, 0)
        self.assertEqual(metrics.peak_owned, 1)

    def test_callback_failure_is_acknowledged_and_later_event_continues(self) -> None:
        events = (key_down_event(context="failing"), key_down_event(context="healthy"))
        source = FakeInboundEventSource(events)
        source.close()
        dispatched: list[str] = []

        def dispatch(event: StreamDockEvent) -> DispatchResult:
            context = getattr(event, "context", "")
            dispatched.append(context)
            if context == "failing":
                return DispatchResult(DispatchOutcome.CALLBACK_FAILED, RuntimeError("failed"))
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)
        scheduler.start()
        pump.start()

        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(dispatched, ["failing", "healthy"])
        self.assertEqual(source.acknowledged, list(events))
        self.assertEqual(scheduler.metrics().callback_failures, 1)
        self.assertIsNone(pump.failure)

    def test_scheduler_shutdown_discards_and_acknowledges_received_event(self) -> None:
        event = key_down_event()
        source = FakeInboundEventSource((event,))
        source.close()
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        scheduler.start()
        scheduler.stop_accepting()
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)

        pump.start()

        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(source.acknowledged, [event])
        self.assertEqual(pump.metrics().discarded_during_shutdown, 1)
        self.assertEqual(scheduler.metrics().discarded_during_shutdown, 1)

    def test_command_send_inside_callback_finishes_before_acknowledgement(self) -> None:
        history: list[str] = []

        class RecordingSource(FakeInboundEventSource):
            def task_done(self) -> None:
                history.append("acknowledge")
                super().task_done()

        class Sender:
            def send(self) -> None:
                history.append("command")

        event = key_down_event()
        source = RecordingSource((event,))
        source.close()
        sender = Sender()

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            history.append("callback-start")
            sender.send()
            history.append("callback-end")
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)
        scheduler.start()
        pump.start()

        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(
            history,
            ["callback-start", "command", "callback-end", "acknowledge"],
        )

    def test_callback_triggered_stop_is_non_blocking_and_does_not_leak_thread(self) -> None:
        source = FakeInboundEventSource((key_down_event(),))
        close_returned = Event()
        pump: RuntimeEventPump

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            self.assertTrue(pump.is_worker_thread())
            self.assertTrue(pump.stop(timeout=0))
            close_returned.set()
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)
        scheduler.start()
        pump.start()

        self.assertTrue(close_returned.wait(1))
        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(len(source.acknowledged), 1)
        self.assertTrue(pump.stop(timeout=0))

    def test_external_stop_timeout_does_not_acknowledge_before_callback_returns(self) -> None:
        event = key_down_event()
        source = FakeInboundEventSource((event,))
        callback_started = Event()
        release_callback = Event()

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            callback_started.set()
            self.assertTrue(release_callback.wait(1))
            return DispatchResult(DispatchOutcome.HANDLED)

        scheduler = _scheduler(dispatch)
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)
        scheduler.start()
        pump.start()
        self.assertTrue(callback_started.wait(1))

        self.assertFalse(pump.stop(timeout=0))
        self.assertEqual(source.acknowledged, [])
        self.assertEqual(pump.metrics().current_owned, 1)
        release_callback.set()

        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(source.acknowledged, [event])

    def test_source_timeouts_are_observable_until_non_blocking_stop(self) -> None:
        source = FakeInboundEventSource()
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        scheduler.start()
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.001)
        pump.start()

        deadline = monotonic() + 1
        poll = Event()
        while pump.metrics().source_poll_timeouts == 0 and monotonic() < deadline:
            poll.wait(0.01)
        self.assertGreaterEqual(pump.metrics().source_poll_timeouts, 1)
        self.assertTrue(pump.stop(timeout=1))
        self.assertTrue(pump.drain(timeout=0))

    def test_scheduler_invariant_failure_is_acknowledged_and_reported_fatal(self) -> None:
        event = key_down_event()
        source = FakeInboundEventSource((event,))
        observed: list[Exception] = []

        def dispatch(_event: StreamDockEvent) -> DispatchResult:
            raise RuntimeError("runtime invariant failed")

        scheduler = _scheduler(dispatch)
        pump = RuntimeEventPump(
            source,
            scheduler,
            poll_interval=0.005,
            on_fatal_error=observed.append,
        )
        scheduler.start()
        pump.start()

        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(source.acknowledged, [event])
        self.assertIsInstance(pump.failure, RuntimeError)
        self.assertEqual(observed, [pump.failure])

    def test_acknowledgement_failure_is_terminal_and_counted_once(self) -> None:
        class FailingAcknowledgementSource(FakeInboundEventSource):
            def task_done(self) -> None:
                raise RuntimeError("ack failed")

        source = FailingAcknowledgementSource((key_down_event(),))
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)
        scheduler.start()

        with self.assertLogs("mirabox_sdk._internal.runtime.pumps", level="ERROR") as logs:
            pump.start()
            self.assertTrue(pump.drain(timeout=1))

        self.assertEqual(pump.metrics().acknowledgement_failures, 1)
        self.assertEqual(pump.metrics().events_acknowledged, 0)
        self.assertIsInstance(pump.failure, RuntimeError)
        self.assertNotIn("ack failed", "\n".join(logs.output))

    def test_invalid_source_value_is_acknowledged_before_fatal_stop(self) -> None:
        source = FakeInboundEventSource((object(),))
        scheduler = _scheduler(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.005)
        scheduler.start()
        pump.start()

        self.assertTrue(pump.drain(timeout=1))
        self.assertEqual(len(source.acknowledged), 1)
        self.assertIsInstance(pump.failure, TypeError)
        self.assertEqual(pump.metrics().events_acknowledged, 1)


if __name__ == "__main__":
    unittest.main()

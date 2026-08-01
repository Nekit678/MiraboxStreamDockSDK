from __future__ import annotations

import unittest
from collections.abc import Callable
from threading import Event
from time import monotonic

from mirabox_sdk import StreamDockEvent
from mirabox_sdk._next.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._next.runtime.ports import RuntimeEventPumpWorker
from mirabox_sdk._next.runtime.pumps import RuntimeEventPump
from mirabox_sdk._next.runtime.scheduler import SequentialHandlerScheduler

from .fakes import FakeInboundEventSource, FakeRuntimeEventDispatcher, key_down_event


def _scheduler(
    dispatch: Callable[[StreamDockEvent], DispatchResult],
) -> SequentialHandlerScheduler:
    return SequentialHandlerScheduler(FakeRuntimeEventDispatcher(dispatch))


class RuntimeEventPumpTests(unittest.TestCase):
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

        with self.assertLogs("mirabox_sdk._next.runtime.pumps", level="ERROR") as logs:
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

from __future__ import annotations

import unittest
from threading import Event, Thread

from mirabox_sdk import (
    Controller,
    Coordinates,
    DialRotateEvent,
    KeyDownEvent,
    StreamDockEvent,
    SystemDidWakeUpEvent,
    WillAppearEvent,
)
from mirabox_sdk._next.messaging.inbound import (
    InboundEventQueue,
    InboundEventQueueClosedError,
    InboundOverflowPolicy,
)
from mirabox_sdk._next.messaging.ports import InboundEventSink, InboundEventSource


def dial(context: str, ticks: int = 1) -> DialRotateEvent:
    return DialRotateEvent(
        action="action-uuid",
        context=context,
        device="device-uuid",
        settings={},
        coordinates=Coordinates(column=0, row=0),
        ticks=ticks,
        pressed=False,
    )


def key_down(context: str) -> KeyDownEvent:
    return KeyDownEvent(
        action="action-uuid",
        context=context,
        device="device-uuid",
        settings={},
        coordinates=Coordinates(column=0, row=0),
        is_in_multi_action=False,
    )


def will_appear(context: str) -> WillAppearEvent:
    return WillAppearEvent(
        action="action-uuid",
        context=context,
        device="device-uuid",
        settings={},
        coordinates=Coordinates(column=0, row=0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


class InboundEventQueueTests(unittest.TestCase):
    def test_rejects_invalid_configuration(self) -> None:
        for invalid_limit in (0, -1, True, 1.5):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                InboundEventQueue(invalid_limit)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "InboundOverflowPolicy"):
            InboundEventQueue(1, overflow_policy="drop_newest")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            InboundEventQueue(1, coalesce_dial_rotations=1)  # type: ignore[arg-type]

    def test_implementation_explicitly_inherits_typed_ports(self) -> None:
        self.assertIn(InboundEventSource, InboundEventQueue.__mro__)
        self.assertIn(InboundEventSink, InboundEventQueue.__mro__)

    def test_preserves_fifo_for_lossless_events(self) -> None:
        queue = InboundEventQueue(3)
        events: list[StreamDockEvent] = [
            key_down("first"),
            will_appear("second"),
            SystemDidWakeUpEvent(),
        ]
        for event in events:
            self.assertTrue(queue.submit(event))

        self.assertEqual([queue.receive() for _ in events], events)
        for _ in events:
            queue.task_done()
        metrics = queue.metrics()
        self.assertEqual(metrics.enqueued, 3)
        self.assertEqual(metrics.dequeued, 3)
        self.assertEqual(metrics.in_flight, 0)
        self.assertEqual(metrics.acknowledged, 3)
        self.assertEqual(metrics.dropped, 0)

    def test_drain_waits_until_received_event_processing_is_acknowledged(self) -> None:
        queue = InboundEventQueue(1)
        self.assertTrue(queue.submit(key_down("button")))
        self.assertEqual(queue.receive().context, "button")  # type: ignore[attr-defined]

        drained: list[bool] = []
        waiter = Thread(target=lambda: drained.append(queue.drain(timeout=1)))
        waiter.start()
        self.assertTrue(waiter.is_alive())
        self.assertEqual(queue.metrics().in_flight, 1)

        queue.task_done()
        waiter.join(1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(drained, [True])
        self.assertEqual(queue.metrics().acknowledged, 1)

    def test_task_done_rejects_missing_or_repeated_acknowledgement(self) -> None:
        queue = InboundEventQueue(1)
        with self.assertRaisesRegex(ValueError, "task_done.*too many"):
            queue.task_done()

        self.assertTrue(queue.submit(key_down("button")))
        queue.receive()
        queue.task_done()
        with self.assertRaisesRegex(ValueError, "task_done.*too many"):
            queue.task_done()

    def test_coalesces_compatible_rotations_per_context_in_place(self) -> None:
        queue = InboundEventQueue(3, coalesce_dial_rotations=True)

        self.assertTrue(queue.submit(dial("dial-a", 1)))
        self.assertTrue(queue.submit(dial("dial-b", 5)))
        self.assertTrue(queue.submit(dial("dial-a", 2)))

        first = queue.receive()
        second = queue.receive()
        self.assertIsInstance(first, DialRotateEvent)
        self.assertIsInstance(second, DialRotateEvent)
        self.assertEqual((first.context, first.ticks), ("dial-a", 3))
        self.assertEqual((second.context, second.ticks), ("dial-b", 5))
        self.assertEqual(queue.metrics().coalesced, 1)

    def test_action_and_broadcast_barriers_prevent_rotation_coalescing(self) -> None:
        barriers: tuple[StreamDockEvent, ...] = (
            key_down("dial-a"),
            will_appear("other"),
            SystemDidWakeUpEvent(),
        )

        for barrier in barriers:
            with self.subTest(barrier=barrier.event_name):
                queue = InboundEventQueue(3, coalesce_dial_rotations=True)
                self.assertTrue(queue.submit(dial("dial-a")))
                self.assertTrue(queue.submit(barrier))
                self.assertTrue(queue.submit(dial("dial-a")))

                self.assertEqual(
                    [queue.receive().event_name for _ in range(3)],
                    ["dialRotate", barrier.event_name, "dialRotate"],
                )
                self.assertEqual(queue.metrics().coalesced, 0)

    def test_overflow_never_displaces_lossless_events(self) -> None:
        drop_oldest = InboundEventQueue(
            2,
            overflow_policy=InboundOverflowPolicy.DROP_OLDEST,
            coalesce_dial_rotations=False,
        )
        lifecycle = will_appear("button")
        latest_rotation = dial("second")
        self.assertTrue(drop_oldest.submit(lifecycle))
        self.assertTrue(drop_oldest.submit(dial("first")))
        self.assertTrue(drop_oldest.submit(latest_rotation))
        self.assertIs(drop_oldest.receive(), lifecycle)
        self.assertIs(drop_oldest.receive(), latest_rotation)
        self.assertEqual(drop_oldest.metrics().dropped_oldest, 1)

        drop_newest = InboundEventQueue(
            2,
            overflow_policy=InboundOverflowPolicy.DROP_NEWEST,
            coalesce_dial_rotations=False,
        )
        oldest_rotation = dial("first")
        self.assertTrue(drop_newest.submit(oldest_rotation))
        self.assertTrue(drop_newest.submit(dial("second")))
        self.assertTrue(drop_newest.submit(lifecycle))
        self.assertIs(drop_newest.receive(), oldest_rotation)
        self.assertIs(drop_newest.receive(), lifecycle)
        self.assertEqual(drop_newest.metrics().dropped_newest, 1)

    def test_lossless_event_backpressures_and_can_time_out_explicitly(self) -> None:
        queue = InboundEventQueue(1)
        self.assertTrue(queue.submit(key_down("first")))
        producer_finished = Event()
        accepted: list[bool] = []

        def submit_second() -> None:
            accepted.append(queue.submit(key_down("second")))
            producer_finished.set()

        producer = Thread(target=submit_second)
        producer.start()
        self.assertFalse(producer_finished.wait(0.02))
        self.assertEqual(queue.receive().context, "first")  # type: ignore[attr-defined]
        self.assertTrue(producer_finished.wait(1))
        producer.join(1)
        self.assertEqual(accepted, [True])
        self.assertEqual(queue.receive().context, "second")  # type: ignore[attr-defined]

        self.assertTrue(queue.submit(key_down("third")))
        self.assertFalse(queue.submit(key_down("fourth"), timeout=0))
        metrics = queue.metrics()
        self.assertEqual(metrics.backpressured, 1)
        self.assertEqual(metrics.rejected_full, 1)
        self.assertEqual(metrics.dropped, 1)

    def test_shutdown_timeout_and_late_rejection_are_observable(self) -> None:
        queue = InboundEventQueue(2)
        self.assertTrue(queue.submit(key_down("first")))
        self.assertTrue(queue.submit(key_down("second")))

        self.assertFalse(queue.shutdown(timeout=0))
        self.assertFalse(queue.submit(key_down("late")))
        with self.assertRaises(InboundEventQueueClosedError):
            queue.receive()

        metrics = queue.metrics()
        self.assertEqual(metrics.discarded_during_shutdown, 2)
        self.assertEqual(metrics.rejected_after_shutdown, 1)
        self.assertEqual(metrics.dropped, 3)

    def test_concurrent_typed_producers_do_not_silently_drop_events(self) -> None:
        producer_count = 4
        events_per_producer = 20
        queue = InboundEventQueue(
            producer_count * events_per_producer,
            coalesce_dial_rotations=False,
        )

        producers = [
            Thread(
                target=lambda producer=index: [
                    queue.submit(key_down(f"{producer}:{sequence}"))
                    for sequence in range(events_per_producer)
                ]
            )
            for index in range(producer_count)
        ]
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join(1)
            self.assertFalse(producer.is_alive())

        contexts = [
            queue.receive().context  # type: ignore[attr-defined]
            for _ in range(producer_count * events_per_producer)
        ]
        self.assertEqual(len(set(contexts)), len(contexts))
        self.assertEqual(queue.metrics().dropped, 0)


if __name__ == "__main__":
    unittest.main()

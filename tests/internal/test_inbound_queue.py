from __future__ import annotations

import unittest
from collections import deque
from dataclasses import replace
from threading import Event, Thread
from unittest.mock import patch

from mirabox_sdk import (
    Controller,
    Coordinates,
    DialRotateEvent,
    KeyDownEvent,
    StreamDockEvent,
    SystemDidWakeUpEvent,
    WillAppearEvent,
)
from mirabox_sdk._internal.messaging.inbound import (
    InboundEventQueue,
    InboundEventQueueClosedError,
    InboundOverflowPolicy,
    _QueuedEvent,
)
from mirabox_sdk._internal.messaging.ports import InboundEventSink, InboundEventSource
from mirabox_sdk._internal.transport.buffer_limits import retained_size


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


class _IndexCostDeque(deque[_QueuedEvent]):
    """Count traversal work for indexed reads without timing-dependent assertions."""

    index_access_cost = 0

    def __getitem__(self, index: int) -> _QueuedEvent:
        queued = super().__getitem__(index)
        if index < 0:
            index += len(self)
        self.index_access_cost += 1 + min(index, len(self) - index - 1)
        return queued


class InboundEventQueueTests(unittest.TestCase):
    def test_settings_byte_budget_rejects_before_item_limit_and_releases(self) -> None:
        event = replace(key_down("button"), settings={"large": "x" * 4096})
        size = retained_size(event, 100_000)
        queue = InboundEventQueue(1024, byte_limit=size)
        self.assertTrue(queue.submit(event))
        self.assertFalse(queue.submit(event, timeout=0))
        self.assertEqual(queue.metrics().current_bytes, size)
        self.assertEqual(queue.metrics().rejected_full, 1)
        self.assertIs(queue.receive_selected(lambda events: 0, timeout=0), event)
        self.assertEqual(queue.metrics().current_bytes, 0)
        queue.task_done()
        self.assertTrue(queue.submit(event))
        queue.shutdown(timeout=0)
        self.assertEqual(queue.metrics().current_bytes, 0)

    def test_impossible_event_is_rejected_without_waiting(self) -> None:
        queue = InboundEventQueue(1024, byte_limit=1)
        self.assertFalse(queue.submit(key_down("button")))
        self.assertEqual(queue.metrics().rejected_oversized, 1)
        self.assertEqual(queue.metrics().dropped, 1)

    def test_byte_pressure_drops_enough_rotations_to_admit_lossless_event(self) -> None:
        first, second = dial("first"), dial("second")
        budget = retained_size(first, 100_000) + retained_size(second, 100_000)
        empty = key_down("button")
        empty_size = retained_size(replace(empty, settings={"large": ""}), 100_000)
        event = replace(empty, settings={"large": "x" * (budget - empty_size)})
        self.assertLessEqual(retained_size(event, 100_000), budget)
        queue = InboundEventQueue(1024, byte_limit=budget)
        self.assertTrue(queue.submit(first))
        self.assertTrue(queue.submit(second))
        self.assertTrue(queue.submit(event, timeout=0))
        self.assertEqual(queue.metrics().dropped_newest, 2)
        self.assertIs(queue.receive(), event)
        queue.task_done()
        self.assertEqual(queue.metrics().current_bytes, 0)

    def test_coalescing_updates_byte_weight_and_preserves_budget(self) -> None:
        small = dial("button")
        large = replace(small, settings={"large": "x" * 4096})
        budget = retained_size(large, 100_000)
        queue = InboundEventQueue(1, byte_limit=budget, coalesce_dial_rotations=True)
        for event in (small, large, small):
            self.assertTrue(queue.submit(event))
            self.assertLessEqual(queue.metrics().current_bytes, budget)
        self.assertEqual(queue.metrics().coalesced, 2)
        self.assertEqual(queue.metrics().current_bytes, retained_size(small, 100_000))
        self.assertEqual(queue.metrics().peak_bytes, budget)
        self.assertEqual(queue.receive().ticks, 3)
        queue.task_done()
        self.assertEqual(queue.metrics().current_bytes, 0)

    def test_selection_leaves_deferred_events_queued_and_preserves_coalescing(self) -> None:
        queue = InboundEventQueue(3, coalesce_dial_rotations=True)
        cold = key_down("cold")
        queue.submit(dial("hot", 1))
        queue.submit(cold)
        self.assertIs(queue.receive_selected(lambda events: 1, timeout=0), cold)
        queue.task_done()
        queue.submit(dial("hot", 2))
        self.assertEqual(queue.metrics().coalesced, 1)
        queue.stop_accepting()
        with self.assertRaises(TimeoutError):
            queue.receive_selected(lambda events: None, timeout=0)
        self.assertEqual(queue.metrics().current_depth, 1)
        self.assertFalse(queue.drain(timeout=0))
        rotation = queue.receive(timeout=0)
        self.assertIsInstance(rotation, DialRotateEvent)
        self.assertEqual(rotation.ticks, 3)
        queue.task_done()
        self.assertTrue(queue.drain(timeout=0))
        with self.assertRaises(InboundEventQueueClosedError):
            queue.receive_selected(lambda events: None, timeout=0)
        self.assertEqual(queue.metrics().acknowledged, 2)

    def test_invalid_selection_never_dequeues_or_acknowledges_an_event(self) -> None:
        queue = InboundEventQueue(1)
        event = key_down("button")
        queue.submit(event)
        for invalid in (-1, 1, True, 0.5):
            with self.subTest(index=invalid), self.assertRaises(ValueError):
                queue.receive_selected(lambda events, index=invalid: index, timeout=0)
        self.assertEqual(queue.metrics().dequeued, 0)
        self.assertIs(queue.receive(timeout=0), event)

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

    def test_full_lossless_queue_overflow_scan_has_linear_index_access_cost(self) -> None:
        for policy in InboundOverflowPolicy:
            for capacity in (128, 512, 2048):
                with self.subTest(policy=policy, capacity=capacity):
                    measured = _IndexCostDeque()
                    with patch(
                        "mirabox_sdk._internal.messaging.inbound.deque", return_value=measured
                    ):
                        queue = InboundEventQueue(capacity, overflow_policy=policy)
                    events = [key_down(str(index)) for index in range(capacity)]
                    for event in events:
                        self.assertTrue(queue.submit(event, timeout=0))

                    self.assertFalse(queue.submit(key_down("overflow"), timeout=0))
                    self.assertLessEqual(measured.index_access_cost, capacity)
                    metrics = queue.metrics()
                    self.assertEqual(metrics.current_depth, capacity)
                    self.assertEqual(metrics.rejected_full, 1)
                    self.assertEqual((metrics.dropped_newest, metrics.dropped_oldest), (0, 0))
                    for event in events:
                        self.assertIs(queue.receive(timeout=0), event)
                        queue.task_done()
                    self.assertTrue(queue.drain(timeout=0))

    def test_overflow_removes_rotation_at_either_end_or_middle_and_preserves_order(self) -> None:
        for policy in InboundOverflowPolicy:
            for rotation_indexes in ((0,), (2,), (4,), (0, 2, 4)):
                with self.subTest(policy=policy, rotation_indexes=rotation_indexes):
                    queue = InboundEventQueue(5, overflow_policy=policy)
                    events: list[StreamDockEvent] = [
                        dial(str(index)) if index in rotation_indexes else key_down(str(index))
                        for index in range(5)
                    ]
                    for event in events:
                        self.assertTrue(queue.submit(event, timeout=0))
                    incoming = key_down("incoming")
                    self.assertTrue(queue.submit(incoming, timeout=0))

                    newest = policy is InboundOverflowPolicy.DROP_NEWEST
                    removed_index = max(rotation_indexes) if newest else min(rotation_indexes)
                    expected = events[:removed_index] + events[removed_index + 1 :] + [incoming]
                    self.assertEqual([queue.receive(timeout=0) for _ in expected], expected)
                    for _ in expected:
                        queue.task_done()
                    metrics = queue.metrics()
                    self.assertEqual(metrics.dropped_newest, int(newest))
                    self.assertEqual(metrics.dropped_oldest, int(not newest))
                    self.assertEqual(metrics.acknowledged, 5)
                    self.assertTrue(queue.drain(timeout=0))

    def test_evicted_rotation_cannot_coalesce_with_a_later_submission(self) -> None:
        for policy in InboundOverflowPolicy:
            with self.subTest(policy=policy):
                queue = InboundEventQueue(3, overflow_policy=policy, coalesce_dial_rotations=True)
                events = [key_down(str(index)) for index in range(3)]
                self.assertTrue(queue.submit(dial("dial", 1)))
                for event in events:
                    self.assertTrue(queue.submit(event, timeout=0))
                self.assertIs(queue.receive(timeout=0), events[0])
                queue.task_done()

                rotation = dial("dial", 2)
                self.assertTrue(queue.submit(rotation, timeout=0))
                expected = [*events[1:], rotation]
                self.assertEqual([queue.receive(timeout=0) for _ in expected], expected)
                for _ in expected:
                    queue.task_done()
                self.assertEqual(queue.metrics().coalesced, 0)
                self.assertTrue(queue.drain(timeout=0))

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

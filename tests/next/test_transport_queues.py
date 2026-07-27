from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from threading import Event, Thread

from mirabox_sdk._next.transport.frames import OutboundFrame, TransportReceipt
from mirabox_sdk._next.transport.ports import (
    RawInboundSink,
    RawInboundSource,
    RawOutboundSink,
    RawOutboundSource,
    SessionEventSink,
    SessionEventSource,
)
from mirabox_sdk._next.transport.queues import (
    RawInboundQueue,
    RawOutboundQueue,
    SessionEventQueue,
    TransportQueueClosedError,
    TransportQueueFullError,
)
from mirabox_sdk._next.transport.session import Connected, Disconnected


class TransportQueueTests(unittest.TestCase):
    def test_rejects_invalid_queue_limits_and_timeouts(self) -> None:
        for queue_type in (RawInboundQueue, RawOutboundQueue, SessionEventQueue):
            for invalid_limit in (0, -1, True, 1.5):
                with (
                    self.subTest(queue_type=queue_type.__name__, limit=invalid_limit),
                    self.assertRaisesRegex(ValueError, "positive integer"),
                ):
                    queue_type(invalid_limit)  # type: ignore[arg-type]

        queue = RawInboundQueue(1)
        for invalid_timeout in (-1, True, float("inf"), float("nan"), "1"):
            with (
                self.subTest(timeout=invalid_timeout),
                self.assertRaisesRegex(ValueError, "non-negative number or None"),
            ):
                queue.receive(timeout=invalid_timeout)  # type: ignore[arg-type]

    def test_implementations_explicitly_inherit_transport_ports(self) -> None:
        contracts_and_implementations = (
            (RawInboundSource, RawInboundQueue),
            (RawInboundSink, RawInboundQueue),
            (RawOutboundSource, RawOutboundQueue),
            (RawOutboundSink, RawOutboundQueue),
            (SessionEventSource, SessionEventQueue),
            (SessionEventSink, SessionEventQueue),
        )

        for contract, implementation in contracts_and_implementations:
            with self.subTest(contract=contract.__name__):
                self.assertIn(contract, implementation.__mro__)

    def test_raw_queues_preserve_fifo_and_return_immutable_atomic_metrics(self) -> None:
        inbound = RawInboundQueue(3)
        for frame in ("first", "second", "third"):
            self.assertTrue(inbound.submit(frame))

        self.assertEqual([inbound.receive() for _ in range(3)], ["first", "second", "third"])
        metrics = inbound.metrics()
        self.assertEqual(metrics.current_depth, 0)
        self.assertEqual(metrics.peak_depth, 3)
        self.assertEqual(metrics.submitted, 3)
        self.assertEqual(metrics.enqueued, 3)
        self.assertEqual(metrics.dequeued, 3)
        with self.assertRaises(FrozenInstanceError):
            metrics.current_depth = 1  # type: ignore[misc]

        outbound = RawOutboundQueue(2)
        frames = [
            OutboundFrame("first", TransportReceipt()),
            OutboundFrame("second", TransportReceipt()),
        ]
        for frame in frames:
            self.assertTrue(outbound.submit(frame))
        self.assertIs(outbound.receive(), frames[0])
        self.assertIs(outbound.receive(), frames[1])

    def test_full_raw_queue_backpressures_until_a_consumer_frees_capacity(self) -> None:
        queue = RawInboundQueue(1)
        self.assertTrue(queue.submit("first"))
        producer_started = Event()
        producer_finished = Event()
        accepted: list[bool] = []

        def submit_second() -> None:
            producer_started.set()
            accepted.append(queue.submit("second"))
            producer_finished.set()

        producer = Thread(target=submit_second)
        producer.start()
        self.assertTrue(producer_started.wait(1))
        self.assertFalse(producer_finished.wait(0.02))

        self.assertEqual(queue.receive(), "first")
        self.assertTrue(producer_finished.wait(1))
        producer.join(1)

        self.assertEqual(accepted, [True])
        self.assertEqual(queue.receive(), "second")
        self.assertEqual(queue.metrics().backpressured, 1)

    def test_capacity_timeout_is_an_explicit_observable_rejection(self) -> None:
        inbound = RawInboundQueue(1)
        self.assertTrue(inbound.submit("first"))
        self.assertFalse(inbound.submit("second", timeout=0))
        metrics = inbound.metrics()
        self.assertEqual(metrics.rejected_full, 1)
        self.assertEqual(metrics.rejected, 1)

        outbound = RawOutboundQueue(1)
        accepted = OutboundFrame("first", TransportReceipt())
        rejected = OutboundFrame("second", TransportReceipt())
        self.assertTrue(outbound.submit(accepted))
        self.assertFalse(outbound.submit(rejected, timeout=0))
        with self.assertRaises(TransportQueueFullError):
            rejected.receipt.result(timeout=0)
        self.assertFalse(accepted.receipt.done())

    def test_shutdown_wakes_blocked_producer_and_rejects_late_items(self) -> None:
        queue = RawInboundQueue(1)
        self.assertTrue(queue.submit("first"))
        producer_finished = Event()
        accepted: list[bool] = []

        def submit_second() -> None:
            accepted.append(queue.submit("second"))
            producer_finished.set()

        producer = Thread(target=submit_second)
        producer.start()
        self.assertFalse(producer_finished.wait(0.02))

        self.assertFalse(queue.shutdown(timeout=0))
        self.assertTrue(producer_finished.wait(1))
        producer.join(1)

        self.assertEqual(accepted, [False])
        self.assertFalse(queue.submit("late"))
        with self.assertRaises(TransportQueueClosedError):
            queue.receive()
        metrics = queue.metrics()
        self.assertEqual(metrics.discarded_during_shutdown, 1)
        self.assertEqual(metrics.rejected_after_shutdown, 2)

    def test_raw_outbound_shutdown_fails_every_discarded_receipt(self) -> None:
        queue = RawOutboundQueue(2)
        frames = [
            OutboundFrame("first", TransportReceipt()),
            OutboundFrame("second", TransportReceipt()),
        ]
        for frame in frames:
            self.assertTrue(queue.submit(frame))

        self.assertFalse(queue.shutdown(timeout=0))

        for frame in frames:
            with self.assertRaisesRegex(
                TransportQueueClosedError,
                "discarded during shutdown",
            ):
                frame.receipt.result(timeout=0)
        self.assertEqual(queue.metrics().discarded_during_shutdown, 2)

    def test_raw_outbound_rejects_repeated_receipt_without_poisoning_shutdown(self) -> None:
        queue = RawOutboundQueue(2)
        frame = OutboundFrame("payload", TransportReceipt())

        self.assertTrue(queue.submit(frame))
        with self.assertRaisesRegex(ValueError, "already owned"):
            queue.submit(frame)

        self.assertFalse(queue.shutdown(timeout=0))
        with self.assertRaises(TransportQueueClosedError):
            frame.receipt.result(timeout=0)

    def test_invalid_raw_outbound_timeout_does_not_claim_receipt(self) -> None:
        queue = RawOutboundQueue(1)
        frame = OutboundFrame("payload", TransportReceipt())

        with self.assertRaisesRegex(ValueError, "non-negative number or None"):
            queue.submit(frame, timeout=-1)

        self.assertTrue(queue.submit(frame))

    def test_shutdown_drains_successfully_when_a_consumer_is_active(self) -> None:
        queue = RawInboundQueue(2)
        self.assertTrue(queue.submit("first"))
        self.assertTrue(queue.submit("second"))
        received: list[str] = []

        consumer = Thread(target=lambda: received.extend((queue.receive(), queue.receive())))
        consumer.start()
        self.assertTrue(queue.shutdown(timeout=1))
        consumer.join(1)

        self.assertEqual(received, ["first", "second"])
        self.assertEqual(queue.metrics().discarded_during_shutdown, 0)
        with self.assertRaises(TransportQueueClosedError):
            queue.receive()

    def test_session_events_are_lossless_and_ordered(self) -> None:
        queue = SessionEventQueue(2)
        connected = Connected()
        disconnected = Disconnected(status_code=1000, reason="normal")

        self.assertTrue(queue.submit(connected))
        self.assertTrue(queue.submit(disconnected))
        self.assertIs(queue.receive(), connected)
        self.assertIs(queue.receive(), disconnected)

    def test_concurrent_raw_producers_do_not_lose_or_duplicate_frames(self) -> None:
        queue = RawInboundQueue(8)
        producer_count = 4
        frames_per_producer = 25
        received: list[str] = []

        consumer = Thread(
            target=lambda: received.extend(
                queue.receive() for _ in range(producer_count * frames_per_producer)
            )
        )
        producers = [
            Thread(
                target=lambda producer=index: [
                    queue.submit(f"{producer}:{sequence}")
                    for sequence in range(frames_per_producer)
                ]
            )
            for index in range(producer_count)
        ]

        consumer.start()
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join(1)
            self.assertFalse(producer.is_alive())
        consumer.join(1)
        self.assertFalse(consumer.is_alive())

        self.assertEqual(len(received), producer_count * frames_per_producer)
        self.assertEqual(len(set(received)), len(received))
        for producer in range(producer_count):
            per_producer = [
                int(frame.partition(":")[2])
                for frame in received
                if frame.startswith(f"{producer}:")
            ]
            self.assertEqual(per_producer, list(range(frames_per_producer)))
        metrics = queue.metrics()
        self.assertEqual(metrics.enqueued, len(received))
        self.assertEqual(metrics.dequeued, len(received))
        self.assertEqual(metrics.rejected, 0)


if __name__ == "__main__":
    unittest.main()

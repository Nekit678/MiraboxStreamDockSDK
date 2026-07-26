from __future__ import annotations

import unittest
from threading import Event, Lock, Thread

from mirabox_sdk import LogMessageCommand, SetStateCommand, SetTitleCommand
from mirabox_sdk._next.messaging.outbound import (
    OutboundCommandQueue,
    OutboundCommandQueueClosedError,
    OutboundQueueFullError,
)
from mirabox_sdk._next.messaging.ports import OutboundCommandSink, OutboundCommandSource


class OutboundCommandQueueTests(unittest.TestCase):
    def test_rejects_invalid_configuration(self) -> None:
        for invalid_limit in (0, -1, True, 1.5):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                OutboundCommandQueue(invalid_limit)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            OutboundCommandQueue(1, coalesce_commands=1)  # type: ignore[arg-type]

    def test_implementation_explicitly_inherits_typed_ports(self) -> None:
        self.assertIn(OutboundCommandSource, OutboundCommandQueue.__mro__)
        self.assertIn(OutboundCommandSink, OutboundCommandQueue.__mro__)

    def test_preserves_fifo_and_rejects_full_queue_synchronously(self) -> None:
        queue = OutboundCommandQueue(2, coalesce_commands=False)
        first = queue.send_async(LogMessageCommand("first"))
        second = queue.send_async(LogMessageCommand("second"))
        with self.assertRaisesRegex(OutboundQueueFullError, "limit=2"):
            queue.send_async(LogMessageCommand("third"))

        first_submission = queue.receive()
        second_submission = queue.receive()
        self.assertEqual(first_submission.command, LogMessageCommand("first"))
        self.assertEqual(second_submission.command, LogMessageCommand("second"))
        first_submission.completion._finish()
        second_submission.completion._finish()
        self.assertIsNone(first.result(timeout=0))
        self.assertIsNone(second.result(timeout=0))

        metrics = queue.metrics()
        self.assertEqual(metrics.submitted, 3)
        self.assertEqual(metrics.enqueued, 2)
        self.assertEqual(metrics.dequeued, 2)
        self.assertEqual(metrics.rejected_full, 1)

    def test_coalesces_adjacent_state_commands_and_completes_every_sender(self) -> None:
        queue = OutboundCommandQueue(1, coalesce_commands=True)
        old = queue.send_async(SetTitleCommand("button", "old", target=1, state=2))
        new = queue.send_async(SetTitleCommand("button", "new", target=1, state=2))

        submission = queue.receive()
        self.assertEqual(
            submission.command,
            SetTitleCommand("button", "new", target=1, state=2),
        )
        submission.completion._finish()

        self.assertIsNone(old.result(timeout=0))
        self.assertIsNone(new.result(timeout=0))
        metrics = queue.metrics()
        self.assertEqual(metrics.current_depth, 0)
        self.assertEqual(metrics.enqueued, 1)
        self.assertEqual(metrics.coalesced, 1)
        self.assertEqual(metrics.dequeued, 1)

    def test_different_command_is_an_outbound_coalescing_barrier(self) -> None:
        queue = OutboundCommandQueue(3, coalesce_commands=True)
        queue.send_async(SetStateCommand("button", 1))
        queue.send_async(LogMessageCommand("barrier"))
        queue.send_async(SetStateCommand("button", 2))

        self.assertEqual(
            [queue.receive().command for _ in range(3)],
            [
                SetStateCommand("button", 1),
                LogMessageCommand("barrier"),
                SetStateCommand("button", 2),
            ],
        )
        self.assertEqual(queue.metrics().coalesced, 0)

    def test_shutdown_timeout_fails_every_accepted_coalesced_command(self) -> None:
        queue = OutboundCommandQueue(1, coalesce_commands=True)
        old = queue.send_async(SetStateCommand("button", 1))
        new = queue.send_async(SetStateCommand("button", 2))

        self.assertFalse(queue.shutdown(timeout=0))

        for completion in (old, new):
            with self.assertRaisesRegex(
                OutboundCommandQueueClosedError,
                "discarded during shutdown",
            ):
                completion.result(timeout=0)
        metrics = queue.metrics()
        self.assertEqual(metrics.coalesced, 1)
        self.assertEqual(metrics.discarded_during_shutdown, 1)

    def test_shutdown_drains_with_consumer_and_rejects_late_commands(self) -> None:
        queue = OutboundCommandQueue(2)
        completions = [
            queue.send_async(LogMessageCommand("first")),
            queue.send_async(LogMessageCommand("second")),
        ]
        consumer_finished = Event()

        def consume() -> None:
            for _ in completions:
                queue.receive().completion._finish()
            consumer_finished.set()

        consumer = Thread(target=consume)
        consumer.start()
        self.assertTrue(queue.shutdown(timeout=1))
        self.assertTrue(consumer_finished.wait(1))
        consumer.join(1)
        for completion in completions:
            self.assertIsNone(completion.result(timeout=0))

        with self.assertRaises(OutboundCommandQueueClosedError):
            queue.send_async(LogMessageCommand("late"))
        with self.assertRaises(OutboundCommandQueueClosedError):
            queue.receive()
        self.assertEqual(queue.metrics().rejected_after_shutdown, 1)

    def test_send_waits_for_completion_without_doing_writer_work(self) -> None:
        queue = OutboundCommandQueue(1)
        send_finished = Event()

        def send() -> None:
            queue.send(LogMessageCommand("message"))
            send_finished.set()

        producer = Thread(target=send)
        producer.start()
        submission = queue.receive(timeout=1)
        self.assertFalse(send_finished.is_set())
        submission.completion._finish()
        self.assertTrue(send_finished.wait(1))
        producer.join(1)

    def test_concurrent_producers_are_accepted_once_without_silent_drops(self) -> None:
        producer_count = 32
        queue = OutboundCommandQueue(producer_count, coalesce_commands=False)
        completions = []
        completions_lock = Lock()

        def submit(index: int) -> None:
            completion = queue.send_async(LogMessageCommand(str(index)))
            with completions_lock:
                completions.append(completion)

        producers = [Thread(target=submit, args=(index,)) for index in range(producer_count)]
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join(1)
            self.assertFalse(producer.is_alive())

        submissions = [queue.receive() for _ in range(producer_count)]
        for submission in submissions:
            submission.completion._finish()
        self.assertEqual(
            {submission.command.message for submission in submissions},
            {str(index) for index in range(producer_count)},
        )
        self.assertEqual(len(completions), producer_count)
        for completion in completions:
            self.assertIsNone(completion.result(timeout=0))
        self.assertEqual(queue.metrics().rejected, 0)


if __name__ == "__main__":
    unittest.main()

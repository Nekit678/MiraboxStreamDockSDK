from __future__ import annotations

import unittest
from threading import Event, current_thread
from threading import enumerate as enumerate_threads

from mirabox_sdk import LogMessageCommand, StreamDockCommand
from mirabox_sdk._next.messaging.outbound import OutboundCommandQueue
from mirabox_sdk._next.messaging.writer import (
    CommandWriter,
    CommandWriterLifecycleError,
    CommandWriterStoppedError,
)
from mirabox_sdk._next.transport.frames import OutboundFrame
from mirabox_sdk._next.transport.queues import RawOutboundQueue, TransportQueueClosedError


class _RecordingEncoder:
    def __init__(self) -> None:
        self.thread_names: list[str] = []

    def encode(self, command: StreamDockCommand) -> str:
        self.thread_names.append(current_thread().name)
        assert isinstance(command, LogMessageCommand)
        return command.message


class CommandWriterTests(unittest.TestCase):
    def test_serializes_off_caller_thread_preserves_fifo_and_bridges_receipts(
        self,
    ) -> None:
        commands = OutboundCommandQueue(3)
        raw_outbound = RawOutboundQueue(3)
        encoder = _RecordingEncoder()
        caller_thread_name = current_thread().name
        completions = [
            commands.send_async(LogMessageCommand(message))
            for message in ("first", "second", "third")
        ]
        commands.stop_accepting()

        writer = CommandWriter(commands, encoder, raw_outbound)
        writer.start()
        frames = [raw_outbound.receive(timeout=1) for _ in completions]
        self.assertEqual([frame.payload for frame in frames], ["first", "second", "third"])
        self.assertTrue(all(not completion.done() for completion in completions))

        frames[0].receipt._finish()
        frames[1].receipt._finish(error=OSError("send failed"))
        frames[2].receipt._finish()
        self.assertTrue(writer.drain(timeout=1))
        self.assertTrue(writer.stop(timeout=1))

        self.assertIsNone(completions[0].result(timeout=0))
        with self.assertRaisesRegex(OSError, "send failed"):
            completions[1].result(timeout=0)
        self.assertIsNone(completions[2].result(timeout=0))
        self.assertEqual(encoder.thread_names, ["mirabox-next-command-writer"] * 3)
        self.assertNotIn(caller_thread_name, encoder.thread_names)
        metrics = writer.metrics()
        self.assertEqual(metrics.commands_received, 3)
        self.assertEqual(metrics.serialized, 3)
        self.assertEqual(metrics.frames_enqueued, 3)
        self.assertEqual(metrics.completed, 2)
        self.assertEqual(metrics.completion_failures, 1)

    def test_encoder_failure_is_isolated_from_the_next_command(self) -> None:
        commands = OutboundCommandQueue(2)
        raw_outbound = RawOutboundQueue(1)
        failed = commands.send_async(LogMessageCommand("bad"))
        succeeded = commands.send_async(LogMessageCommand("good"))
        commands.stop_accepting()
        serialization_error = ValueError("cannot encode")

        class SelectiveEncoder:
            def encode(self, command: StreamDockCommand) -> str:
                assert isinstance(command, LogMessageCommand)
                if command.message == "bad":
                    raise serialization_error
                return command.message

        writer = CommandWriter(commands, SelectiveEncoder(), raw_outbound)
        with self.assertLogs(
            "mirabox_sdk._next.messaging.writer",
            level="ERROR",
        ):
            writer.start()
            frame = raw_outbound.receive(timeout=1)
        frame.receipt._finish()
        self.assertTrue(writer.drain(timeout=1))
        self.assertTrue(writer.stop(timeout=1))

        with self.assertRaises(ValueError) as caught:
            failed.result(timeout=0)
        self.assertIs(caught.exception, serialization_error)
        self.assertIsNone(succeeded.result(timeout=0))
        metrics = writer.metrics()
        self.assertEqual(metrics.serialization_failures, 1)
        self.assertEqual(metrics.frames_enqueued, 1)

    def test_raw_sink_failure_is_isolated_and_completes_each_command(self) -> None:
        commands = OutboundCommandQueue(2)
        first = commands.send_async(LogMessageCommand("first"))
        second = commands.send_async(LogMessageCommand("second"))
        commands.stop_accepting()
        sink_error = OSError("queue unavailable")

        class SelectiveSink:
            calls = 0

            def submit(self, frame: OutboundFrame) -> bool:
                self.calls += 1
                if self.calls == 1:
                    raise sink_error
                frame.receipt._finish()
                return True

        writer = CommandWriter(commands, _RecordingEncoder(), SelectiveSink())
        with self.assertLogs(
            "mirabox_sdk._next.messaging.writer",
            level="ERROR",
        ):
            writer.start()
            self.assertTrue(writer.drain(timeout=1))
        self.assertTrue(writer.stop(timeout=1))

        with self.assertRaises(OSError) as caught:
            first.result(timeout=0)
        self.assertIs(caught.exception, sink_error)
        self.assertIsNone(second.result(timeout=0))
        metrics = writer.metrics()
        self.assertEqual(metrics.raw_outbound_failures, 1)
        self.assertEqual(metrics.frames_enqueued, 1)

    def test_send_async_returns_before_worker_serialization_finishes(self) -> None:
        commands = OutboundCommandQueue(1)
        raw_outbound = RawOutboundQueue(1)
        encoder_started = Event()
        release_encoder = Event()

        class BlockingEncoder:
            def encode(self, command: StreamDockCommand) -> str:
                encoder_started.set()
                if not release_encoder.wait(1):
                    raise TimeoutError("test did not release encoder")
                return "serialized"

        writer = CommandWriter(commands, BlockingEncoder(), raw_outbound)
        writer.start()
        completion = commands.send_async(LogMessageCommand("message"))
        self.assertTrue(encoder_started.wait(1))
        self.assertFalse(completion.done())

        release_encoder.set()
        frame = raw_outbound.receive(timeout=1)
        frame.receipt._finish()
        self.assertTrue(writer.drain(timeout=1))
        self.assertTrue(writer.stop(timeout=1))
        self.assertIsNone(completion.result(timeout=0))

    def test_stop_from_worker_does_not_deadlock_and_drains_commands(self) -> None:
        commands = OutboundCommandQueue(2)
        completions = [
            commands.send_async(LogMessageCommand("stop")),
            commands.send_async(LogMessageCommand("after-stop")),
        ]
        writer: CommandWriter

        class StoppingEncoder:
            def encode(self, command: StreamDockCommand) -> str:
                assert isinstance(command, LogMessageCommand)
                if command.message == "stop" and not writer.stop(timeout=0):
                    raise AssertionError("worker stop request failed")
                return command.message

        class ImmediateSink:
            def submit(self, frame: OutboundFrame) -> bool:
                frame.receipt._finish()
                return True

        writer = CommandWriter(commands, StoppingEncoder(), ImmediateSink())
        writer.start()
        self.assertTrue(writer.stop(timeout=1))
        for completion in completions:
            self.assertIsNone(completion.result(timeout=0))
        self.assertEqual(writer.metrics().frames_enqueued, 2)

    def test_external_stop_fails_a_receipt_that_never_finishes(self) -> None:
        commands = OutboundCommandQueue(1)
        raw_outbound = RawOutboundQueue(1)
        completion = commands.send_async(LogMessageCommand("pending"))
        writer = CommandWriter(commands, _RecordingEncoder(), raw_outbound)
        writer.start()
        frame = raw_outbound.receive(timeout=1)

        self.assertTrue(writer.stop(timeout=1))
        with self.assertRaises(CommandWriterStoppedError):
            completion.result(timeout=0)
        self.assertFalse(frame.receipt.done())
        self.assertEqual(writer.metrics().discarded_during_shutdown, 1)

        frame.receipt._finish()
        with self.assertRaises(CommandWriterStoppedError):
            completion.result(timeout=0)

    def test_stop_timeout_unblocks_a_writer_backpressured_by_raw_sink(self) -> None:
        commands = OutboundCommandQueue(2)
        raw_outbound = RawOutboundQueue(1)
        both_encoded = Event()

        class CountingEncoder:
            count = 0

            def encode(self, command: StreamDockCommand) -> str:
                self.count += 1
                if self.count == 2:
                    both_encoded.set()
                assert isinstance(command, LogMessageCommand)
                return command.message

        completions = [
            commands.send_async(LogMessageCommand("first")),
            commands.send_async(LogMessageCommand("second")),
        ]
        writer = CommandWriter(commands, CountingEncoder(), raw_outbound)
        writer.start()
        self.assertTrue(both_encoded.wait(1))

        self.assertTrue(writer.stop(timeout=0.05))
        with self.assertRaises(CommandWriterStoppedError):
            completions[0].result(timeout=0)
        with self.assertRaises(TransportQueueClosedError):
            completions[1].result(timeout=0)
        self.assertFalse(
            any(
                thread.name == "mirabox-next-command-writer" and thread.is_alive()
                for thread in enumerate_threads()
            )
        )

    def test_start_and_stop_are_idempotent_without_thread_leaks(self) -> None:
        writer = CommandWriter(
            OutboundCommandQueue(1),
            _RecordingEncoder(),
            RawOutboundQueue(1),
        )
        writer.start()
        writer.start()
        self.assertTrue(writer.stop(timeout=1))
        self.assertTrue(writer.stop(timeout=0))
        with self.assertRaises(CommandWriterLifecycleError):
            writer.start()

        self.assertFalse(
            any(
                thread.name == "mirabox-next-command-writer" and thread.is_alive()
                for thread in enumerate_threads()
            )
        )

    def test_stop_before_start_is_terminal(self) -> None:
        writer = CommandWriter(
            OutboundCommandQueue(1),
            _RecordingEncoder(),
            RawOutboundQueue(1),
        )
        self.assertTrue(writer.stop(timeout=0))
        self.assertTrue(writer.stop(timeout=0))
        with self.assertRaises(CommandWriterLifecycleError):
            writer.start()

    def test_rejects_invalid_lifecycle_timeouts(self) -> None:
        writer = CommandWriter(
            OutboundCommandQueue(1),
            _RecordingEncoder(),
            RawOutboundQueue(1),
        )
        for timeout in (-1, True, float("inf"), float("nan"), "1"):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(
                    ValueError,
                    "non-negative number or None",
                ),
            ):
                writer.stop(timeout=timeout)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

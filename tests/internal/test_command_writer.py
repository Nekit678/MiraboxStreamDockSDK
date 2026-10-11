from __future__ import annotations

import unittest
from threading import Event, current_thread
from threading import enumerate as enumerate_threads
from time import monotonic

from mirabox_sdk import LogMessageCommand, OutboundCommandReentrancyError, StreamDockCommand
from mirabox_sdk._internal.messaging.outbound import OutboundCommandQueue
from mirabox_sdk._internal.messaging.writer import (
    CommandWriter,
    CommandWriterLifecycleError,
    CommandWriterStoppedError,
)
from mirabox_sdk._internal.transport.frames import OutboundFrame
from mirabox_sdk._internal.transport.queues import RawOutboundQueue, TransportQueueClosedError


class _RecordingEncoder:
    def __init__(self) -> None:
        self.thread_names: list[str] = []

    def encode(self, command: StreamDockCommand) -> str:
        self.thread_names.append(current_thread().name)
        assert isinstance(command, LogMessageCommand)
        return command.message


class CommandWriterTests(unittest.TestCase):
    def test_serialization_failure_callback_rejects_sync_send_and_allows_async_send(self) -> None:
        commands = OutboundCommandQueue(2)
        entered = Event()
        release = Event()
        callback_finished = Event()
        errors = []
        follow_ups = []

        class SelectiveEncoder(_RecordingEncoder):
            def encode(self, command):
                if command.message == "bad":
                    entered.set()
                    if not release.wait(1):
                        raise AssertionError("encoder was not released")
                    raise ValueError("cannot encode")
                return super().encode(command)

        class ImmediateSink:
            def submit(self, frame):
                frame.receipt._finish()
                return True

        def follow_up(completion):
            try:
                self.assertIsInstance(completion.exception(timeout=0), ValueError)
                try:
                    commands.send(LogMessageCommand("sync"), deadline=monotonic() + 0.2)
                except Exception as exc:
                    errors.append(exc)
                follow_ups.append(commands.send_async(LogMessageCommand("async")))
            finally:
                callback_finished.set()

        writer = CommandWriter(commands, SelectiveEncoder(), ImmediateSink())
        writer.start()
        try:
            completion = commands.send_async(LogMessageCommand("bad"))
            self.assertTrue(entered.wait(1))
            completion.add_done_callback(follow_up)
            with self.assertLogs("mirabox_sdk._internal.messaging.writer", level="ERROR"):
                release.set()
                self.assertTrue(callback_finished.wait(1))
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], OutboundCommandReentrancyError)
            self.assertEqual(len(follow_ups), 1)
            follow_ups[0].result(timeout=1)
            self.assertTrue(writer.drain(timeout=1))
            self.assertEqual(commands.metrics().enqueued, 2)
        finally:
            release.set()
            self.assertTrue(writer.stop(timeout=1))

    def test_cancel_unblocks_raw_backpressure_without_draining_the_first_frame(self) -> None:
        commands = OutboundCommandQueue(2)
        entered = Event()

        class BlockingRawQueue(RawOutboundQueue):
            def submit(self, frame, *, timeout=None):
                if frame.payload == "cancelled":
                    entered.set()
                return super().submit(frame, timeout=timeout)

        raw = BlockingRawQueue(1)
        writer = CommandWriter(commands, _RecordingEncoder(), raw)
        writer.start()
        first = commands.send_async(LogMessageCommand("first"))
        cancelled = commands.send_async(LogMessageCommand("cancelled"))
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(cancelled.cancel())
            self.assertTrue(writer.stop(timeout=1))
            self.assertEqual(raw.metrics().current_depth, 1)
            self.assertFalse(raw.receive(timeout=0).receipt.done())
            with self.assertRaises(CommandWriterStoppedError):
                first.result(timeout=0)
        finally:
            raw.stop_accepting()
            writer.stop(timeout=1)

    def test_deadline_during_encoding_completes_without_late_frame(self) -> None:
        commands = OutboundCommandQueue(1)
        raw = RawOutboundQueue(1)
        entered = Event()
        release = Event()

        class BlockingEncoder:
            def encode(self, command: StreamDockCommand) -> str:
                entered.set()
                if not release.wait(1):
                    raise AssertionError("encoder was not released")
                return "late"

        writer = CommandWriter(commands, BlockingEncoder(), raw)
        writer.start()
        try:
            completion = commands.send_async(LogMessageCommand("late"), deadline=monotonic() + 0.1)
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                completion.result(timeout=1)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            self.assertTrue(writer.stop(timeout=1))
        self.assertEqual(raw.metrics().enqueued, 0)

    def test_raw_backpressure_uses_remaining_deadline_and_clears_pending(self) -> None:
        commands = OutboundCommandQueue(2)
        raw = RawOutboundQueue(1)
        writer = CommandWriter(commands, _RecordingEncoder(), raw)
        writer.start()
        first = commands.send_async(LogMessageCommand("first"))
        second = commands.send_async(LogMessageCommand("expired"), deadline=monotonic() + 0.05)
        try:
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                second.result(timeout=1)
            frame = raw.receive(timeout=1)
            self.assertEqual(frame.payload, "first")
            frame.receipt._finish()
            first.result(timeout=1)
            self.assertTrue(writer.drain(timeout=1))
            # A frame admitted concurrently with expiry is guarded against send.
            if raw.metrics().current_depth:
                expired = raw.receive(timeout=0)
                self.assertFalse(expired.begin_send(lambda: None))
        finally:
            self.assertTrue(writer.stop(timeout=1))

    def test_cancel_after_serialization_prevents_transport_send(self) -> None:
        commands = OutboundCommandQueue(1)
        raw = RawOutboundQueue(1)
        writer = CommandWriter(commands, _RecordingEncoder(), raw)
        writer.start()
        completion = commands.send_async(LogMessageCommand("cancelled"))
        try:
            frame = raw.receive(timeout=1)
            self.assertTrue(completion.cancel())
            self.assertFalse(frame.begin_send(lambda: None))
            self.assertTrue(writer.drain(timeout=1))
        finally:
            self.assertTrue(writer.stop(timeout=1))

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
        self.assertEqual(encoder.thread_names, ["mirabox-internal-command-writer"] * 3)
        self.assertNotIn(caller_thread_name, encoder.thread_names)
        metrics = writer.metrics()
        self.assertEqual(metrics.commands_received, 3)
        self.assertEqual(metrics.serialized, 3)
        self.assertEqual(metrics.frames_enqueued, 3)
        self.assertEqual(metrics.completed, 2)
        self.assertEqual(metrics.completion_failures, 1)

    def test_encoder_failure_is_isolated_from_the_internal_command(self) -> None:
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
            "mirabox_sdk._internal.messaging.writer",
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
            "mirabox_sdk._internal.messaging.writer",
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

        self.assertFalse(writer.stop(timeout=0.05))
        self.assertTrue(writer.stop(timeout=1))
        with self.assertRaises(CommandWriterStoppedError):
            completions[0].result(timeout=0)
        with self.assertRaises(TransportQueueClosedError):
            completions[1].result(timeout=0)
        self.assertFalse(
            any(
                thread.name == "mirabox-internal-command-writer" and thread.is_alive()
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
                thread.name == "mirabox-internal-command-writer" and thread.is_alive()
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

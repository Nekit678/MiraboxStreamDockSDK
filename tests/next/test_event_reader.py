from __future__ import annotations

import unittest
from collections import deque
from threading import Lock, current_thread
from threading import enumerate as enumerate_threads

from mirabox_sdk import StreamDockEvent, UnknownStreamDockEvent
from mirabox_sdk._next.messaging.inbound import InboundEventQueue
from mirabox_sdk._next.messaging.reader import EventReader, EventReaderLifecycleError
from mirabox_sdk._next.transport.queues import RawInboundQueue


def _event(name: str) -> UnknownStreamDockEvent:
    return UnknownStreamDockEvent(event=name, data={"event": name})


class _DequeFrameSource:
    def __init__(self, frames: tuple[str, ...] = ()) -> None:
        self._frames = deque(frames)
        self._lock = Lock()

    def receive(self, *, timeout: float | None = None) -> str:
        with self._lock:
            if self._frames:
                return self._frames.popleft()
        raise TimeoutError


class _RecordingDecoder:
    def __init__(self) -> None:
        self.thread_names: list[str] = []

    def decode(self, frame: str) -> StreamDockEvent:
        self.thread_names.append(current_thread().name)
        return _event(frame)


class EventReaderTests(unittest.TestCase):
    def test_decodes_off_the_producer_thread_and_preserves_wire_order(self) -> None:
        raw_inbound = RawInboundQueue(3)
        typed_inbound = InboundEventQueue(3)
        decoder = _RecordingDecoder()
        producer_thread_name = current_thread().name
        for frame in ("first", "second", "third"):
            self.assertTrue(raw_inbound.submit(frame))
        raw_inbound.stop_accepting()

        reader = EventReader(raw_inbound, decoder, typed_inbound)
        reader.start()
        self.assertTrue(reader.drain(timeout=1))
        self.assertTrue(reader.stop(timeout=1))

        self.assertEqual(
            [typed_inbound.receive().event_name for _ in range(3)],
            ["first", "second", "third"],
        )
        self.assertEqual(decoder.thread_names, ["mirabox-next-event-reader"] * 3)
        self.assertNotIn(producer_thread_name, decoder.thread_names)
        metrics = reader.metrics()
        self.assertEqual(metrics.frames_received, 3)
        self.assertEqual(metrics.decoded, 3)
        self.assertEqual(metrics.submitted, 3)
        self.assertEqual(metrics.unknown_events, 3)

    def test_decoder_failure_is_isolated_and_payload_is_not_logged(self) -> None:
        source = _DequeFrameSource(("secret malformed payload", "valid"))
        typed_inbound = InboundEventQueue(1)

        class FailingDecoder:
            def decode(self, frame: str) -> StreamDockEvent:
                if frame.startswith("secret"):
                    raise ValueError(frame)
                return _event(frame)

        reader = EventReader(source, FailingDecoder(), typed_inbound)
        with self.assertLogs(
            "mirabox_sdk._next.messaging.reader",
            level="WARNING",
        ) as captured:
            reader.start()
            self.assertTrue(reader.drain(timeout=1))
            self.assertTrue(reader.stop(timeout=1))

        self.assertEqual(typed_inbound.receive().event_name, "valid")
        self.assertNotIn("secret malformed payload", "\n".join(captured.output))
        metrics = reader.metrics()
        self.assertEqual(metrics.protocol_failures, 1)
        self.assertEqual(metrics.decoded, 1)

    def test_sink_rejection_and_failure_do_not_stop_the_reader(self) -> None:
        source = _DequeFrameSource(("rejected", "failed", "accepted"))
        accepted: list[str] = []

        class SelectiveSink:
            def submit(self, event: StreamDockEvent) -> bool:
                if event.event_name == "rejected":
                    return False
                if event.event_name == "failed":
                    raise RuntimeError("sink failure")
                accepted.append(event.event_name)
                return True

        reader = EventReader(source, _RecordingDecoder(), SelectiveSink())
        with self.assertLogs(
            "mirabox_sdk._next.messaging.reader",
            level="ERROR",
        ):
            reader.start()
            self.assertTrue(reader.drain(timeout=1))
            self.assertTrue(reader.stop(timeout=1))

        self.assertEqual(accepted, ["accepted"])
        metrics = reader.metrics()
        self.assertEqual(metrics.rejected, 1)
        self.assertEqual(metrics.sink_failures, 1)
        self.assertEqual(metrics.frames_received, 3)

    def test_stop_from_worker_drains_available_frames_without_deadlock(self) -> None:
        source = _DequeFrameSource(("stop", "after-stop"))
        typed_inbound = InboundEventQueue(2)
        reader: EventReader

        class StoppingDecoder:
            def decode(self, frame: str) -> StreamDockEvent:
                if frame == "stop":
                    self.assert_stop_succeeded()
                return _event(frame)

            @staticmethod
            def assert_stop_succeeded() -> None:
                if not reader.stop(timeout=0):
                    raise AssertionError("worker stop request failed")

        reader = EventReader(source, StoppingDecoder(), typed_inbound)
        reader.start()
        self.assertTrue(reader.stop(timeout=1))

        self.assertEqual(
            [typed_inbound.receive().event_name for _ in range(2)],
            ["stop", "after-stop"],
        )

    def test_start_and_stop_are_idempotent_without_thread_leaks(self) -> None:
        reader = EventReader(_DequeFrameSource(), _RecordingDecoder(), InboundEventQueue(1))
        reader.start()
        reader.start()
        self.assertTrue(reader.stop(timeout=1))
        self.assertTrue(reader.stop(timeout=0))
        with self.assertRaises(EventReaderLifecycleError):
            reader.start()

        self.assertFalse(
            any(
                thread.name == "mirabox-next-event-reader" and thread.is_alive()
                for thread in enumerate_threads()
            )
        )

    def test_stop_timeout_unblocks_a_reader_backpressured_by_typed_sink(self) -> None:
        raw_inbound = RawInboundQueue(2)
        typed_inbound = InboundEventQueue(1)
        self.assertTrue(raw_inbound.submit("first"))
        self.assertTrue(raw_inbound.submit("second"))
        reader = EventReader(raw_inbound, _RecordingDecoder(), typed_inbound)
        reader.start()

        self.assertTrue(reader.stop(timeout=0.05))
        self.assertEqual(typed_inbound.receive().event_name, "first")
        self.assertEqual(reader.metrics().rejected, 1)
        self.assertFalse(
            any(
                thread.name == "mirabox-next-event-reader" and thread.is_alive()
                for thread in enumerate_threads()
            )
        )

    def test_stop_before_start_is_terminal(self) -> None:
        reader = EventReader(_DequeFrameSource(), _RecordingDecoder(), InboundEventQueue(1))
        self.assertTrue(reader.stop(timeout=0))
        self.assertTrue(reader.stop(timeout=0))
        with self.assertRaises(EventReaderLifecycleError):
            reader.start()

    def test_rejects_invalid_lifecycle_timeouts(self) -> None:
        reader = EventReader(_DequeFrameSource(), _RecordingDecoder(), InboundEventQueue(1))
        for timeout in (-1, True, float("inf"), float("nan"), "1"):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(
                    ValueError,
                    "non-negative number or None",
                ),
            ):
                reader.stop(timeout=timeout)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

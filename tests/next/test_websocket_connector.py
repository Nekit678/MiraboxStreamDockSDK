from __future__ import annotations

import ast
import unittest
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from threading import enumerate as enumerate_threads
from typing import Any

from mirabox_sdk._next.transport.frames import OutboundFrame, TransportReceipt
from mirabox_sdk._next.transport.ports import WebSocketConnector
from mirabox_sdk._next.transport.queues import (
    RawInboundQueue,
    RawOutboundQueue,
    SessionEventQueue,
)
from mirabox_sdk._next.transport.session import Connected, Disconnected, TransportError
from mirabox_sdk._next.transport.websocket import (
    UnsupportedWebSocketFrameError,
    WebSocketClientConnector,
    WebSocketConnectorClosedError,
    WebSocketConnectorError,
    WebSocketConnectorLifecycleError,
    WebSocketDisconnectedError,
)


class _FakeWebSocketApp:
    def __init__(
        self,
        url: str,
        *,
        on_open: Any,
        on_message: Any,
        on_error: Any,
        on_close: Any,
        block_open: bool,
        block_sends: bool,
        send_error: Exception | None,
    ) -> None:
        self.url = url
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.block_open = block_open
        self.block_sends = block_sends
        self.send_error = send_error
        self.run_started = Event()
        self.release_open = Event()
        self.send_started = Event()
        self.release_sends = Event()
        self.loop_finished = Event()
        self._lock = Lock()
        self._status_code: int | None = 1000
        self._reason: str | None = "closed"
        self._remote_disconnected = False
        self.close_calls = 0
        self.sent: list[tuple[str, str]] = []
        if not block_open:
            self.release_open.set()
        if not block_sends:
            self.release_sends.set()

    def run_forever(self) -> None:
        self.run_started.set()
        if not self.release_open.wait(2):
            raise TimeoutError("test WebSocket open was not released")
        self.on_open(self)
        if not self.loop_finished.wait(2):
            raise TimeoutError("test WebSocket loop was not closed")
        self.on_close(self, self._status_code, self._reason)

    def send(self, data: str) -> None:
        self.send_started.set()
        if not self.release_sends.wait(2):
            raise TimeoutError("test WebSocket send was not released")
        if self.send_error is not None:
            raise self.send_error
        if self._remote_disconnected:
            raise OSError("remote disconnected during send")
        with self._lock:
            self.sent.append((data, current_thread().name))

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
        self.release_sends.set()
        self.loop_finished.set()

    def emit_message(self, message: object) -> None:
        self.on_message(self, message)

    def emit_error(self, error: object) -> None:
        self.on_error(self, error)

    def disconnect(
        self,
        *,
        status_code: int | None = 1006,
        reason: str | None = "unexpected",
    ) -> None:
        self._status_code = status_code
        self._reason = reason
        self._remote_disconnected = True
        self.release_sends.set()
        self.loop_finished.set()


class _FakeWebSocketFactory:
    def __init__(
        self,
        *,
        block_open: bool = False,
        block_sends: bool = False,
        send_error: Exception | None = None,
    ) -> None:
        self.block_open = block_open
        self.block_sends = block_sends
        self.send_error = send_error
        self.app: _FakeWebSocketApp | None = None

    def __call__(self, url: str, **callbacks: Any) -> _FakeWebSocketApp:
        self.app = _FakeWebSocketApp(
            url,
            block_open=self.block_open,
            block_sends=self.block_sends,
            send_error=self.send_error,
            **callbacks,
        )
        return self.app


class _ConnectorHarness:
    def __init__(
        self,
        *,
        outbound_limit: int = 8,
        outbound_shutdown_timeout: float | None = 1,
        block_open: bool = False,
        block_sends: bool = False,
        send_error: Exception | None = None,
    ) -> None:
        self.raw_inbound = RawInboundQueue(8)
        self.raw_outbound = RawOutboundQueue(outbound_limit)
        self.session_events = SessionEventQueue(16)
        self.factory = _FakeWebSocketFactory(
            block_open=block_open,
            block_sends=block_sends,
            send_error=send_error,
        )
        self.connector = WebSocketClientConnector(
            12345,
            self.raw_inbound,
            self.raw_outbound,
            self.session_events,
            outbound_shutdown_timeout=outbound_shutdown_timeout,
            websocket_app_factory=self.factory,
        )
        assert self.factory.app is not None
        self.app = self.factory.app
        self.run_errors: list[Exception] = []
        self.thread = Thread(target=self._run, name="test-websocket-lifecycle")

    def start(self) -> None:
        self.thread.start()
        if not self.app.run_started.wait(1):
            raise AssertionError("fake WebSocket loop did not start")
        connected = self.session_events.receive(timeout=1)
        if not isinstance(connected, Connected):
            raise AssertionError(f"expected Connected, got {type(connected).__name__}")

    def finish(self) -> Disconnected:
        self.thread.join(1)
        if self.thread.is_alive():
            raise AssertionError("connector lifecycle thread did not finish")
        if self.run_errors:
            raise self.run_errors[0]
        event = self.session_events.receive(timeout=1)
        if not isinstance(event, Disconnected):
            raise AssertionError(f"expected Disconnected, got {type(event).__name__}")
        return event

    def _run(self) -> None:
        try:
            self.connector.run_forever()
        except Exception as exc:
            self.run_errors.append(exc)


class WebSocketClientConnectorTests(unittest.TestCase):
    def test_transport_module_is_api_independent_and_explicitly_inherits_port(
        self,
    ) -> None:
        module_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "mirabox_sdk"
            / "_next"
            / "transport"
            / "websocket.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        forbidden = {
            "commands",
            "events",
            "messaging",
            "parser",
            "plugin",
            "protocol",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                with self.subTest(module=node.module):
                    self.assertFalse(forbidden.intersection((node.module or "").split(".")))

        self.assertIn(WebSocketConnector, WebSocketClientConnector.__mro__)

    def test_forwards_text_frames_without_parsing_or_runtime_callbacks(self) -> None:
        harness = _ConnectorHarness()
        harness.start()
        frame = '{"notStreamDockProtocol": true}'

        harness.app.emit_message(frame)

        self.assertEqual(harness.raw_inbound.receive(timeout=1), frame)
        harness.connector.close()
        harness.finish()
        metrics = harness.connector.metrics()
        self.assertEqual(metrics.inbound_frames_received, 1)
        self.assertEqual(metrics.inbound_frames_forwarded, 1)
        self.assertEqual(metrics.inbound_frames_rejected, 0)

    def test_rejects_binary_frames_observably_without_logging_payload(self) -> None:
        harness = _ConnectorHarness()
        harness.start()

        with self.assertLogs(
            "mirabox_sdk._next.transport.websocket",
            level="ERROR",
        ) as captured:
            harness.app.emit_message(b"secret binary payload")

        event = harness.session_events.receive(timeout=1)
        self.assertIsInstance(event, TransportError)
        assert isinstance(event, TransportError)
        self.assertIsInstance(event.error, UnsupportedWebSocketFrameError)
        with self.assertRaises(TimeoutError):
            harness.raw_inbound.receive(timeout=0)
        self.assertNotIn("secret binary payload", "\n".join(captured.output))

        harness.connector.close()
        harness.finish()
        metrics = harness.connector.metrics()
        self.assertEqual(metrics.binary_frames_rejected, 1)
        self.assertEqual(metrics.inbound_frames_rejected, 1)
        self.assertEqual(metrics.transport_error_count, 1)

    def test_single_owned_sender_preserves_fifo_and_completes_receipts(self) -> None:
        harness = _ConnectorHarness()
        harness.start()
        frames = [
            OutboundFrame(payload, TransportReceipt()) for payload in ("first", "second", "third")
        ]

        for frame in frames:
            self.assertTrue(harness.raw_outbound.submit(frame))
        for frame in frames:
            self.assertIsNone(frame.receipt.result(timeout=1))

        harness.connector.close()
        harness.finish()
        self.assertEqual(
            harness.app.sent,
            [
                ("first", "mirabox-next-websocket-sender"),
                ("second", "mirabox-next-websocket-sender"),
                ("third", "mirabox-next-websocket-sender"),
            ],
        )
        metrics = harness.connector.metrics()
        self.assertEqual(metrics.outbound_frames_received, 3)
        self.assertEqual(metrics.outbound_frames_sent, 3)
        self.assertEqual(metrics.outbound_send_failures, 0)

    def test_send_failure_fails_receipt_and_publishes_transport_error(self) -> None:
        send_error = OSError("send failed")
        harness = _ConnectorHarness(send_error=send_error)
        harness.start()
        frame = OutboundFrame("payload", TransportReceipt())
        self.assertTrue(harness.raw_outbound.submit(frame))

        with self.assertRaises(OSError) as caught:
            frame.receipt.result(timeout=1)
        self.assertIs(caught.exception, send_error)

        transport_error = harness.session_events.receive(timeout=1)
        self.assertIsInstance(transport_error, TransportError)
        assert isinstance(transport_error, TransportError)
        self.assertIs(transport_error.error, send_error)
        harness.finish()
        metrics = harness.connector.metrics()
        self.assertEqual(metrics.outbound_send_failures, 1)
        self.assertEqual(metrics.outbound_frames_sent, 0)
        self.assertEqual(metrics.transport_error_count, 1)

    def test_invalid_outbound_frame_is_isolated_from_the_next_frame(self) -> None:
        harness = _ConnectorHarness()
        harness.start()
        invalid = OutboundFrame(b"binary", TransportReceipt())  # type: ignore[arg-type]
        valid = OutboundFrame("text", TransportReceipt())
        self.assertTrue(harness.raw_outbound.submit(invalid))
        self.assertTrue(harness.raw_outbound.submit(valid))

        with self.assertRaisesRegex(TypeError, "text frame"):
            invalid.receipt.result(timeout=1)
        error_event = harness.session_events.receive(timeout=1)
        self.assertIsInstance(error_event, TransportError)
        self.assertIsNone(valid.receipt.result(timeout=1))

        harness.connector.close()
        harness.finish()
        self.assertEqual(harness.app.sent, [("text", "mirabox-next-websocket-sender")])
        metrics = harness.connector.metrics()
        self.assertEqual(metrics.outbound_send_failures, 1)
        self.assertEqual(metrics.outbound_frames_sent, 1)

    def test_connect_error_and_unexpected_disconnect_are_typed_and_counted(
        self,
    ) -> None:
        harness = _ConnectorHarness()
        harness.start()

        harness.app.emit_error("opaque websocket-client error")
        error_event = harness.session_events.receive(timeout=1)
        self.assertIsInstance(error_event, TransportError)
        assert isinstance(error_event, TransportError)
        self.assertIsInstance(error_event.error, WebSocketConnectorError)
        self.assertNotIn("opaque websocket-client error", str(error_event.error))

        harness.app.disconnect(status_code=1006, reason="peer vanished")
        disconnected = harness.finish()
        self.assertEqual(disconnected.status_code, 1006)
        self.assertEqual(disconnected.reason, "peer vanished")
        metrics = harness.connector.metrics()
        self.assertEqual(metrics.connect_count, 1)
        self.assertEqual(metrics.disconnect_count, 1)
        self.assertEqual(metrics.last_close_code, 1006)
        self.assertEqual(metrics.transport_error_count, 1)

    def test_concurrent_close_is_idempotent_and_drains_raw_outbound_queue(
        self,
    ) -> None:
        harness = _ConnectorHarness(block_sends=True)
        harness.start()
        frames = [
            OutboundFrame(payload, TransportReceipt()) for payload in ("first", "second", "third")
        ]
        for frame in frames:
            self.assertTrue(harness.raw_outbound.submit(frame))
        self.assertTrue(harness.app.send_started.wait(1))

        close_finished = [Event() for _ in range(6)]
        closers = [
            Thread(
                target=lambda finished=finished: (
                    harness.connector.close(),
                    finished.set(),
                )
            )
            for finished in close_finished
        ]
        for closer in closers:
            closer.start()
        self.assertFalse(any(finished.wait(0.02) for finished in close_finished))

        harness.app.release_sends.set()
        for closer in closers:
            closer.join(1)
            self.assertFalse(closer.is_alive())
        for frame in frames:
            self.assertIsNone(frame.receipt.result(timeout=0))

        harness.finish()
        self.assertEqual(harness.app.close_calls, 1)
        self.assertEqual(
            [payload for payload, _thread_name in harness.app.sent],
            ["first", "second", "third"],
        )
        self.assertEqual(harness.connector.metrics().outbound_drain_timeouts, 0)

    def test_close_during_send_times_out_and_fails_every_remaining_receipt(
        self,
    ) -> None:
        harness = _ConnectorHarness(
            outbound_limit=2,
            outbound_shutdown_timeout=0.02,
            block_sends=True,
        )
        harness.start()
        frames = [OutboundFrame(payload, TransportReceipt()) for payload in ("in-flight", "queued")]
        for frame in frames:
            self.assertTrue(harness.raw_outbound.submit(frame))
        self.assertTrue(harness.app.send_started.wait(1))

        harness.connector.close()

        for frame in frames:
            with self.assertRaises(WebSocketConnectorClosedError):
                frame.receipt.result(timeout=1)
        harness.finish()
        metrics = harness.connector.metrics()
        self.assertEqual(metrics.outbound_drain_timeouts, 1)
        self.assertEqual(metrics.outbound_discarded_during_shutdown, 2)
        self.assertEqual(harness.app.close_calls, 1)

    def test_unexpected_disconnect_fails_in_flight_and_queued_frames(self) -> None:
        harness = _ConnectorHarness(block_sends=True)
        harness.start()
        frames = [OutboundFrame(payload, TransportReceipt()) for payload in ("in-flight", "queued")]
        for frame in frames:
            self.assertTrue(harness.raw_outbound.submit(frame))
        self.assertTrue(harness.app.send_started.wait(1))

        harness.app.disconnect()

        for frame in frames:
            self.assertIsInstance(
                frame.receipt.exception(timeout=1),
                (OSError, WebSocketDisconnectedError),
            )
        events = [
            harness.session_events.receive(timeout=1),
            harness.session_events.receive(timeout=1),
        ]
        self.assertEqual(
            {type(event) for event in events},
            {TransportError, Disconnected},
        )
        harness.thread.join(1)
        self.assertFalse(harness.thread.is_alive())
        self.assertFalse(
            any(
                thread.name == "mirabox-next-websocket-sender" and thread.is_alive()
                for thread in enumerate_threads()
            )
        )

    def test_close_before_run_is_terminal_and_fails_queued_frames(self) -> None:
        harness = _ConnectorHarness()
        frame = OutboundFrame("never sent", TransportReceipt())
        self.assertTrue(harness.raw_outbound.submit(frame))

        harness.connector.close()
        harness.connector.close()

        with self.assertRaises(WebSocketConnectorClosedError):
            frame.receipt.result(timeout=0)
        with self.assertRaises(WebSocketConnectorLifecycleError):
            harness.connector.run_forever()
        self.assertEqual(harness.app.close_calls, 1)

    def test_close_racing_with_initial_connect_closes_the_late_socket(self) -> None:
        harness = _ConnectorHarness(block_open=True)
        harness.thread.start()
        self.assertTrue(harness.app.run_started.wait(1))

        harness.connector.close()
        harness.app.release_open.set()
        harness.thread.join(1)

        self.assertFalse(harness.thread.is_alive())
        self.assertEqual(harness.app.close_calls, 2)
        self.assertIsInstance(harness.session_events.receive(timeout=1), Disconnected)
        with self.assertRaises(TimeoutError):
            harness.session_events.receive(timeout=0)

    def test_rejects_invalid_configuration(self) -> None:
        inbound = RawInboundQueue(1)
        outbound = RawOutboundQueue(1)
        sessions = SessionEventQueue(1)
        factory = _FakeWebSocketFactory()

        for port in (0, 65536, True, 1.5, "12345"):
            with (
                self.subTest(port=port),
                self.assertRaisesRegex(ValueError, "between 1 and 65535"),
            ):
                WebSocketClientConnector(
                    port,  # type: ignore[arg-type]
                    inbound,
                    outbound,
                    sessions,
                    websocket_app_factory=factory,
                )

        for timeout in (-1, True, float("inf"), float("nan"), "1"):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(ValueError, "non-negative number or None"),
            ):
                WebSocketClientConnector(
                    12345,
                    inbound,
                    outbound,
                    sessions,
                    outbound_shutdown_timeout=timeout,  # type: ignore[arg-type]
                    websocket_app_factory=factory,
                )


if __name__ == "__main__":
    unittest.main()

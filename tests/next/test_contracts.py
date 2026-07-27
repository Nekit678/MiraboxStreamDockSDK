from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from concurrent.futures import InvalidStateError
from dataclasses import FrozenInstanceError
from importlib.util import resolve_name
from inspect import signature
from pathlib import Path
from threading import Thread

import mirabox_sdk
from mirabox_sdk import RegisterPluginCommand, StreamDockCommand, StreamDockEvent
from mirabox_sdk._next.boundary.composition import ComposedStreamDockBoundary
from mirabox_sdk._next.boundary.config import BoundaryQueueConfig
from mirabox_sdk._next.boundary.metrics import StreamDockBoundaryMetrics
from mirabox_sdk._next.boundary.ports import (
    StreamDockBoundary,
    WebSocketConnectorFactory,
)
from mirabox_sdk._next.messaging.inbound import InboundEventQueue
from mirabox_sdk._next.messaging.models import CommandFuture, CommandSubmission
from mirabox_sdk._next.messaging.outbound import OutboundCommandQueue
from mirabox_sdk._next.messaging.ports import (
    CommandWriterWorker,
    EventReaderWorker,
    InboundEventQueueControl,
    InboundEventSink,
    InboundEventSource,
    OutboundCommandQueueControl,
    OutboundCommandSink,
    OutboundCommandSource,
)
from mirabox_sdk._next.messaging.ports import (
    QueueAcceptanceControl as MessagingQueueAcceptanceControl,
)
from mirabox_sdk._next.messaging.reader import EventReader
from mirabox_sdk._next.messaging.writer import CommandWriter
from mirabox_sdk._next.protocol.adapters.legacy import LegacyEventParserAdapter
from mirabox_sdk._next.protocol.decoder import JsonStreamDockEventDecoder
from mirabox_sdk._next.protocol.encoder import JsonStreamDockCommandEncoder
from mirabox_sdk._next.protocol.ports import (
    DecodedEventParser,
    StreamDockCommandEncoder,
    StreamDockEventDecoder,
)
from mirabox_sdk._next.transport.frames import OutboundFrame, TransportReceipt
from mirabox_sdk._next.transport.metrics import WebSocketConnectorMetrics
from mirabox_sdk._next.transport.ports import (
    QueueAcceptanceControl as TransportQueueAcceptanceControl,
)
from mirabox_sdk._next.transport.ports import (
    RawInboundSink,
    RawInboundSource,
    RawOutboundSink,
    RawOutboundSource,
    SessionEventSink,
    SessionEventSource,
    TransportQueueControl,
    WebSocketConnector,
)
from mirabox_sdk._next.transport.queues import (
    RawInboundQueue,
    RawOutboundQueue,
    SessionEventQueue,
)
from mirabox_sdk._next.transport.session import (
    Connected,
    Disconnected,
    SessionEvent,
    TransportError,
)

PROJECT_ROOT = Path(__file__).parents[2]
NEXT_PACKAGE = PROJECT_ROOT / "src" / "mirabox_sdk" / "_next"
TRANSPORT_PACKAGE = NEXT_PACKAGE / "transport"


def _imported_names(source_file: Path) -> set[str]:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    module_parts = source_file.relative_to(PROJECT_ROOT / "src").with_suffix("").parts
    package = ".".join(module_parts[:-1])
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            relative_module = f"{'.' * node.level}{node.module or ''}"
            imported_from = (
                resolve_name(relative_module, package) if node.level else relative_module
            )
            imported_names.update(f"{imported_from}.{alias.name}" for alias in node.names)
    return imported_names


class CompletionContractTests(unittest.TestCase):
    def test_completion_reports_success_failure_and_timeout(self) -> None:
        for completion_type in (TransportReceipt, CommandFuture):
            with self.subTest(completion_type=completion_type.__name__):
                pending = completion_type()
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=0)

                successful = completion_type()
                successful._finish()
                self.assertTrue(successful.done())
                self.assertIsNone(successful.result(timeout=0))
                self.assertIsNone(successful.exception(timeout=0))

                failure = RuntimeError("send failed")
                failed = completion_type()
                failed._finish(error=failure)
                self.assertIs(failed.exception(timeout=0), failure)
                with self.assertRaises(RuntimeError) as raised:
                    failed.result(timeout=0)
                self.assertIs(raised.exception, failure)

    def test_completion_rejects_every_repeated_terminal_transition(self) -> None:
        for completion_type in (TransportReceipt, CommandFuture):
            for first_error in (None, RuntimeError("first")):
                for second_error in (None, RuntimeError("second")):
                    with self.subTest(
                        completion_type=completion_type.__name__,
                        first_error=first_error,
                        second_error=second_error,
                    ):
                        completion = completion_type()
                        completion._finish(error=first_error)
                        with self.assertRaises(InvalidStateError):
                            completion._finish(error=second_error)

    def test_transport_receipt_wakes_concurrent_waiters(self) -> None:
        receipt = TransportReceipt()
        results: list[None] = []
        waiters = [
            Thread(target=lambda: results.append(receipt.result(timeout=1))) for _ in range(4)
        ]

        for waiter in waiters:
            waiter.start()
        receipt._finish()
        for waiter in waiters:
            waiter.join(1)

        self.assertTrue(all(not waiter.is_alive() for waiter in waiters))
        self.assertEqual(results, [None] * len(waiters))

    def test_transport_receipt_callbacks_preserve_order_and_isolate_failures(self) -> None:
        receipt = TransportReceipt()
        callbacks: list[str] = []
        error = RuntimeError("send failed")

        receipt._add_done_callback(lambda observed: callbacks.append(f"first:{observed}"))

        def fail_callback(_observed: Exception | None) -> None:
            callbacks.append("failed")
            raise RuntimeError("callback failed")

        receipt._add_done_callback(fail_callback)
        receipt._add_done_callback(lambda observed: callbacks.append(f"last:{observed}"))
        with self.assertLogs(
            "mirabox_sdk._next.transport.frames",
            level="ERROR",
        ):
            receipt._finish(error=error)
        receipt._add_done_callback(lambda observed: callbacks.append(f"late:{observed}"))

        self.assertEqual(
            callbacks,
            [
                "first:send failed",
                "failed",
                "last:send failed",
                "late:send failed",
            ],
        )


class BoundaryContractTests(unittest.TestCase):
    def test_transport_and_command_models_retain_typed_values(self) -> None:
        receipt = TransportReceipt()
        frame = OutboundFrame('{"event":"register"}', receipt)
        command = RegisterPluginCommand(event="registerPlugin", uuid="plugin.uuid")
        completion = CommandFuture()
        submission = CommandSubmission(command, completion)

        self.assertEqual(frame.payload, '{"event":"register"}')
        self.assertIs(frame.receipt, receipt)
        self.assertIs(submission.command, command)
        self.assertIs(submission.completion, completion)

    def test_session_events_are_immutable_typed_values(self) -> None:
        connected = Connected()
        disconnected = Disconnected(status_code=1000, reason="normal")
        error = RuntimeError("socket failed")
        transport_error = TransportError(error)

        self.assertIsInstance(connected, SessionEvent)
        self.assertIsInstance(disconnected, SessionEvent)
        self.assertIsInstance(transport_error, SessionEvent)
        self.assertEqual(disconnected.status_code, 1000)
        self.assertEqual(disconnected.reason, "normal")
        self.assertIs(transport_error.error, error)
        with self.assertRaises(FrozenInstanceError):
            disconnected.reason = "changed"  # type: ignore[misc]

    def test_ports_accept_structural_implementations(self) -> None:
        event = StreamDockEvent()
        command = RegisterPluginCommand(event="registerPlugin", uuid="plugin.uuid")
        receipt = TransportReceipt()
        frame = OutboundFrame("{}", receipt)
        session_event = Connected()

        inbound_source = _Source(event)
        inbound_sink = _Sink()
        raw_inbound_source = _Source("{}")
        raw_inbound_sink = _Sink()
        raw_outbound_source = _Source(frame)
        raw_outbound_sink = _Sink()
        command_source = _Source(CommandSubmission(command, CommandFuture()))
        command_sink = _CommandSink()
        session_source = _Source(session_event)
        session_sink = _Sink()
        connector = _Connector()
        boundary = _Boundary(inbound_source, command_sink, session_source)

        contracts_and_values = (
            (InboundEventSource, inbound_source),
            (InboundEventSink, inbound_sink),
            (RawInboundSource, raw_inbound_source),
            (RawInboundSink, raw_inbound_sink),
            (RawOutboundSource, raw_outbound_source),
            (RawOutboundSink, raw_outbound_sink),
            (OutboundCommandSource, command_source),
            (OutboundCommandSink, command_sink),
            (SessionEventSource, session_source),
            (SessionEventSink, session_sink),
            (WebSocketConnector, connector),
            (StreamDockBoundary, boundary),
        )

        for contract, value in contracts_and_values:
            with self.subTest(contract=contract.__name__):
                self.assertIsInstance(value, contract)

        self.assertIs(inbound_source.receive(), event)
        self.assertTrue(inbound_sink.submit(event, timeout=0))
        self.assertIs(command_sink.send_async(command).result(timeout=0), None)
        command_sink.send(command)
        connector.run_forever()
        connector.close()
        self.assertIsInstance(connector.metrics(), WebSocketConnectorMetrics)
        boundary.run_forever()
        boundary.close()

    def test_explicit_implementations_match_port_signatures(self) -> None:
        implementations = (
            (InboundEventSource, InboundEventQueue, ("receive",)),
            (InboundEventSink, InboundEventQueue, ("submit",)),
            (OutboundCommandSource, OutboundCommandQueue, ("receive",)),
            (OutboundCommandSink, OutboundCommandQueue, ("send", "send_async")),
            (
                InboundEventQueueControl,
                InboundEventQueue,
                ("stop_accepting", "drain", "shutdown", "metrics"),
            ),
            (
                OutboundCommandQueueControl,
                OutboundCommandQueue,
                ("stop_accepting", "drain", "shutdown", "metrics"),
            ),
            (EventReaderWorker, EventReader, ("start", "drain", "stop", "metrics")),
            (CommandWriterWorker, CommandWriter, ("start", "drain", "stop", "metrics")),
            (DecodedEventParser, LegacyEventParserAdapter, ("parse",)),
            (StreamDockEventDecoder, JsonStreamDockEventDecoder, ("decode",)),
            (StreamDockCommandEncoder, JsonStreamDockCommandEncoder, ("encode",)),
            (RawInboundSource, RawInboundQueue, ("receive",)),
            (RawInboundSink, RawInboundQueue, ("submit",)),
            (RawOutboundSource, RawOutboundQueue, ("receive",)),
            (RawOutboundSink, RawOutboundQueue, ("submit",)),
            (SessionEventSource, SessionEventQueue, ("receive",)),
            (SessionEventSink, SessionEventQueue, ("submit",)),
            (
                TransportQueueControl,
                RawInboundQueue,
                ("stop_accepting", "drain", "shutdown", "metrics"),
            ),
            (
                TransportQueueControl,
                RawOutboundQueue,
                ("stop_accepting", "drain", "shutdown", "metrics"),
            ),
            (
                TransportQueueControl,
                SessionEventQueue,
                ("stop_accepting", "drain", "shutdown", "metrics"),
            ),
            (
                StreamDockBoundary,
                ComposedStreamDockBoundary,
                ("run_forever", "close", "metrics"),
            ),
        )

        for port, implementation, methods in implementations:
            for method_name in methods:
                with self.subTest(
                    port=port.__name__,
                    implementation=implementation.__name__,
                    method=method_name,
                ):
                    self.assertEqual(
                        signature(getattr(implementation, method_name)),
                        signature(getattr(port, method_name)),
                    )

    def test_concrete_components_explicitly_inherit_their_control_ports(self) -> None:
        implementations = (
            (ComposedStreamDockBoundary, StreamDockBoundary),
            (InboundEventQueue, InboundEventQueueControl),
            (OutboundCommandQueue, OutboundCommandQueueControl),
            (RawInboundQueue, TransportQueueControl),
            (RawOutboundQueue, TransportQueueControl),
            (SessionEventQueue, TransportQueueControl),
            (EventReader, EventReaderWorker),
            (CommandWriter, CommandWriterWorker),
        )

        for implementation, port in implementations:
            with self.subTest(implementation=implementation.__name__, port=port.__name__):
                self.assertIn(port, implementation.__bases__)

    def test_queue_configuration_uses_positive_integer_limits(self) -> None:
        field_names = (
            "raw_inbound_limit",
            "inbound_event_limit",
            "outbound_command_limit",
            "raw_outbound_limit",
            "session_event_limit",
        )
        valid_limits = dict.fromkeys(field_names, 1)
        config = BoundaryQueueConfig(**valid_limits)
        for field_name in field_names:
            self.assertEqual(getattr(config, field_name), 1)

        for field_name in field_names:
            for invalid in (0, -1, True, 1.5, "1"):
                with (
                    self.subTest(field_name=field_name, invalid=invalid),
                    self.assertRaisesRegex(
                        ValueError,
                        f"^{field_name} must be a positive integer$",
                    ),
                ):
                    invalid_limits = {**valid_limits, field_name: invalid}
                    BoundaryQueueConfig(**invalid_limits)  # type: ignore[arg-type]


class PackageIsolationTests(unittest.TestCase):
    def test_next_modules_import_without_starting_threads(self) -> None:
        module_names = (
            "mirabox_sdk._next",
            "mirabox_sdk._next.boundary",
            "mirabox_sdk._next.boundary.composition",
            "mirabox_sdk._next.boundary.config",
            "mirabox_sdk._next.boundary.metrics",
            "mirabox_sdk._next.boundary.ports",
            "mirabox_sdk._next.messaging",
            "mirabox_sdk._next.messaging.inbound",
            "mirabox_sdk._next.messaging.metrics",
            "mirabox_sdk._next.messaging.models",
            "mirabox_sdk._next.messaging.outbound",
            "mirabox_sdk._next.messaging.ports",
            "mirabox_sdk._next.messaging.reader",
            "mirabox_sdk._next.messaging.writer",
            "mirabox_sdk._next.protocol",
            "mirabox_sdk._next.protocol.adapters",
            "mirabox_sdk._next.protocol.adapters.legacy",
            "mirabox_sdk._next.protocol.decoder",
            "mirabox_sdk._next.protocol.encoder",
            "mirabox_sdk._next.protocol.ports",
            "mirabox_sdk._next.transport",
            "mirabox_sdk._next.transport.frames",
            "mirabox_sdk._next.transport.metrics",
            "mirabox_sdk._next.transport.ports",
            "mirabox_sdk._next.transport.queues",
            "mirabox_sdk._next.transport.session",
            "mirabox_sdk._next.transport.websocket",
        )
        script = (
            "import importlib\n"
            "from unittest.mock import patch\n"
            f"modules = {module_names!r}\n"
            "with patch('threading.Thread.start', side_effect=AssertionError("
            "'import started a thread')):\n"
            "    for module in modules:\n"
            "        importlib.import_module(module)\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_next_does_not_import_legacy_connection_or_runtime_modules(self) -> None:
        forbidden_prefixes = tuple(
            f"mirabox_sdk.{module_name}"
            for module_name in (
                "action",
                "action_registry",
                "connection",
                "inbound",
                "outbound",
                "plugin",
                "stores",
            )
        )

        for source_file in NEXT_PACKAGE.rglob("*.py"):
            for imported_name in _imported_names(source_file):
                self.assertFalse(
                    any(
                        imported_name == prefix or imported_name.startswith(f"{prefix}.")
                        for prefix in forbidden_prefixes
                    ),
                    f"{source_file.relative_to(PROJECT_ROOT)} imports {imported_name}",
                )

    def test_transport_layer_has_no_sdk_or_protocol_dependencies(self) -> None:
        for source_file in TRANSPORT_PACKAGE.rglob("*.py"):
            for imported_name in _imported_names(source_file):
                if not imported_name.startswith("mirabox_sdk"):
                    continue
                self.assertTrue(
                    imported_name.startswith("mirabox_sdk._next.transport"),
                    f"{source_file.relative_to(PROJECT_ROOT)} imports {imported_name}",
                )

    def test_existing_parser_dependency_is_confined_to_explicit_adapter(self) -> None:
        parser_importers = {
            source_file.relative_to(NEXT_PACKAGE)
            for source_file in NEXT_PACKAGE.rglob("*.py")
            if any(
                imported_name.startswith("mirabox_sdk.parser")
                for imported_name in _imported_names(source_file)
            )
        }

        self.assertEqual(
            parser_importers,
            {Path("protocol/adapters/legacy.py")},
        )

    def test_ports_are_declared_in_dedicated_port_modules(self) -> None:
        ports = (
            StreamDockBoundary,
            WebSocketConnectorFactory,
            InboundEventSource,
            InboundEventSink,
            OutboundCommandSource,
            OutboundCommandSink,
            MessagingQueueAcceptanceControl,
            InboundEventQueueControl,
            OutboundCommandQueueControl,
            EventReaderWorker,
            CommandWriterWorker,
            DecodedEventParser,
            StreamDockEventDecoder,
            StreamDockCommandEncoder,
            RawInboundSource,
            RawInboundSink,
            RawOutboundSource,
            RawOutboundSink,
            SessionEventSource,
            SessionEventSink,
            TransportQueueAcceptanceControl,
            TransportQueueControl,
            WebSocketConnector,
        )

        for port in ports:
            with self.subTest(port=port.__name__):
                self.assertTrue(port.__module__.endswith(".ports"))

    def test_next_contracts_are_not_part_of_the_stable_public_api(self) -> None:
        private_contracts = {
            "BoundaryQueueConfig",
            "BoundaryShutdownConfig",
            "ComposedStreamDockBoundary",
            "CommandSubmission",
            "CommandWriter",
            "CommandWriterMetrics",
            "CommandWriterWorker",
            "Connected",
            "EventReader",
            "EventReaderMetrics",
            "EventReaderWorker",
            "InboundEventQueue",
            "InboundEventSource",
            "InboundEventQueueControl",
            "InboundEventQueueMetrics",
            "JsonStreamDockCommandEncoder",
            "JsonStreamDockEventDecoder",
            "OutboundCommandSink",
            "OutboundCommandQueue",
            "OutboundCommandQueueControl",
            "OutboundCommandQueueMetrics",
            "OutboundFrame",
            "QueueAcceptanceControl",
            "RawInboundQueue",
            "RawOutboundQueue",
            "SessionEventQueue",
            "SessionEventSource",
            "StreamDockBoundary",
            "StreamDockBoundaryMetrics",
            "StreamDockCommandEncoder",
            "StreamDockEventDecoder",
            "TransportReceipt",
            "TransportQueueControl",
            "TransportQueueMetrics",
            "WebSocketConnector",
            "WebSocketConnectorFactory",
            "WebSocketConnectorMetrics",
        }

        self.assertTrue(private_contracts.isdisjoint(mirabox_sdk.__all__))


class _Source:
    def __init__(self, value: object) -> None:
        self._value = value

    def receive(self, *, timeout: float | None = None) -> object:
        return self._value


class _Sink:
    def submit(self, value: object, *, timeout: float | None = None) -> bool:
        return True


class _CommandSink(OutboundCommandSink):
    def send(self, command: StreamDockCommand) -> None:
        self.send_async(command).result()

    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        completion = CommandFuture()
        completion._finish()
        return completion


class _Connector(WebSocketConnector):
    def run_forever(self) -> None:
        pass

    def close(self) -> None:
        pass

    def metrics(self) -> WebSocketConnectorMetrics:
        return WebSocketConnectorMetrics(
            connect_count=0,
            disconnect_count=0,
            last_close_code=None,
            transport_error_count=0,
            session_events_rejected=0,
            inbound_frames_received=0,
            inbound_frames_forwarded=0,
            inbound_frames_rejected=0,
            binary_frames_rejected=0,
            outbound_frames_received=0,
            outbound_frames_sent=0,
            outbound_send_failures=0,
            outbound_drain_timeouts=0,
            outbound_discarded_during_shutdown=0,
        )


class _Boundary(StreamDockBoundary):
    def __init__(
        self,
        events: InboundEventSource,
        commands: OutboundCommandSink,
        session_events: SessionEventSource,
    ) -> None:
        self._events = events
        self._commands = commands
        self._session_events = session_events

    @property
    def events(self) -> InboundEventSource:
        return self._events

    @property
    def commands(self) -> OutboundCommandSink:
        return self._commands

    @property
    def session_events(self) -> SessionEventSource:
        return self._session_events

    def run_forever(self) -> None:
        pass

    def close(self) -> None:
        pass

    def metrics(self) -> StreamDockBoundaryMetrics:
        raise NotImplementedError

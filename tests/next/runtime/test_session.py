from __future__ import annotations

import unittest
from threading import Event
from time import monotonic

from mirabox_sdk import (
    GetGlobalSettingsCommand,
    RegisterPluginCommand,
    StreamDockCommand,
    StreamDockEvent,
)
from mirabox_sdk._next.runtime.models import DispatchOutcome, DispatchResult, SessionState
from mirabox_sdk._next.runtime.ports import (
    SessionEventCoordinator,
    SessionEventPumpWorker,
    SessionReadiness,
)
from mirabox_sdk._next.runtime.pumps import RuntimeEventPump, SessionEventPump
from mirabox_sdk._next.runtime.scheduler import SequentialHandlerScheduler
from mirabox_sdk._next.runtime.session import SessionCoordinator, SessionReadinessGate
from mirabox_sdk._next.transport.session import Connected, Disconnected, TransportError

from .fakes import (
    FakeInboundEventSource,
    FakeRuntimeEventDispatcher,
    FakeSessionEventSource,
    RecordingCommandSink,
    key_down_event,
)


def _coordinator(sender: RecordingCommandSink) -> SessionCoordinator:
    return SessionCoordinator(
        sender,
        register_event="registerPlugin",
        plugin_uuid="plugin.uuid",
    )


class SessionCoordinatorTests(unittest.TestCase):
    def test_connected_initializes_in_order_before_opening_readiness(self) -> None:
        sender = RecordingCommandSink()
        coordinator = _coordinator(sender)

        self.assertIsInstance(coordinator, SessionEventCoordinator)
        self.assertIsInstance(coordinator.readiness, SessionReadiness)
        self.assertFalse(coordinator.readiness.ready)

        coordinator.handle(Connected())

        self.assertEqual(
            sender.commands,
            [
                RegisterPluginCommand("registerPlugin", "plugin.uuid"),
                GetGlobalSettingsCommand("plugin.uuid"),
            ],
        )
        self.assertIs(coordinator.state, SessionState.READY)
        self.assertTrue(coordinator.readiness.ready)
        metrics = coordinator.metrics()
        self.assertEqual(metrics.events_received, 1)
        self.assertEqual(metrics.connected, 1)
        self.assertEqual(metrics.initialization_started, 1)
        self.assertEqual(metrics.initialization_succeeded, 1)

    def test_registration_failure_is_fatal_and_skips_initial_request(self) -> None:
        sender = RecordingCommandSink()
        failure = RuntimeError("registration secret")
        sender.failures[RegisterPluginCommand] = failure
        coordinator = _coordinator(sender)

        with self.assertLogs("mirabox_sdk._next.runtime.session", level="ERROR") as logs:
            with self.assertRaises(RuntimeError) as raised:
                coordinator.handle(Connected())

        self.assertIs(raised.exception, failure)
        self.assertEqual(sender.commands, [RegisterPluginCommand("registerPlugin", "plugin.uuid")])
        self.assertIs(coordinator.state, SessionState.FAILED)
        self.assertIs(coordinator.readiness.failure, failure)
        self.assertFalse(coordinator.readiness.ready)
        metrics = coordinator.metrics()
        self.assertEqual(metrics.initialization_failed, 1)
        self.assertEqual(metrics.registration_failures, 1)
        self.assertEqual(metrics.initial_settings_request_failures, 0)
        self.assertNotIn("registration secret", "\n".join(logs.output))

    def test_initial_settings_request_failure_preserves_phase_metrics(self) -> None:
        sender = RecordingCommandSink()
        failure = RuntimeError("request secret")
        sender.failures[GetGlobalSettingsCommand] = failure
        coordinator = _coordinator(sender)

        with self.assertLogs("mirabox_sdk._next.runtime.session", level="ERROR") as logs:
            with self.assertRaises(RuntimeError) as raised:
                coordinator.handle(Connected())

        self.assertIs(raised.exception, failure)
        self.assertEqual(len(sender.commands), 2)
        self.assertIs(coordinator.state, SessionState.FAILED)
        metrics = coordinator.metrics()
        self.assertEqual(metrics.initialization_failed, 1)
        self.assertEqual(metrics.registration_failures, 0)
        self.assertEqual(metrics.initial_settings_request_failures, 1)
        self.assertNotIn("request secret", "\n".join(logs.output))

    def test_duplicate_and_out_of_order_transitions_do_not_reinitialize(self) -> None:
        sender = RecordingCommandSink()
        coordinator = _coordinator(sender)

        coordinator.handle(Disconnected(1006, "private close reason"))
        coordinator.handle(Connected())

        self.assertEqual(sender.commands, [])
        self.assertIs(coordinator.state, SessionState.WAITING_CONNECTED)
        self.assertTrue(coordinator.readiness.terminal)
        metrics = coordinator.metrics()
        self.assertEqual(metrics.events_received, 2)
        self.assertEqual(metrics.invalid_transitions, 2)
        self.assertEqual(metrics.disconnected, 1)
        self.assertEqual(metrics.last_close_code, 1006)
        self.assertEqual(coordinator.last_close_reason, "private close reason")

    def test_ready_disconnect_keeps_readiness_latched_and_duplicate_connect_is_ignored(
        self,
    ) -> None:
        sender = RecordingCommandSink()
        coordinator = _coordinator(sender)

        coordinator.handle(Connected())
        coordinator.handle(Disconnected(1000, None))
        coordinator.handle(Connected())

        self.assertIs(coordinator.state, SessionState.DISCONNECTED)
        self.assertTrue(coordinator.readiness.ready)
        self.assertTrue(coordinator.readiness.terminal)
        self.assertEqual(len(sender.commands), 2)
        self.assertEqual(coordinator.metrics().invalid_transitions, 1)

    def test_transport_error_is_observed_without_changing_session_state_or_logging_message(
        self,
    ) -> None:
        sender = RecordingCommandSink()
        coordinator = _coordinator(sender)

        with self.assertLogs("mirabox_sdk._next.runtime.session", level="ERROR") as logs:
            coordinator.handle(TransportError(RuntimeError("private transport detail")))

        self.assertIs(coordinator.state, SessionState.WAITING_CONNECTED)
        self.assertEqual(coordinator.metrics().transport_errors, 1)
        self.assertFalse(coordinator.readiness.terminal)
        self.assertNotIn("private transport detail", "\n".join(logs.output))

    def test_readiness_gate_validates_timeout_and_unblocks_on_terminal_close(self) -> None:
        gate = SessionReadinessGate()

        with self.assertRaisesRegex(ValueError, "timeout"):
            gate.wait(-1)
        gate.close()

        self.assertFalse(gate.wait(0))
        self.assertTrue(gate.terminal)
        self.assertFalse(gate.ready)


class SessionPumpIntegrationTests(unittest.TestCase):
    def test_protocol_event_remains_buffered_until_initialization_completes(self) -> None:
        session_source = FakeSessionEventSource((Connected(),))
        session_source.close()
        command_sink = RecordingCommandSink()
        coordinator = _coordinator(command_sink)
        request_started = Event()
        release_request = Event()

        def block_initial_request(command: StreamDockCommand) -> None:
            if isinstance(command, GetGlobalSettingsCommand):
                request_started.set()
                release_request.wait(2)

        command_sink.on_send = block_initial_request

        event = key_down_event()
        event_source = FakeInboundEventSource((event,))
        event_source.close()
        dispatched: list[StreamDockEvent] = []
        scheduler = SequentialHandlerScheduler(
            FakeRuntimeEventDispatcher(
                lambda received: (
                    dispatched.append(received),
                    DispatchResult(DispatchOutcome.HANDLED),
                )[1]
            )
        )
        event_pump = RuntimeEventPump(
            event_source,
            scheduler,
            poll_interval=0.005,
            readiness_gate=coordinator.readiness,
        )
        session_pump = SessionEventPump(
            session_source,
            coordinator,
            poll_interval=0.005,
        )

        scheduler.start()
        event_pump.start()
        session_pump.start()
        self.assertTrue(request_started.wait(1))
        self.assertEqual(event_source.received, [])
        self.assertEqual(dispatched, [])

        release_request.set()

        self.assertTrue(event_source.wait_for_acknowledged(1, timeout=1))
        self.assertTrue(session_pump.drain(timeout=1))
        self.assertTrue(event_pump.drain(timeout=1))
        self.assertEqual(dispatched, [event])
        self.assertEqual(event_source.acknowledged, [event])
        self.assertEqual(
            command_sink.commands[0],
            RegisterPluginCommand("registerPlugin", "plugin.uuid"),
        )
        self.assertEqual(command_sink.commands[1], GetGlobalSettingsCommand("plugin.uuid"))

    def test_initialization_failure_reaches_observer_and_unblocks_event_pump(self) -> None:
        session_source = FakeSessionEventSource((Connected(),))
        session_source.close()
        command_sink = RecordingCommandSink()
        failure = RuntimeError("registration failed")
        command_sink.failures[RegisterPluginCommand] = failure
        coordinator = _coordinator(command_sink)
        fatal_errors: list[Exception] = []
        event_source = FakeInboundEventSource((key_down_event(),))
        scheduler = SequentialHandlerScheduler(
            FakeRuntimeEventDispatcher(lambda _event: DispatchResult(DispatchOutcome.HANDLED))
        )
        event_pump = RuntimeEventPump(
            event_source,
            scheduler,
            poll_interval=0.005,
            readiness_gate=coordinator.readiness,
        )
        session_pump = SessionEventPump(
            session_source,
            coordinator,
            poll_interval=0.005,
            on_fatal_error=fatal_errors.append,
        )

        scheduler.start()
        event_pump.start()
        session_pump.start()

        self.assertTrue(session_pump.drain(timeout=1))
        self.assertTrue(event_pump.drain(timeout=1))
        self.assertEqual(fatal_errors, [failure])
        self.assertIs(session_pump.failure, failure)
        self.assertIsNone(event_pump.failure)
        self.assertEqual(event_source.received, [])
        self.assertEqual(event_source.acknowledged, [])

    def test_session_source_timeouts_and_terminal_close_are_observable(self) -> None:
        source = FakeSessionEventSource()
        coordinator = _coordinator(RecordingCommandSink())
        pump = SessionEventPump(source, coordinator, poll_interval=0.001)

        self.assertIsInstance(pump, SessionEventPumpWorker)
        pump.start()
        deadline = monotonic() + 1
        poll = Event()
        while pump.metrics().source_poll_timeouts == 0 and monotonic() < deadline:
            poll.wait(0.01)
        source.close()

        self.assertTrue(pump.drain(timeout=1))
        self.assertGreaterEqual(pump.metrics().source_poll_timeouts, 1)
        self.assertEqual(pump.metrics().source_closed, 1)
        self.assertTrue(coordinator.readiness.terminal)
        self.assertIsNone(pump.failure)

    def test_transport_error_does_not_stop_session_pump_before_disconnect_and_close(self) -> None:
        source = FakeSessionEventSource(
            (
                Connected(),
                TransportError(RuntimeError("transport secret")),
                Disconnected(1001, "close secret"),
            )
        )
        source.close()
        coordinator = _coordinator(RecordingCommandSink())
        pump = SessionEventPump(source, coordinator, poll_interval=0.005)

        with self.assertLogs("mirabox_sdk._next.runtime.session", level="INFO") as logs:
            pump.start()
            self.assertTrue(pump.drain(timeout=1))

        self.assertIsNone(pump.failure)
        self.assertIs(coordinator.state, SessionState.DISCONNECTED)
        metrics = pump.metrics()
        self.assertEqual(metrics.events_received, 3)
        self.assertEqual(metrics.transport_errors, 1)
        self.assertEqual(metrics.disconnected, 1)
        self.assertEqual(metrics.source_closed, 1)
        diagnostic_output = "\n".join(logs.output)
        self.assertNotIn("transport secret", diagnostic_output)
        self.assertNotIn("close secret", diagnostic_output)


if __name__ == "__main__":
    unittest.main()

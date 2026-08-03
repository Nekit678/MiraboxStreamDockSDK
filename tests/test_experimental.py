"""Runtime integration tests for the opt-in experimental boundary adapter."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic, sleep

import mirabox_sdk
from mirabox_sdk import (
    Action,
    ActionRegistry,
    CommandFuture,
    JsonObject,
    KeyDownEvent,
    LogMessageCommand,
    OutboundCommandBusClosedError,
    OutboundQueueFullError,
    PluginLaunchArguments,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    StreamDockCommand,
    StreamDockEvent,
    StreamDockPlugin,
    StreamDockSender,
    WillAppearEvent,
)
from mirabox_sdk._next.boundary.metrics import StreamDockBoundaryMetrics
from mirabox_sdk._next.messaging.models import CommandFuture as BoundaryCommandFuture
from mirabox_sdk._next.messaging.outbound import (
    OutboundCommandQueueClosedError,
)
from mirabox_sdk._next.messaging.outbound import (
    OutboundQueueFullError as BoundaryQueueFullError,
)
from mirabox_sdk._next.messaging.ports import InboundEventSourceClosedError
from mirabox_sdk._next.transport.frames import OutboundFrame
from mirabox_sdk._next.transport.metrics import WebSocketConnectorMetrics
from mirabox_sdk._next.transport.ports import (
    RawInboundSink,
    RawOutboundSource,
    SessionEventSink,
    SessionEventSourceClosedError,
    WebSocketConnector,
)
from mirabox_sdk._next.transport.queues import TransportQueueClosedError
from mirabox_sdk._next.transport.session import Connected, Disconnected
from mirabox_sdk.experimental import (
    BoundaryQueueConfig,
    BoundaryShutdownConfig,
    BoundaryStreamDockConnection,
    ExperimentalStreamDockApplication,
    RuntimeDispatcherConfig,
    RuntimeSchedulerKind,
    create_experimental_stream_dock_application,
    create_experimental_stream_dock_connection,
)

_ACTION_UUID = "com.example.experimental.action"


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition was not reached before test timeout")
        sleep(0.005)


def _launch_arguments() -> PluginLaunchArguments:
    return PluginLaunchArguments(
        port=12345,
        plugin_uuid="plugin-uuid",
        register_event="registerPlugin",
        info=RegistrationInfo(
            application=RegistrationApplicationInfo(
                language="en",
                platform="windows",
                platform_version="11",
                version="2.10.179.426",
            ),
            colors=RegistrationColors(),
            device_pixel_ratio=1.0,
            devices=(),
            plugin=RegistrationPluginInfo(uuid="plugin-uuid", version="0.1.0"),
        ),
    )


def _event_frame(event_name: str) -> str:
    payload: JsonObject = {
        "settings": {},
        "coordinates": {"column": 0, "row": 0},
        "isInMultiAction": False,
    }
    if event_name == "willAppear":
        payload["controller"] = "Keypad"
    return json.dumps(
        {
            "event": event_name,
            "action": _ACTION_UUID,
            "context": "button",
            "device": "device-uuid",
            "payload": payload,
        }
    )


def _global_settings_frame() -> str:
    return json.dumps(
        {
            "event": "didReceiveGlobalSettings",
            "payload": {"settings": {"theme": "dark"}},
        }
    )


@dataclass(frozen=True, slots=True)
class _Dependencies:
    stream_dock: StreamDockSender


class _RuntimeAction(Action[JsonObject, _Dependencies]):
    def on_will_appear(self, _event: WillAppearEvent) -> None:
        self.set_title("ready")

    def on_key_down(self, _event: KeyDownEvent) -> None:
        self.set_title("pressed")


class _RuntimeConnector(WebSocketConnector):
    def __init__(
        self,
        raw_inbound: RawInboundSink,
        raw_outbound: RawOutboundSource,
        session_events: SessionEventSink,
        initial_frames: tuple[str, ...],
    ) -> None:
        self._raw_inbound = raw_inbound
        self._raw_outbound = raw_outbound
        self._session_events = session_events
        self._initial_frames = initial_frames
        self._stop_requested = Event()
        self._lock = Lock()
        self.started = Event()
        self.sent: list[str] = []
        self.close_calls = 0
        self._outbound_received = 0

    def run_forever(self) -> None:
        self._session_events.submit(Connected(), timeout=0)
        for frame in self._initial_frames:
            self._raw_inbound.submit(frame, timeout=0)
        self.started.set()

        try:
            while not self._stop_requested.is_set():
                try:
                    frame = self._raw_outbound.receive(timeout=0.01)
                except TimeoutError:
                    continue
                except TransportQueueClosedError:
                    self._stop_requested.wait(0.01)
                    continue
                self._send(frame)
        finally:
            self._session_events.submit(
                Disconnected(status_code=1000, reason="test connector closed"),
                timeout=0,
            )

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
        self._stop_requested.set()

    def metrics(self) -> WebSocketConnectorMetrics:
        with self._lock:
            sent = len(self.sent)
            return WebSocketConnectorMetrics(
                connect_count=1 if self.started.is_set() else 0,
                disconnect_count=1 if self._stop_requested.is_set() else 0,
                last_close_code=1000 if self._stop_requested.is_set() else None,
                transport_error_count=0,
                session_events_rejected=0,
                inbound_frames_received=len(self._initial_frames),
                inbound_frames_forwarded=len(self._initial_frames),
                inbound_frames_rejected=0,
                binary_frames_rejected=0,
                outbound_frames_received=self._outbound_received,
                outbound_frames_sent=sent,
                outbound_send_failures=0,
                outbound_drain_timeouts=0,
                outbound_discarded_during_shutdown=0,
            )

    def _send(self, frame: OutboundFrame) -> None:
        with self._lock:
            self._outbound_received += 1
            self.sent.append(frame.payload)
        frame.receipt._finish()


class _RuntimeConnectorFactory:
    def __init__(self, initial_frames: tuple[str, ...]) -> None:
        self._initial_frames = initial_frames
        self.connector: _RuntimeConnector | None = None

    def __call__(
        self,
        raw_inbound_sink: RawInboundSink,
        raw_outbound_source: RawOutboundSource,
        session_event_sink: SessionEventSink,
    ) -> WebSocketConnector:
        self.connector = _RuntimeConnector(
            raw_inbound_sink,
            raw_outbound_source,
            session_event_sink,
            self._initial_frames,
        )
        return self.connector


class _ClosingListener:
    def __init__(self, connection: BoundaryStreamDockConnection) -> None:
        self._connection = connection
        self.events: list[str] = []

    def on_stream_dock_connected(self) -> None:
        pass

    def on_stream_dock_event(self, event: StreamDockEvent) -> None:
        self.events.append(event.event_name)
        self._connection.close()


class _ClosedInboundEventSource:
    def receive(self, *, timeout: float | None = None) -> StreamDockEvent:
        raise InboundEventSourceClosedError("events closed")

    def task_done(self) -> None:
        raise AssertionError("a closed source has no event to acknowledge")


class _ClosedSessionEventSource:
    def receive(self, *, timeout: float | None = None) -> Connected:
        raise SessionEventSourceClosedError("session events closed")


class _RecordingBoundaryCommandSink:
    def __init__(self) -> None:
        self.completion = BoundaryCommandFuture()
        self.error: Exception | None = None
        self.commands: list[StreamDockCommand] = []

    def send(self, command: StreamDockCommand) -> None:
        self.send_async(command).result()

    def send_async(self, command: StreamDockCommand) -> BoundaryCommandFuture:
        if self.error is not None:
            raise self.error
        self.commands.append(command)
        return self.completion


class _ClosedPortBoundary:
    def __init__(self, commands: _RecordingBoundaryCommandSink | None = None) -> None:
        self._events = _ClosedInboundEventSource()
        self._session_events = _ClosedSessionEventSource()
        self._commands = commands if commands is not None else _RecordingBoundaryCommandSink()

    @property
    def events(self) -> _ClosedInboundEventSource:
        return self._events

    @property
    def commands(self) -> _RecordingBoundaryCommandSink:
        return self._commands

    @property
    def session_events(self) -> _ClosedSessionEventSource:
        return self._session_events

    def run_forever(self) -> None:
        pass

    def close(self) -> None:
        pass

    def metrics(self) -> StreamDockBoundaryMetrics:
        raise AssertionError("metrics are not used by this fake")


class ExperimentalRuntimeIntegrationTests(unittest.TestCase):
    def test_keyed_scheduler_kind_is_explicitly_available_only_from_experimental(self) -> None:
        self.assertEqual(RuntimeSchedulerKind.KEYED_SERIAL.value, "keyed_serial")
        self.assertFalse(hasattr(mirabox_sdk, "RuntimeSchedulerKind"))

    def test_legacy_connection_remains_the_package_default(self) -> None:
        self.assertIs(
            mirabox_sdk.WebSocketStreamDockConnection,
            mirabox_sdk.connection.WebSocketStreamDockConnection,
        )
        self.assertFalse(hasattr(mirabox_sdk, "create_experimental_stream_dock_connection"))
        self.assertFalse(hasattr(mirabox_sdk, "create_experimental_stream_dock_application"))

    def test_dispatcher_stops_on_port_level_source_close_errors(self) -> None:
        connection = BoundaryStreamDockConnection(
            _ClosedPortBoundary(),
            dispatcher_poll_interval=0,
            dispatcher_shutdown_timeout=1,
        )

        connection.run_forever()

    def test_temporary_sender_adapter_preserves_legacy_completion_and_errors(self) -> None:
        commands = _RecordingBoundaryCommandSink()
        connection = BoundaryStreamDockConnection(_ClosedPortBoundary(commands))
        command = LogMessageCommand("hello")

        completion = connection.send_async(command)
        self.assertIsInstance(completion, CommandFuture)
        self.assertFalse(completion.done())
        commands.completion._finish()
        self.assertTrue(completion.wait(timeout=0))
        self.assertIsNone(completion.result(timeout=0))
        self.assertEqual(commands.commands, [command])

        commands.error = BoundaryQueueFullError("full")
        with self.assertRaisesRegex(OutboundQueueFullError, "full"):
            connection.send_async(command)

        commands.error = OutboundCommandQueueClosedError("closed")
        with self.assertRaisesRegex(OutboundCommandBusClosedError, "closed"):
            connection.send_async(command)

    def test_current_runtime_registers_dispatches_and_sends_through_boundary(self) -> None:
        factory = _RuntimeConnectorFactory((_event_frame("willAppear"), _event_frame("keyDown")))
        connection = create_experimental_stream_dock_connection(
            12345,
            queue_config=BoundaryQueueConfig(
                raw_inbound_limit=16,
                inbound_event_limit=16,
                outbound_command_limit=16,
                raw_outbound_limit=16,
                session_event_limit=16,
            ),
            shutdown_config=BoundaryShutdownConfig(
                raw_inbound_drain_timeout=0.5,
                inbound_event_drain_timeout=0.5,
                outbound_command_drain_timeout=0.5,
                raw_outbound_drain_timeout=0.5,
                session_event_drain_timeout=0.5,
                worker_stop_timeout=0.5,
                connector_stop_timeout=0.5,
            ),
            connector_factory=factory,
            dispatcher_poll_interval=0.005,
            dispatcher_shutdown_timeout=1,
        )
        self.assertIsInstance(connection, BoundaryStreamDockConnection)
        assert factory.connector is not None
        connector = factory.connector

        registry: ActionRegistry[_Dependencies] = ActionRegistry()
        registry.register(_ACTION_UUID)(_RuntimeAction)
        runtime = StreamDockPlugin(
            _launch_arguments(),
            stream_dock=connection,
            action_registry=registry,
            action_dependencies=_Dependencies(connection),
        )
        self.assertEqual(runtime.info.application.version, "2.10.179.426")
        errors: list[Exception] = []

        def run() -> None:
            try:
                runtime.run()
            except Exception as exc:
                errors.append(exc)

        runtime_thread = Thread(target=run, name="test-experimental-runtime")
        runtime_thread.start()
        try:
            self.assertTrue(connector.started.wait(1))
            _wait_until(lambda: len(connector.sent) == 4)
        finally:
            connection.close()
            runtime_thread.join(1)
            runtime.stop()

        self.assertFalse(runtime_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            [json.loads(frame) for frame in connector.sent],
            [
                {"event": "registerPlugin", "uuid": "plugin-uuid"},
                {"event": "getGlobalSettings", "context": "plugin-uuid"},
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "ready", "target": 0},
                },
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "pressed", "target": 0},
                },
            ],
        )
        metrics = connection.boundary.metrics()
        self.assertEqual(metrics.inbound_events.acknowledged, 2)
        self.assertEqual(metrics.connector.outbound_frames_sent, 4)

    def test_new_runtime_application_runs_full_pipeline_without_connection_adapter(self) -> None:
        factory = _RuntimeConnectorFactory(
            (
                _global_settings_frame(),
                _event_frame("willAppear"),
                _event_frame("keyDown"),
            )
        )
        registry: ActionRegistry[_Dependencies] = ActionRegistry()
        registry.register(_ACTION_UUID)(_RuntimeAction)

        with self.assertLogs("mirabox_sdk.experimental", level="INFO") as logs:
            application = create_experimental_stream_dock_application(
                _launch_arguments(),
                action_factory=registry,
                action_dependencies_factory=_Dependencies,
                queue_config=BoundaryQueueConfig(
                    raw_inbound_limit=16,
                    inbound_event_limit=16,
                    outbound_command_limit=16,
                    raw_outbound_limit=16,
                    session_event_limit=16,
                ),
                shutdown_config=BoundaryShutdownConfig(
                    raw_inbound_drain_timeout=0.5,
                    inbound_event_drain_timeout=0.5,
                    outbound_command_drain_timeout=0.5,
                    raw_outbound_drain_timeout=0.5,
                    session_event_drain_timeout=0.5,
                    worker_stop_timeout=0.5,
                    connector_stop_timeout=0.5,
                ),
                runtime_config=RuntimeDispatcherConfig(
                    event_poll_interval=0.005,
                    session_poll_interval=0.005,
                ),
                connector_factory=factory,
            )

        self.assertIsInstance(application, ExperimentalStreamDockApplication)
        self.assertNotIsInstance(application, BoundaryStreamDockConnection)
        self.assertFalse(hasattr(application.runtime, "events"))
        self.assertIn("legacy runtime remains the default", "\n".join(logs.output))
        assert factory.connector is not None
        connector = factory.connector
        errors: list[Exception] = []

        def run() -> None:
            try:
                application.run()
            except Exception as exc:
                errors.append(exc)

        runtime_thread = Thread(
            target=run,
            name="test-next-runtime-application",
        )

        runtime_thread.start()
        try:
            self.assertTrue(connector.started.wait(1))
            _wait_until(lambda: len(connector.sent) == 4)
        finally:
            application.stop()
            runtime_thread.join(1)

        self.assertFalse(runtime_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            [json.loads(frame) for frame in connector.sent],
            [
                {"event": "registerPlugin", "uuid": "plugin-uuid"},
                {"event": "getGlobalSettings", "context": "plugin-uuid"},
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "ready", "target": 0},
                },
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "pressed", "target": 0},
                },
            ],
        )
        metrics = application.metrics()
        self.assertEqual(metrics.session.initialization_succeeded, 1)
        self.assertEqual(metrics.event_pump.events_acknowledged, 3)
        self.assertEqual(metrics.actions.global_settings_updates, 1)
        self.assertEqual(metrics.actions.global_settings_replays, 1)
        self.assertEqual(metrics.boundary.connector.outbound_frames_sent, 4)

    def test_close_from_runtime_callback_does_not_deadlock(self) -> None:
        factory = _RuntimeConnectorFactory(('{"event":"systemDidWakeUp"}',))
        connection = create_experimental_stream_dock_connection(
            12345,
            connector_factory=factory,
            dispatcher_poll_interval=0.005,
            dispatcher_shutdown_timeout=1,
        )
        listener = _ClosingListener(connection)
        connection.set_listener(listener)
        errors: list[Exception] = []

        def run() -> None:
            try:
                connection.run_forever()
            except Exception as exc:
                errors.append(exc)

        connection_thread = Thread(target=run, name="test-experimental-callback-close")
        connection_thread.start()
        connection_thread.join(1)

        self.assertFalse(connection_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(listener.events, ["systemDidWakeUp"])
        self.assertEqual(connection.boundary.metrics().inbound_events.acknowledged, 1)


if __name__ == "__main__":
    unittest.main()

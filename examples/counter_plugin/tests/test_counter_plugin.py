"""Integration tests for the minimal counter example."""

from __future__ import annotations

import json
import os
import unittest
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from unittest.mock import Mock, patch

from counter_plugin import bootstrap
from counter_plugin.action_registry import ACTION_REGISTRY
from counter_plugin.actions.counter import ACTION_UUID, CounterAction
from counter_plugin.contracts import ActionDependencies

from mirabox_sdk import (
    JsonObject,
    PluginLaunchArguments,
    PropertyInspectorMessage,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    SendToPluginEvent,
)
from mirabox_sdk._next.transport.frames import OutboundFrame
from mirabox_sdk._next.transport.metrics import WebSocketConnectorMetrics
from mirabox_sdk._next.transport.ports import (
    RawInboundSink,
    RawOutboundSource,
    SessionEventSink,
    WebSocketConnector,
)
from mirabox_sdk._next.transport.queues import TransportQueueClosedError
from mirabox_sdk._next.transport.session import Connected, Disconnected
from mirabox_sdk.experimental import (
    BoundaryQueueConfig,
    BoundaryShutdownConfig,
    RuntimeDispatcherConfig,
    create_experimental_stream_dock_application,
)

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition was not reached before test timeout")
        sleep(0.005)


def _launch_arguments() -> PluginLaunchArguments:
    return PluginLaunchArguments(
        port=12345,
        plugin_uuid="com.example.counter",
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
            plugin=RegistrationPluginInfo(uuid="com.example.counter", version="0.1.0"),
        ),
    )


def _counter_frames() -> tuple[str, ...]:
    identity: JsonObject = {
        "action": ACTION_UUID,
        "context": "button",
        "device": "device-uuid",
    }
    action_payload: JsonObject = {
        "settings": {},
        "coordinates": {"column": 0, "row": 0},
        "isInMultiAction": False,
    }
    return tuple(
        json.dumps(frame)
        for frame in (
            {
                "event": "didReceiveGlobalSettings",
                "payload": {"settings": {"profile": "integration"}},
            },
            {
                "event": "willAppear",
                **identity,
                "payload": {**action_payload, "controller": "Keypad"},
            },
            {
                "event": "keyDown",
                **identity,
                "payload": action_payload,
            },
            {
                "event": "sendToPlugin",
                "action": ACTION_UUID,
                "context": "button",
                "payload": {"event": "reset"},
            },
        )
    )


class _CounterConnector(WebSocketConnector):
    def __init__(
        self,
        raw_inbound: RawInboundSink,
        raw_outbound: RawOutboundSource,
        session_events: SessionEventSink,
    ) -> None:
        self._raw_inbound = raw_inbound
        self._raw_outbound = raw_outbound
        self._session_events = session_events
        self._stop_requested = Event()
        self._lock = Lock()
        self.started = Event()
        self.sent: list[str] = []
        self.close_calls = 0
        self._outbound_received = 0

    def run_forever(self) -> None:
        self._session_events.submit(Connected(), timeout=0)
        for frame in _counter_frames():
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
            return WebSocketConnectorMetrics(
                connect_count=1 if self.started.is_set() else 0,
                disconnect_count=1 if self._stop_requested.is_set() else 0,
                last_close_code=1000 if self._stop_requested.is_set() else None,
                transport_error_count=0,
                session_events_rejected=0,
                inbound_frames_received=4,
                inbound_frames_forwarded=4,
                inbound_frames_rejected=0,
                binary_frames_rejected=0,
                outbound_frames_received=self._outbound_received,
                outbound_frames_sent=len(self.sent),
                outbound_send_failures=0,
                outbound_drain_timeouts=0,
                outbound_discarded_during_shutdown=0,
            )

    def _send(self, frame: OutboundFrame) -> None:
        with self._lock:
            self._outbound_received += 1
            self.sent.append(frame.payload)
        frame.receipt._finish()


class _CounterConnectorFactory:
    def __init__(self) -> None:
        self.connector: _CounterConnector | None = None

    def __call__(
        self,
        raw_inbound_sink: RawInboundSink,
        raw_outbound_source: RawOutboundSource,
        session_event_sink: SessionEventSink,
    ) -> WebSocketConnector:
        self.connector = _CounterConnector(
            raw_inbound_sink,
            raw_outbound_source,
            session_event_sink,
        )
        return self.connector


class CounterActionTests(unittest.TestCase):
    def test_increments_and_resets_persisted_count(self) -> None:
        stream_dock = Mock()
        action = CounterAction(
            ACTION_UUID,
            "button",
            {},
            ActionDependencies(stream_dock),
        )

        action.on_will_appear(Mock())
        action.on_key_down(Mock())
        action.on_send_to_plugin(
            SendToPluginEvent(
                action=ACTION_UUID,
                context="button",
                message=PropertyInspectorMessage(
                    name="reset",
                    value={"event": "reset"},
                ),
            )
        )

        self.assertEqual(action.settings, {"count": 0})
        wires = [call.args[0].to_wire() for call in stream_dock.send.call_args_list]
        self.assertIn(
            {"event": "setSettings", "context": "button", "payload": {"count": 1}},
            wires,
        )
        self.assertEqual(wires[-1]["payload"]["title"], "0")


class CounterExperimentalRuntimeIntegrationTests(unittest.TestCase):
    def test_registration_global_settings_actions_outbound_and_shutdown(self) -> None:
        connector_factory = _CounterConnectorFactory()
        application = create_experimental_stream_dock_application(
            _launch_arguments(),
            action_factory=ACTION_REGISTRY,
            action_dependencies_factory=ActionDependencies,
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
            connector_factory=connector_factory,
        )
        assert connector_factory.connector is not None
        connector = connector_factory.connector
        errors: list[Exception] = []

        def run() -> None:
            try:
                application.run()
            except Exception as exc:
                errors.append(exc)

        runtime_thread = Thread(target=run, name="test-counter-next-runtime")
        runtime_thread.start()
        try:
            self.assertTrue(connector.started.wait(1))
            _wait_until(lambda: len(connector.sent) == 7)
        finally:
            application.stop()
            runtime_thread.join(1)

        self.assertFalse(runtime_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(connector.close_calls, 1)
        self.assertEqual(
            [json.loads(frame) for frame in connector.sent],
            [
                {"event": "registerPlugin", "uuid": "com.example.counter"},
                {"event": "getGlobalSettings", "context": "com.example.counter"},
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "0", "target": 0},
                },
                {
                    "event": "setSettings",
                    "context": "button",
                    "payload": {"count": 1},
                },
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "1", "target": 0},
                },
                {
                    "event": "setSettings",
                    "context": "button",
                    "payload": {"count": 0},
                },
                {
                    "event": "setTitle",
                    "context": "button",
                    "payload": {"title": "0", "target": 0},
                },
            ],
        )
        metrics = application.metrics()
        self.assertEqual(metrics.session.initialization_succeeded, 1)
        self.assertEqual(metrics.event_pump.events_acknowledged, 4)
        self.assertEqual(metrics.actions.action_instances_created, 1)
        self.assertEqual(metrics.actions.global_settings_updates, 1)
        self.assertEqual(metrics.actions.global_settings_replays, 1)
        self.assertEqual(metrics.boundary.connector.outbound_frames_sent, 7)


class CounterBundleTests(unittest.TestCase):
    def test_manifest_references_existing_files(self) -> None:
        bundle = EXAMPLE_ROOT / "com.example.counter.sdPlugin"
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["CodePath"], "CounterPlugin.exe")
        for action in manifest["Actions"]:
            self.assertTrue((bundle / action["Icon"]).is_file())
            self.assertTrue((bundle / action["PropertyInspectorPath"]).is_file())

    def test_property_inspector_client_matches_installed_sdk(self) -> None:
        from mirabox_sdk import property_inspector_client_bytes

        client = (
            EXAMPLE_ROOT / "com.example.counter.sdPlugin" / "property-inspector" / "mirabox-sdk.js"
        )

        self.assertEqual(client.read_bytes(), property_inspector_client_bytes())


class CounterBootstrapTests(unittest.TestCase):
    def test_uses_legacy_connection_by_default(self) -> None:
        arguments = Mock(port=12345)
        connection = Mock()
        application = Mock()

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                bootstrap,
                "WebSocketStreamDockConnection",
                return_value=connection,
            ) as connection_factory,
            patch.object(bootstrap, "Plugin", return_value=application) as plugin_factory,
        ):
            result = bootstrap.build_application(arguments)

        self.assertIs(result, application)
        connection_factory.assert_called_once_with(12345)
        plugin_factory.assert_called_once_with(arguments, stream_dock=connection)

    def test_uses_boundary_only_after_explicit_opt_in(self) -> None:
        arguments = Mock(port=12345)
        connection = Mock()
        application = Mock()

        with (
            patch.dict(
                os.environ,
                {bootstrap.EXPERIMENTAL_BOUNDARY_ENV: "1"},
                clear=True,
            ),
            patch(
                "mirabox_sdk.experimental.create_experimental_stream_dock_connection",
                return_value=connection,
            ) as connection_factory,
            patch.object(bootstrap, "Plugin", return_value=application) as plugin_factory,
        ):
            result = bootstrap.build_application(arguments)

        self.assertIs(result, application)
        connection_factory.assert_called_once_with(12345)
        plugin_factory.assert_called_once_with(arguments, stream_dock=connection)

    def test_new_runtime_opt_in_takes_precedence_over_boundary_adapter(self) -> None:
        arguments = Mock(port=12345)
        application = Mock()

        with (
            patch.dict(
                os.environ,
                {
                    bootstrap.EXPERIMENTAL_RUNTIME_ENV: "1",
                    bootstrap.EXPERIMENTAL_BOUNDARY_ENV: "1",
                },
                clear=True,
            ),
            patch(
                "mirabox_sdk.experimental.create_experimental_stream_dock_application",
                return_value=application,
            ) as application_factory,
            patch.object(bootstrap, "WebSocketStreamDockConnection") as legacy_factory,
            patch.object(bootstrap, "Plugin") as plugin_factory,
        ):
            result = bootstrap.build_application(arguments)

        self.assertIs(result, application)
        application_factory.assert_called_once_with(
            arguments,
            action_factory=bootstrap.ACTION_REGISTRY,
            action_dependencies_factory=ActionDependencies,
        )
        legacy_factory.assert_not_called()
        plugin_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

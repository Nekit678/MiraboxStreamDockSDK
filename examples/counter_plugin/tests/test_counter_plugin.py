"""Integration tests for the minimal counter example."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from counter_plugin import bootstrap
from counter_plugin.action_registry import ACTION_REGISTRY
from counter_plugin.actions.counter import ACTION_UUID, CounterAction
from counter_plugin.contracts import ActionDependencies

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    DidReceiveGlobalSettingsEvent,
    JsonObject,
    LogMessageCommand,
    Plugin,
    PluginLaunchArguments,
    PropertyInspectorMessage,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    RuntimeDispatcherConfig,
    SendToPluginEvent,
    StreamDockQueueConfig,
    StreamDockShutdownConfig,
    WillAppearEvent,
)
from mirabox_sdk.testing import FakeStreamDockSender, StreamDockHarness

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]


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


class _CounterService:
    def __init__(self) -> None:
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.started = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.started = False


class CounterActionTests(unittest.TestCase):
    def test_increments_and_resets_persisted_count(self) -> None:
        stream_dock = FakeStreamDockSender()
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
        wires = stream_dock.messages
        self.assertIn(
            {"event": "setSettings", "context": "button", "payload": {"count": 1}},
            wires,
        )
        self.assertEqual(wires[-1]["payload"]["title"], "0")


class CounterRuntimeIntegrationTests(unittest.TestCase):
    def test_plugin_factory_shares_context_and_manages_session_lifecycle(self) -> None:
        contexts: list[ApplicationContext] = []
        action_contexts: list[ApplicationContext] = []
        history: list[str] = []
        readiness: list[bool] = []
        received_settings: list[JsonObject] = []

        class SessionPlugin(Plugin):
            def __init__(self, context: ApplicationContext) -> None:
                self.context = context
                contexts.append(context)

            def on_ready(self) -> None:
                history.append("plugin.ready")
                readiness.append(self.context.session_readiness.ready)
                self.context.stream_dock.send(LogMessageCommand("session ready"))
                self.context.global_settings.set({"ready": True})

            def on_did_receive_global_settings(self, event: DidReceiveGlobalSettingsEvent) -> None:
                history.append("plugin.settings")
                received_settings.append(self.context.global_settings.snapshot())

            def on_stop(self) -> None:
                history.append("plugin.stop")

        class SessionService(_CounterService):
            def start(self) -> None:
                history.append("service.start")
                super().start()

            def stop(self) -> None:
                history.append("service.stop")
                super().stop()

        def build_service(context: ApplicationContext) -> SessionService:
            contexts.append(context)
            return SessionService()

        def build_dependencies(context: ApplicationContext) -> ApplicationContext:
            contexts.append(context)
            return context

        registry = ActionRegistry[ApplicationContext]()

        @registry.register(ACTION_UUID)
        class SessionAction(Action[JsonObject, ApplicationContext]):
            def on_will_appear(self, event: WillAppearEvent) -> None:
                history.append("action.appear")
                action_contexts.append(self.dependencies)

            def on_will_disappear(self) -> None:
                history.append("action.stop")

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=build_dependencies,
            plugin_factory=SessionPlugin,
            service_factories=(build_service,),
        )
        application = harness.application
        with harness:
            for frame in _counter_frames():
                harness.send_json(frame)
            harness.wait_for_events(4)

        self.assertEqual(len(contexts), 3)
        self.assertEqual(len(action_contexts), 1)
        self.assertTrue(all(context is contexts[0] for context in (*contexts, *action_contexts)))
        self.assertIs(contexts[0].global_settings, application.global_settings)
        self.assertEqual(readiness, [True])
        self.assertEqual(received_settings, [{"profile": "integration"}])
        self.assertEqual(
            history,
            [
                "service.start",
                "plugin.ready",
                "plugin.settings",
                "action.appear",
                "action.stop",
                "plugin.stop",
                "service.stop",
            ],
        )
        self.assertEqual(
            [message["event"] for message in harness.messages],
            ["registerPlugin", "getGlobalSettings", "logMessage", "setGlobalSettings"],
        )

    def test_registration_global_settings_actions_outbound_and_shutdown(self) -> None:
        service = _CounterService()
        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=ACTION_REGISTRY,
            action_dependencies_factory=bootstrap.build_dependencies,
            queue_config=StreamDockQueueConfig(
                raw_inbound_limit=16,
                inbound_event_limit=16,
                outbound_command_limit=16,
                raw_outbound_limit=16,
                session_event_limit=16,
            ),
            shutdown_config=StreamDockShutdownConfig(
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
            services=(service,),
        )
        application = harness.application
        with harness:
            self.assertTrue(service.started)
            harness.assert_registration()
            for frame in _counter_frames():
                harness.send_json(frame)
            harness.wait_for_events(4)
            harness.assert_command("setSettings", context="button", payload={"count": 0})

        self.assertEqual(service.start_calls, 1)
        self.assertEqual(service.stop_calls, 1)
        self.assertFalse(service.started)
        self.assertEqual(
            list(harness.messages),
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
        settings = application.global_settings.snapshot()
        settings["profile"] = "mutated"
        self.assertEqual(application.global_settings.snapshot(), {"profile": "integration"})


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
    def test_uses_production_runtime_by_default(self) -> None:
        arguments = Mock(port=12345)
        application = Mock()

        with (
            patch.object(
                bootstrap,
                "create_stream_dock_application",
                return_value=application,
            ) as application_factory,
        ):
            result = bootstrap.build_application(arguments)

        self.assertIs(result, application)
        application_factory.assert_called_once_with(
            arguments,
            action_factory=bootstrap.ACTION_REGISTRY,
            action_dependencies_factory=bootstrap.build_dependencies,
        )


if __name__ == "__main__":
    unittest.main()

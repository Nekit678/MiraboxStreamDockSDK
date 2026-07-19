"""Tests for the reusable action and plugin runtime in the MiraBox SDK."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import Mock, call

from mirabox_sdk import (
    Action,
    ActionRegistry,
    Controller,
    Coordinates,
    DidReceiveGlobalSettingsEvent,
    GetGlobalSettingsCommand,
    InvalidPluginLaunchArgumentsError,
    JsonCodecEncodeError,
    JsonObject,
    KeyDownEvent,
    PluginLaunchArguments,
    RegisterPluginCommand,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    StreamDockPlugin,
    StreamDockSender,
    SystemDidWakeUpEvent,
    WillAppearEvent,
    parse_plugin_cli_arguments,
    run_plugin_cli,
)

ACTION_UUID = "com.example.counter"
REGISTRATION_INFO_JSON = (
    '{"application":{"language":"en","platform":"windows",'
    '"platformVersion":"11","version":"2.10"},"colors":{},'
    '"devicePixelRatio":1,"devices":[],"plugin":'
    '{"uuid":"plugin-uuid","version":"0.1.0"}}'
)


@dataclass(frozen=True, slots=True)
class ExampleDependencies:
    stream_dock: StreamDockSender


class RecordingAction(Action[JsonObject, ExampleDependencies]):
    def __init__(
        self,
        action: str,
        context: str,
        settings: JsonObject,
        dependencies: ExampleDependencies,
    ) -> None:
        super().__init__(action, context, settings, dependencies)
        self.received_events: list[object | None] = []

    def on_will_appear(self, event: WillAppearEvent) -> None:
        self.received_events.append(event)

    def on_key_down(self, event: KeyDownEvent) -> None:
        self.received_events.append(event)

    def on_did_receive_global_settings(self, event: DidReceiveGlobalSettingsEvent) -> None:
        self.received_events.append(event)

    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
        self.received_events.append(event)

    def on_will_disappear(self, event=None) -> None:
        self.received_events.append(event)


class FailingAction(RecordingAction):
    last_instance: FailingAction | None = None

    def __init__(
        self,
        action: str,
        context: str,
        settings: JsonObject,
        dependencies: ExampleDependencies,
    ) -> None:
        super().__init__(action, context, settings, dependencies)
        FailingAction.last_instance = self

    def on_will_appear(self, event: WillAppearEvent) -> None:
        super().on_will_appear(event)
        raise RuntimeError("appearance failed")


class FailingBroadcastAction(RecordingAction):
    def on_did_receive_global_settings(self, _event: DidReceiveGlobalSettingsEvent) -> None:
        raise RuntimeError("global settings failed")

    def on_system_did_wake_up(self, _event: SystemDidWakeUpEvent) -> None:
        raise RuntimeError("system wake-up failed")


def launch_arguments() -> PluginLaunchArguments:
    return PluginLaunchArguments(
        port=12345,
        plugin_uuid="plugin-uuid",
        register_event="registerPlugin",
        info=RegistrationInfo(
            application=RegistrationApplicationInfo(
                language="en",
                platform="windows",
                platform_version="11",
                version="2.10",
            ),
            colors=RegistrationColors(),
            device_pixel_ratio=1.0,
            devices=(),
            plugin=RegistrationPluginInfo(uuid="plugin-uuid", version="0.1.0"),
        ),
    )


def will_appear_event() -> WillAppearEvent:
    return WillAppearEvent(
        action=ACTION_UUID,
        context="button",
        device="device-uuid",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


class ActionTests(unittest.TestCase):
    def test_set_settings_preserves_state_when_encoding_fails(self) -> None:
        stream_dock = Mock()
        action = RecordingAction(
            ACTION_UUID,
            "button",
            {"count": 1},
            ExampleDependencies(stream_dock),
        )
        invalid_settings = {"count": object()}

        with self.assertRaises(JsonCodecEncodeError):
            action.set_settings(invalid_settings)  # type: ignore[arg-type]

        self.assertEqual(action.settings, {"count": 1})
        stream_dock.send.assert_not_called()


class ActionRegistryTests(unittest.TestCase):
    def test_registrations_are_isolated_per_plugin(self) -> None:
        first: ActionRegistry[ExampleDependencies] = ActionRegistry()
        second: ActionRegistry[ExampleDependencies] = ActionRegistry()
        first.register(ACTION_UUID)(RecordingAction)
        dependencies = ExampleDependencies(Mock())

        action = first.create(ACTION_UUID, "button", {"count": 1}, dependencies)

        self.assertIsInstance(action, RecordingAction)
        self.assertEqual(action.settings, {"count": 1})
        self.assertIsNone(second.create(ACTION_UUID, "button", {}, dependencies))
        self.assertEqual(first.action_uuids, frozenset({ACTION_UUID}))
        self.assertEqual(second.action_uuids, frozenset())

    def test_rejects_empty_and_duplicate_action_uuids(self) -> None:
        registry: ActionRegistry[ExampleDependencies] = ActionRegistry()

        with self.assertRaisesRegex(ValueError, "must not be empty"):
            registry.register(" ")

        registry.register(ACTION_UUID)(RecordingAction)
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(ACTION_UUID)(RecordingAction)


class StreamDockPluginRuntimeTests(unittest.TestCase):
    def build_runtime(self, *services: Mock) -> tuple[StreamDockPlugin, Mock]:
        stream_dock = Mock()
        registry: ActionRegistry[ExampleDependencies] = ActionRegistry()
        registry.register(ACTION_UUID)(RecordingAction)
        runtime = StreamDockPlugin(
            launch_arguments(),
            stream_dock=stream_dock,
            action_registry=registry,
            action_dependencies=ExampleDependencies(stream_dock),
            services=services,
        )
        return runtime, stream_dock

    def test_registers_and_dispatches_events_without_plugin_specific_code(self) -> None:
        runtime, stream_dock = self.build_runtime()
        appear = will_appear_event()
        key_down = KeyDownEvent(
            action=ACTION_UUID,
            context="button",
            device="device-uuid",
            settings={"count": 1},
            coordinates=Coordinates(0, 0),
            is_in_multi_action=False,
        )

        runtime.on_stream_dock_connected()
        runtime.on_stream_dock_event(appear)
        runtime.on_stream_dock_event(key_down)

        action = runtime.actions["button"]
        self.assertIsInstance(action, RecordingAction)
        self.assertEqual(action.received_events, [appear, key_down])
        self.assertEqual(
            stream_dock.send.call_args_list,
            [
                call(RegisterPluginCommand("registerPlugin", "plugin-uuid")),
                call(GetGlobalSettingsCommand("plugin-uuid")),
            ],
        )
        stream_dock.set_listener.assert_called_once_with(runtime)

    def test_starts_services_and_stops_them_in_reverse_order_once(self) -> None:
        events: list[str] = []
        first = Mock()
        second = Mock()
        first.start.side_effect = lambda: events.append("start-first")
        first.stop.side_effect = lambda: events.append("stop-first")
        second.start.side_effect = lambda: events.append("start-second")
        second.stop.side_effect = lambda: events.append("stop-second")
        runtime, stream_dock = self.build_runtime(first, second)
        runtime.on_stream_dock_event(will_appear_event())
        action = runtime.actions["button"]

        runtime.run()
        runtime.stop()
        runtime.stop()

        self.assertEqual(
            events,
            ["start-first", "start-second", "stop-second", "stop-first"],
        )
        self.assertEqual(action.received_events[-1], None)
        stream_dock.run_forever.assert_called_once_with()
        stream_dock.close.assert_called_once_with()

    def test_stops_only_services_that_started_successfully(self) -> None:
        first = Mock()
        failing = Mock()
        failing.start.side_effect = RuntimeError("cannot start")
        runtime, stream_dock = self.build_runtime(first, failing)

        with self.assertRaisesRegex(RuntimeError, "cannot start"):
            runtime.run()
        runtime.stop()

        first.stop.assert_called_once_with()
        failing.stop.assert_not_called()
        stream_dock.run_forever.assert_not_called()
        stream_dock.close.assert_called_once_with()

    def test_cannot_run_runtime_more_than_once_or_after_stop(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.run()

        with self.assertRaisesRegex(RuntimeError, "already been run"):
            runtime.run()

        runtime.stop()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            runtime.run()

    def test_rolls_back_action_when_appearance_fails(self) -> None:
        stream_dock = Mock()
        registry: ActionRegistry[ExampleDependencies] = ActionRegistry()
        registry.register(ACTION_UUID)(FailingAction)
        runtime = StreamDockPlugin(
            launch_arguments(),
            stream_dock=stream_dock,
            action_registry=registry,
            action_dependencies=ExampleDependencies(stream_dock),
        )

        with self.assertLogs("mirabox_sdk.plugin", level="ERROR"):
            runtime.on_stream_dock_event(will_appear_event())

        self.assertNotIn("button", runtime.actions)
        action = FailingAction.last_instance
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.received_events[-1], None)

    def test_broadcast_failure_does_not_block_other_actions(self) -> None:
        runtime, stream_dock = self.build_runtime()
        dependencies = ExampleDependencies(stream_dock)
        failing_action = FailingBroadcastAction(
            "com.example.failing-broadcast",
            "failing-button",
            {},
            dependencies,
        )
        healthy_action = RecordingAction(
            ACTION_UUID,
            "healthy-button",
            {},
            dependencies,
        )
        runtime.actions = {
            failing_action.context: failing_action,
            healthy_action.context: healthy_action,
        }
        global_settings = DidReceiveGlobalSettingsEvent(settings={"theme": "dark"})
        system_wake_up = SystemDidWakeUpEvent()

        with self.assertLogs("mirabox_sdk.plugin", level="ERROR") as logs:
            runtime.on_stream_dock_event(global_settings)
            runtime.on_stream_dock_event(system_wake_up)

        self.assertEqual(
            healthy_action.received_events,
            [global_settings, system_wake_up],
        )
        self.assertEqual(runtime.global_settings, {"theme": "dark"})
        self.assertTrue(all("failing-button" in message for message in logs.output))

    def test_replays_empty_global_settings_to_action_created_after_response(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        global_settings = DidReceiveGlobalSettingsEvent(settings={})
        appear = will_appear_event()

        runtime.on_stream_dock_event(global_settings)
        runtime.on_stream_dock_event(appear)

        action = runtime.actions["button"]
        self.assertEqual(action.received_events, [appear, global_settings])

    def test_replays_only_latest_global_settings_to_late_action(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        first = DidReceiveGlobalSettingsEvent(settings={"theme": "light"})
        latest = DidReceiveGlobalSettingsEvent(settings={"theme": "dark"})
        appear = will_appear_event()

        runtime.on_stream_dock_event(first)
        runtime.on_stream_dock_event(latest)
        runtime.on_stream_dock_event(appear)

        action = runtime.actions["button"]
        self.assertEqual(action.received_events, [appear, latest])
        self.assertEqual(runtime.global_settings, {"theme": "dark"})

    def test_isolates_saved_global_settings_from_event_mutation(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        event = DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.5}})

        runtime.on_stream_dock_event(event)
        audio = event.settings["audio"]
        assert isinstance(audio, dict)
        audio["threshold"] = 0.75
        runtime.on_stream_dock_event(will_appear_event())

        self.assertEqual(runtime.global_settings, {"audio": {"threshold": 0.5}})
        action = runtime.actions["button"]
        self.assertEqual(
            action.received_events[-1],
            DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.5}}),
        )

    def test_isolates_saved_global_settings_from_setter_input(self) -> None:
        runtime, stream_dock = self.build_runtime()
        settings: JsonObject = {"audio": {"threshold": 0.5}}

        runtime.set_global_settings(settings)
        settings["audio"] = {"threshold": 0.75}

        self.assertEqual(runtime.global_settings, {"audio": {"threshold": 0.5}})
        sent_command = stream_dock.send.call_args.args[0]
        self.assertEqual(sent_command.settings, {"audio": {"threshold": 0.5}})
        sent_audio = sent_command.settings["audio"]
        assert isinstance(sent_audio, dict)
        sent_audio["threshold"] = 0.25
        self.assertEqual(runtime.global_settings, {"audio": {"threshold": 0.5}})


class PluginCliTests(unittest.TestCase):
    def test_parses_standard_plugin_launch_arguments(self) -> None:
        arguments = parse_plugin_cli_arguments(
            [
                "-port",
                "12345",
                "-pluginUUID",
                "plugin-uuid",
                "-registerEvent",
                "registerPlugin",
                "-info",
                REGISTRATION_INFO_JSON,
            ]
        )

        self.assertEqual(arguments, launch_arguments())

    def test_reports_invalid_info_json_as_a_typed_launch_error(self) -> None:
        with self.assertRaises(InvalidPluginLaunchArgumentsError) as caught:
            parse_plugin_cli_arguments(
                [
                    "-port",
                    "12345",
                    "-pluginUUID",
                    "plugin-uuid",
                    "-registerEvent",
                    "registerPlugin",
                    "-info",
                    "not-json",
                ]
            )

        self.assertEqual(caught.exception.path, ("info",))
        self.assertIn("invalid JSON", caught.exception.reason)

    def test_runs_and_stops_built_application(self) -> None:
        application = Mock()
        factory = Mock(return_value=application)

        result = run_plugin_cli(
            factory,
            [
                "-port",
                "12345",
                "-pluginUUID",
                "plugin-uuid",
                "-registerEvent",
                "registerPlugin",
                "-info",
                REGISTRATION_INFO_JSON,
            ],
        )

        self.assertEqual(result, 0)
        factory.assert_called_once_with(launch_arguments())
        application.run.assert_called_once_with()
        application.stop.assert_called_once_with()

    def test_returns_failure_and_still_stops_after_runtime_error(self) -> None:
        application = Mock()
        application.run.side_effect = RuntimeError("runtime failed")

        with self.assertLogs("mirabox_sdk.cli", level="ERROR"):
            result = run_plugin_cli(
                Mock(return_value=application),
                [
                    "-port",
                    "12345",
                    "-pluginUUID",
                    "plugin-uuid",
                    "-registerEvent",
                    "registerPlugin",
                    "-info",
                    REGISTRATION_INFO_JSON,
                ],
            )

        self.assertEqual(result, 1)
        application.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

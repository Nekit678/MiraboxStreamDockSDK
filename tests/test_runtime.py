"""Tests for the reusable action and plugin runtime in the MiraBox SDK."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from unittest.mock import Mock, call, patch

from mirabox_sdk import (
    JSON_OBJECT_CODEC,
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
    SetGlobalSettingsCommand,
    StreamDockPlugin,
    StreamDockSender,
    SystemDidWakeUpEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    parse_plugin_cli_arguments,
    run_plugin_cli,
)
from mirabox_sdk.json_types import (
    _clone_json_object_source,
    _copy_on_write_json_object,
    _prepare_copy_on_write_json_object,
    clone_json_object,
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


class RecordingUnhandledEventPlugin(StreamDockPlugin[ExampleDependencies]):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.unhandled_events: list[UnknownStreamDockEvent] = []

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        self.unhandled_events.append(event)


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


def will_appear_event(*, context: str = "button") -> WillAppearEvent:
    return WillAppearEvent(
        action=ACTION_UUID,
        context=context,
        device="device-uuid",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


class CopyOnWriteJsonTests(unittest.TestCase):
    def test_keeps_wide_containers_lazy_during_selective_access(self) -> None:
        settings: JsonObject = {
            **{f"key-{index}": index for index in range(10_000)},
            "items": list(range(10_000)),
        }
        source = _prepare_copy_on_write_json_object(settings)
        view = _copy_on_write_json_object(source)
        root_storage_size = dict.__sizeof__(view)

        self.assertEqual(view["key-5000"], 5000)
        items = view["items"]
        assert isinstance(items, list)
        source_items = settings["items"]
        assert isinstance(source_items, list)
        list_storage_size = list.__sizeof__(items)
        self.assertEqual(items[5000], 5000)
        items[5000] = -1

        self.assertEqual(dict.__sizeof__(view), root_storage_size)
        self.assertLess(root_storage_size, dict.__sizeof__(settings))
        self.assertEqual(list.__sizeof__(items), list_storage_size)
        self.assertLess(list_storage_size, list.__sizeof__(source_items))
        self.assertEqual(items[5000], -1)
        self.assertEqual(source_items[5000], 5000)

    def test_serializes_lazy_empty_and_mutated_containers(self) -> None:
        settings: JsonObject = {
            "empty_object": {},
            "empty_list": [],
            "profile": {"levels": [1, 2]},
        }
        view = _copy_on_write_json_object(_prepare_copy_on_write_json_object(settings))
        profile = view["profile"]
        assert isinstance(profile, dict)
        levels = profile["levels"]
        assert isinstance(levels, list)
        levels.append(3)
        del view["empty_object"]
        view["empty_object"] = {"enabled": True}

        expected: JsonObject = {
            "empty_list": [],
            "profile": {"levels": [1, 2, 3]},
            "empty_object": {"enabled": True},
        }
        self.assertEqual(json.loads(json.dumps(view)), expected)
        self.assertEqual(view, expected)
        self.assertEqual(
            settings,
            {
                "empty_object": {},
                "empty_list": [],
                "profile": {"levels": [1, 2]},
            },
        )

    def test_keeps_scalar_mapping_views_dynamic_after_mutation(self) -> None:
        view = _copy_on_write_json_object(_prepare_copy_on_write_json_object({"enabled": True}))
        items = view.items()
        values = view.values()

        view["enabled"] = False
        view["count"] = 1

        self.assertEqual(list(items), [("enabled", False), ("count", 1)])
        self.assertEqual(list(values), [False, 1])


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

    def test_set_settings_preserves_state_when_send_fails(self) -> None:
        stream_dock = Mock()
        stream_dock.send.side_effect = RuntimeError("send failed")
        action = RecordingAction(
            ACTION_UUID,
            "button",
            {"count": 1},
            ExampleDependencies(stream_dock),
        )

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            action.set_settings({"count": 2})

        self.assertEqual(action.settings, {"count": 1})

    def test_set_settings_isolates_local_state_from_input_and_command(self) -> None:
        stream_dock = Mock()
        action = RecordingAction(
            ACTION_UUID,
            "button",
            {"nested": {"value": 0}},
            ExampleDependencies(stream_dock),
        )
        settings: JsonObject = {"nested": {"value": 1}}

        action.set_settings(settings)

        command = stream_dock.send.call_args.args[0]
        command_settings = command.settings
        input_nested = settings["nested"]
        command_nested = command_settings["nested"]
        local_nested = action.settings["nested"]
        assert isinstance(input_nested, dict)
        assert isinstance(command_nested, dict)
        assert isinstance(local_nested, dict)

        input_nested["value"] = 2
        command_nested["value"] = 3

        self.assertEqual(local_nested["value"], 1)
        self.assertEqual(input_nested["value"], 2)
        self.assertEqual(command_nested["value"], 3)

    def test_set_settings_reuses_one_owned_snapshot_for_local_state(self) -> None:
        stream_dock = Mock()
        action = RecordingAction(
            ACTION_UUID,
            "button",
            {"items": []},
            ExampleDependencies(stream_dock),
        )
        settings: JsonObject = {"items": list(range(100))}

        with (
            patch(
                "mirabox_sdk.codecs._clone_json_object_source",
                wraps=_clone_json_object_source,
            ) as own_payload,
            patch(
                "mirabox_sdk.codecs.clone_json_object",
                wraps=clone_json_object,
            ) as clone,
        ):
            action.set_settings(settings)

        own_payload.assert_called_once()
        clone.assert_not_called()
        self.assertEqual(action.settings, settings)


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

    def test_delivers_unknown_event_to_plugin_hook_once(self) -> None:
        stream_dock = Mock()
        runtime = RecordingUnhandledEventPlugin(
            launch_arguments(),
            stream_dock=stream_dock,
            action_registry=ActionRegistry(),
            action_dependencies=ExampleDependencies(stream_dock),
        )
        event = UnknownStreamDockEvent(
            event="futureEvent",
            data={"event": "futureEvent", "payload": {"version": 2}},
        )

        runtime.on_stream_dock_event(event)

        self.assertEqual(runtime.unhandled_events, [event])

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

    def test_replays_global_settings_updated_by_setter(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings={"theme": "light"}))

        runtime.set_global_settings({"theme": "dark"})
        runtime.on_stream_dock_event(will_appear_event())

        action = runtime.actions["button"]
        self.assertEqual(
            action.received_events[-1],
            DidReceiveGlobalSettingsEvent(settings={"theme": "dark"}),
        )

    def test_replays_global_settings_updated_by_typed_setter(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings={"theme": "light"}))

        runtime.set_typed_global_settings({"theme": "dark"}, JSON_OBJECT_CODEC)
        runtime.on_stream_dock_event(will_appear_event())

        action = runtime.actions["button"]
        self.assertEqual(
            action.received_events[-1],
            DidReceiveGlobalSettingsEvent(settings={"theme": "dark"}),
        )

    def test_replays_global_settings_set_before_first_response(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        appear = will_appear_event()

        runtime.set_global_settings({"theme": "dark"})
        runtime.on_stream_dock_event(appear)

        action = runtime.actions["button"]
        self.assertEqual(
            action.received_events,
            [appear, DidReceiveGlobalSettingsEvent(settings={"theme": "dark"})],
        )

    def test_replays_typed_global_settings_set_before_first_response(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        appear = will_appear_event()

        runtime.set_typed_global_settings({"theme": "dark"}, JSON_OBJECT_CODEC)
        runtime.on_stream_dock_event(appear)

        action = runtime.actions["button"]
        self.assertEqual(
            action.received_events,
            [appear, DidReceiveGlobalSettingsEvent(settings={"theme": "dark"})],
        )

    def test_isolates_global_settings_broadcasts_between_actions(self) -> None:
        runtime, stream_dock = self.build_runtime()
        dependencies = ExampleDependencies(stream_dock)
        first = RecordingAction(ACTION_UUID, "first-button", {}, dependencies)
        second = RecordingAction(ACTION_UUID, "second-button", {}, dependencies)
        runtime.actions = {first.context: first, second.context: second}

        runtime.on_stream_dock_event(
            DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.5}})
        )

        first_event = first.received_events[-1]
        second_event = second.received_events[-1]
        assert isinstance(first_event, DidReceiveGlobalSettingsEvent)
        assert isinstance(second_event, DidReceiveGlobalSettingsEvent)
        first_audio = first_event.settings["audio"]
        assert isinstance(first_audio, dict)
        first_audio["threshold"] = 0.75
        self.assertIsNot(first_event, second_event)
        self.assertEqual(second_event.settings, {"audio": {"threshold": 0.5}})
        self.assertEqual(runtime.global_settings, {"audio": {"threshold": 0.5}})

    def test_clones_global_settings_once_for_all_actions(self) -> None:
        runtime, stream_dock = self.build_runtime()
        dependencies = ExampleDependencies(stream_dock)
        runtime.actions = {
            f"button-{index}": RecordingAction(
                ACTION_UUID,
                f"button-{index}",
                {},
                dependencies,
            )
            for index in range(64)
        }
        settings: JsonObject = {
            "profiles": [
                {"name": f"profile-{index}", "levels": list(range(100))} for index in range(16)
            ]
        }

        with (
            patch(
                "mirabox_sdk.plugin.clone_json_object",
                wraps=clone_json_object,
            ) as clone,
            patch(
                "mirabox_sdk.plugin._prepare_copy_on_write_json_object",
                wraps=_prepare_copy_on_write_json_object,
            ) as prepare,
        ):
            runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings=settings))

        self.assertEqual(clone.call_count, 1)
        self.assertEqual(prepare.call_count, 1)
        received = [action.received_events[-1] for action in runtime.actions.values()]
        self.assertEqual(len({id(event) for event in received}), 64)

    def test_replays_global_settings_without_cloning_snapshot(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(
            DidReceiveGlobalSettingsEvent(settings={"profiles": [{"level": 1}]})
        )

        with (
            patch(
                "mirabox_sdk.plugin.clone_json_object",
                wraps=clone_json_object,
            ) as clone,
            patch(
                "mirabox_sdk.plugin._prepare_copy_on_write_json_object",
                wraps=_prepare_copy_on_write_json_object,
            ) as prepare,
        ):
            runtime.on_stream_dock_event(will_appear_event())

        clone.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(
            runtime.actions["button"].received_events[-1],
            DidReceiveGlobalSettingsEvent(settings={"profiles": [{"level": 1}]}),
        )

    def test_copies_only_mutated_global_settings_containers(self) -> None:
        runtime, stream_dock = self.build_runtime()
        dependencies = ExampleDependencies(stream_dock)
        first = RecordingAction(ACTION_UUID, "first-button", {}, dependencies)
        second = RecordingAction(ACTION_UUID, "second-button", {}, dependencies)
        runtime.actions = {first.context: first, second.context: second}

        runtime.on_stream_dock_event(
            DidReceiveGlobalSettingsEvent(settings={"profiles": [{"levels": [1, 2, 3]}]})
        )

        first_event = first.received_events[-1]
        second_event = second.received_events[-1]
        assert isinstance(first_event, DidReceiveGlobalSettingsEvent)
        assert isinstance(second_event, DidReceiveGlobalSettingsEvent)
        first_profiles = first_event.settings["profiles"]
        assert isinstance(first_profiles, list)
        first_profile = first_profiles[0]
        assert isinstance(first_profile, dict)
        first_levels = first_profile["levels"]
        assert isinstance(first_levels, list)
        first_levels.append(4)

        self.assertEqual(
            first_event.settings,
            {"profiles": [{"levels": [1, 2, 3, 4]}]},
        )
        self.assertEqual(
            json.loads(json.dumps(first_event.settings)),
            {"profiles": [{"levels": [1, 2, 3, 4]}]},
        )
        self.assertEqual(
            second_event.settings,
            {"profiles": [{"levels": [1, 2, 3]}]},
        )
        self.assertEqual(
            runtime.global_settings,
            {"profiles": [{"levels": [1, 2, 3]}]},
        )

    def test_isolates_global_settings_replays_between_late_actions(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(
            DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.5}})
        )
        runtime.on_stream_dock_event(will_appear_event(context="first-button"))
        first = runtime.actions["first-button"]
        first_event = first.received_events[-1]
        assert isinstance(first_event, DidReceiveGlobalSettingsEvent)
        first_audio = first_event.settings["audio"]
        assert isinstance(first_audio, dict)
        first_audio["threshold"] = 0.75

        runtime.on_stream_dock_event(will_appear_event(context="second-button"))

        second = runtime.actions["second-button"]
        second_event = second.received_events[-1]
        assert isinstance(second_event, DidReceiveGlobalSettingsEvent)
        self.assertIsNot(first_event, second_event)
        self.assertEqual(second_event.settings, {"audio": {"threshold": 0.5}})
        self.assertEqual(runtime.global_settings, {"audio": {"threshold": 0.5}})

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

    def test_replays_mutated_runtime_global_settings_without_changing_old_events(
        self,
    ) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(
            DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.5}})
        )
        runtime.on_stream_dock_event(will_appear_event(context="first-button"))
        first_event = runtime.actions["first-button"].received_events[-1]

        audio = runtime.global_settings["audio"]
        assert isinstance(audio, dict)
        audio["threshold"] = 0.75
        runtime.on_stream_dock_event(will_appear_event(context="second-button"))

        assert isinstance(first_event, DidReceiveGlobalSettingsEvent)
        self.assertEqual(first_event.settings, {"audio": {"threshold": 0.5}})
        self.assertEqual(
            runtime.actions["second-button"].received_events[-1],
            DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.75}}),
        )

    def test_batches_runtime_global_settings_mutations_before_snapshot_clone(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings={"items": []}))
        items = runtime.global_settings["items"]
        assert isinstance(items, list)

        with patch(
            "mirabox_sdk.plugin.clone_json_object",
            wraps=clone_json_object,
        ) as clone:
            for item in range(100):
                items.append(item)

            clone.assert_not_called()
            runtime.on_stream_dock_event(will_appear_event())

        clone.assert_called_once()
        self.assertEqual(
            runtime.actions["button"].received_events[-1],
            DidReceiveGlobalSettingsEvent(settings={"items": list(range(100))}),
        )

    def test_rejects_invalid_runtime_global_settings_mutation_atomically(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        expected: JsonObject = {"items": [1]}
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings=expected))
        items = runtime.global_settings["items"]
        assert isinstance(items, list)

        with patch(
            "mirabox_sdk.plugin.clone_json_object",
            wraps=clone_json_object,
        ) as clone:
            with self.assertRaisesRegex(ValueError, "expected a JSON value"):
                items.append(object())  # type: ignore[arg-type]
            runtime.on_stream_dock_event(will_appear_event())

        clone.assert_not_called()
        self.assertEqual(runtime.global_settings, expected)
        self.assertEqual(
            runtime.actions["button"].received_events[-1],
            DidReceiveGlobalSettingsEvent(settings=expected),
        )

    def test_isolates_runtime_global_settings_mutation_inputs(self) -> None:
        runtime, _stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings={}))
        profile: JsonObject = {"levels": [1]}

        runtime.global_settings["profile"] = profile
        levels = profile["levels"]
        assert isinstance(levels, list)
        levels.append(2)
        runtime.on_stream_dock_event(will_appear_event())

        expected: JsonObject = {"profile": {"levels": [1]}}
        self.assertEqual(runtime.global_settings, expected)
        self.assertEqual(
            runtime.actions["button"].received_events[-1],
            DidReceiveGlobalSettingsEvent(settings=expected),
        )

    def test_updates_runtime_global_settings_in_one_transaction(self) -> None:
        runtime, stream_dock = self.build_runtime()
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings={"items": [0]}))

        def append_items(settings: JsonObject) -> None:
            items = settings["items"]
            assert isinstance(items, list)
            for item in range(1, 101):
                items.append(item)

        with (
            patch(
                "mirabox_sdk.codecs._clone_json_object_source",
                wraps=_clone_json_object_source,
            ) as own_payload,
            patch(
                "mirabox_sdk.plugin.clone_json_object",
                wraps=clone_json_object,
            ) as clone,
            patch(
                "mirabox_sdk.plugin._prepare_copy_on_write_json_object",
                wraps=_prepare_copy_on_write_json_object,
            ) as prepare,
        ):
            runtime.update_global_settings(append_items)
            runtime.on_stream_dock_event(will_appear_event())

        own_payload.assert_called_once()
        clone.assert_not_called()
        prepare.assert_not_called()
        expected: JsonObject = {"items": list(range(101))}
        self.assertEqual(runtime.global_settings, expected)
        self.assertEqual(
            stream_dock.send.call_args.args[0],
            SetGlobalSettingsCommand("plugin-uuid", expected),
        )
        self.assertEqual(
            runtime.actions["button"].received_events[-1],
            DidReceiveGlobalSettingsEvent(settings=expected),
        )

    def test_rolls_back_runtime_global_settings_transaction_on_callback_failure(
        self,
    ) -> None:
        runtime, stream_dock = self.build_runtime()
        expected: JsonObject = {"items": [1]}
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings=expected))
        original = runtime.global_settings

        def fail_after_mutation(settings: JsonObject) -> None:
            items = settings["items"]
            assert isinstance(items, list)
            items.append(2)
            raise RuntimeError("update failed")

        with self.assertRaisesRegex(RuntimeError, "update failed"):
            runtime.update_global_settings(fail_after_mutation)

        self.assertIs(runtime.global_settings, original)
        self.assertEqual(runtime.global_settings, expected)
        stream_dock.send.assert_not_called()

    def test_rolls_back_runtime_global_settings_transaction_on_invalid_value(
        self,
    ) -> None:
        runtime, stream_dock = self.build_runtime()
        expected: JsonObject = {"items": [1]}
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings=expected))
        original = runtime.global_settings

        def append_invalid_value(settings: JsonObject) -> None:
            items = settings["items"]
            assert isinstance(items, list)
            items.append(object())  # type: ignore[arg-type]

        with self.assertRaisesRegex(ValueError, "expected a JSON value"):
            runtime.update_global_settings(append_invalid_value)

        self.assertIs(runtime.global_settings, original)
        self.assertEqual(runtime.global_settings, expected)
        stream_dock.send.assert_not_called()

    def test_rolls_back_runtime_global_settings_transaction_on_send_failure(
        self,
    ) -> None:
        runtime, stream_dock = self.build_runtime()
        expected: JsonObject = {"items": [1]}
        runtime.on_stream_dock_event(DidReceiveGlobalSettingsEvent(settings=expected))
        original = runtime.global_settings
        stream_dock.send.side_effect = RuntimeError("send failed")

        def append_item(settings: JsonObject) -> None:
            items = settings["items"]
            assert isinstance(items, list)
            items.append(2)

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            runtime.update_global_settings(append_item)

        self.assertIs(runtime.global_settings, original)
        self.assertEqual(runtime.global_settings, expected)

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

    def test_set_global_settings_preserves_state_when_validation_fails(self) -> None:
        runtime, stream_dock = self.build_runtime()
        runtime.global_settings = {"threshold": 0.5}

        with self.assertRaises(JsonCodecEncodeError):
            runtime.set_global_settings({"threshold": float("nan")})

        self.assertEqual(runtime.global_settings, {"threshold": 0.5})
        stream_dock.send.assert_not_called()

    def test_set_global_settings_preserves_state_when_send_fails(self) -> None:
        runtime, stream_dock = self.build_runtime()
        runtime.global_settings = {"theme": "light"}
        stream_dock.send.side_effect = RuntimeError("send failed")

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            runtime.set_global_settings({"theme": "dark"})

        self.assertEqual(runtime.global_settings, {"theme": "light"})

    def test_set_typed_global_settings_preserves_state_when_send_fails(self) -> None:
        runtime, stream_dock = self.build_runtime()
        runtime.global_settings = {"theme": "light"}
        stream_dock.send.side_effect = RuntimeError("send failed")

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            runtime.set_typed_global_settings({"theme": "dark"}, JSON_OBJECT_CODEC)

        self.assertEqual(runtime.global_settings, {"theme": "light"})


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

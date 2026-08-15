"""Tests for the reusable action and plugin runtime in the MiraBox SDK."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from unittest.mock import Mock, call, patch

from mirabox_sdk import (
    Action,
    ActionRegistry,
    CommandFuture,
    Controller,
    Coordinates,
    DidReceiveGlobalSettingsEvent,
    InvalidPluginLaunchArgumentsError,
    JsonCodecEncodeError,
    JsonObject,
    KeyDownEvent,
    PluginLaunchArguments,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    SetImageCommand,
    SetStateCommand,
    SetTitleCommand,
    StreamDockSender,
    SystemDidWakeUpEvent,
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
    def test_async_display_helpers_return_command_futures(self) -> None:
        stream_dock = Mock()
        futures = (CommandFuture(), CommandFuture(), CommandFuture())
        stream_dock.send_async.side_effect = futures
        action = RecordingAction(
            ACTION_UUID,
            "button",
            {"count": 1},
            ExampleDependencies(stream_dock),
        )

        returned = (
            action.set_state_async(2),
            action.set_title_async("Count", target=1, state=2),
            action.set_image_async("data:image/png;base64,abc", target=1, state=2),
        )

        self.assertEqual(returned, futures)
        self.assertEqual(
            stream_dock.send_async.call_args_list,
            [
                call(SetStateCommand("button", 2)),
                call(SetTitleCommand("button", "Count", target=1, state=2)),
                call(
                    SetImageCommand(
                        "button",
                        "data:image/png;base64,abc",
                        target=1,
                        state=2,
                    )
                ),
            ],
        )
        stream_dock.send.assert_not_called()

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

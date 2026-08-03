"""Tests for the internal typed MiraBox SDK package."""

from __future__ import annotations

import ast
import json
import unittest
from collections.abc import Callable
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from threading import Event, Thread, get_ident
from time import monotonic, sleep
from unittest.mock import Mock, patch

import mirabox_sdk
from mirabox_sdk import (
    JSON_OBJECT_CODEC,
    Action,
    CommandFuture,
    Controller,
    Coordinates,
    DeviceDidDisconnectEvent,
    DeviceInfo,
    DeviceSize,
    DialRotateEvent,
    DidReceiveGlobalSettingsEvent,
    FunctionalJsonCodec,
    GetSettingsCommand,
    InvalidFieldError,
    InvalidRegistrationInfoError,
    JsonCodecDecodeError,
    JsonCodecEncodeError,
    JsonObject,
    JsonObjectCodec,
    KeyDownEvent,
    LogMessageCommand,
    MalformedEventError,
    OutboundCommandBusClosedError,
    OutboundQueueFullError,
    OwnedJsonPayload,
    PluginLaunchArguments,
    PropertyInspectorMessage,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationDeviceInfo,
    RegistrationInfo,
    RegistrationPluginInfo,
    SendToPluginEvent,
    SendToPropertyInspectorCommand,
    SetGlobalSettingsCommand,
    SetSettingsCommand,
    SetStateCommand,
    SetTitleCommand,
    StreamDockCommand,
    StreamDockEventType,
    TitleAlignment,
    TitleParameters,
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    UnknownStreamDockEvent,
    UnsupportedEventError,
    ValidatedJsonObject,
    ValidatedWireMessage,
    WillAppearEvent,
    configure_logging,
    decode_with_codec,
    encode_with_codec,
    parse_plugin_launch_arguments,
    parse_registration_info,
    parse_stream_dock_event,
)
from mirabox_sdk.connection import WebSocketStreamDockConnection
from mirabox_sdk.events import EventScope
from mirabox_sdk.inbound import InboundOverflowPolicy
from mirabox_sdk.json_types import clone_json_object
from mirabox_sdk.parser import EVENT_REGISTRY
from mirabox_sdk.plugin import StreamDockPlugin
from mirabox_sdk.protocols import StreamDockConnection


@dataclass(frozen=True, slots=True)
class ExampleSettings:
    channel_id: str


def decode_example_settings(value: JsonObject) -> ExampleSettings:
    channel_id = value.get("channelId")
    if not isinstance(channel_id, str):
        raise JsonCodecDecodeError("expected string", path=("channelId",))
    return ExampleSettings(channel_id)


def encode_example_settings(value: ExampleSettings) -> JsonObject:
    return {"channelId": value.channel_id}


EXAMPLE_SETTINGS_CODEC = FunctionalJsonCodec(
    decoder=decode_example_settings,
    encoder=encode_example_settings,
)


def wait_for(predicate: Callable[[], bool], *, timeout: float = 1.0) -> bool:
    """Return whether a predicate became true before timeout."""

    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            return False
        sleep(0.001)
    return True


def dial_rotate_message(context: str, *, ticks: int = 1) -> str:
    return json.dumps(
        {
            "event": "dialRotate",
            "action": "action-uuid",
            "context": context,
            "device": "device-uuid",
            "payload": {
                "settings": {},
                "coordinates": {"column": 0, "row": 0},
                "ticks": ticks,
                "pressed": False,
            },
        }
    )


def key_down_message(context: str, *, sequence: int = 0) -> str:
    return json.dumps(
        {
            "event": "keyDown",
            "action": "action-uuid",
            "context": context,
            "device": "device-uuid",
            "payload": {
                "settings": {"sequence": sequence},
                "coordinates": {"column": 0, "row": 0},
                "isInMultiAction": False,
            },
        }
    )


def will_appear_message(context: str) -> str:
    return json.dumps(
        {
            "event": "willAppear",
            "action": "action-uuid",
            "context": context,
            "device": "device-uuid",
            "payload": {
                "settings": {},
                "coordinates": {"column": 0, "row": 0},
                "controller": "Keypad",
                "isInMultiAction": False,
            },
        }
    )


def registration_info_data() -> JsonObject:
    return {
        "application": {
            "font": "HarmonyOS Sans",
            "language": "en",
            "platform": "windows",
            "platformVersion": "11",
            "version": "2.10",
        },
        "colors": {"highlightColor": "#0078FFFF"},
        "devicePixelRatio": 1.25,
        "devices": [
            {
                "id": "device-uuid",
                "name": "N4ProE",
                "type": 0,
                "size": {"columns": 5, "rows": 3},
            }
        ],
        "plugin": {"uuid": "plugin-uuid", "version": "0.1.0"},
    }


class MiraBoxSdkPackageTests(unittest.TestCase):
    def test_does_not_import_wave_link_plugin_implementation(self) -> None:
        sdk_directory = Path(__file__).resolve().parents[1] / "src" / "mirabox_sdk"

        for path in sdk_directory.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertLessEqual(node.level, 1)
                        self.assertFalse((node.module or "").startswith("wave_link_plugin"))
                    elif isinstance(node, ast.Import):
                        self.assertFalse(
                            any(name.name.startswith("wave_link_plugin") for name in node.names)
                        )


class StreamDockRegistrationTests(unittest.TestCase):
    def test_parses_typed_registration_info(self) -> None:
        info = parse_registration_info(registration_info_data())

        self.assertEqual(
            info,
            RegistrationInfo(
                application=RegistrationApplicationInfo(
                    language="en",
                    platform="windows",
                    platform_version="11",
                    version="2.10",
                    font="HarmonyOS Sans",
                ),
                colors=RegistrationColors(highlight_color="#0078FFFF"),
                device_pixel_ratio=1.25,
                devices=(
                    RegistrationDeviceInfo(
                        id="device-uuid",
                        name="N4ProE",
                        type=0,
                        size=DeviceSize(columns=5, rows=3),
                    ),
                ),
                plugin=RegistrationPluginInfo(uuid="plugin-uuid", version="0.1.0"),
            ),
        )

    def test_builds_typed_plugin_launch_arguments(self) -> None:
        arguments = parse_plugin_launch_arguments(
            port=12345,
            plugin_uuid="plugin-uuid",
            register_event="registerPlugin",
            info=registration_info_data(),
        )

        self.assertEqual(
            arguments,
            PluginLaunchArguments(
                port=12345,
                plugin_uuid="plugin-uuid",
                register_event="registerPlugin",
                info=parse_registration_info(registration_info_data()),
            ),
        )

    def test_reports_exact_invalid_registration_path(self) -> None:
        data = registration_info_data()
        devices = data["devices"]
        self.assertIsInstance(devices, list)
        devices[0]["size"]["rows"] = 0

        with self.assertRaises(InvalidRegistrationInfoError) as caught:
            parse_registration_info(data)

        self.assertEqual(caught.exception.path, ("devices", 0, "size", "rows"))
        self.assertEqual(caught.exception.reason, "expected positive integer")

    def test_keeps_runtime_and_manifest_plugin_uuids_separate(self) -> None:
        arguments = parse_plugin_launch_arguments(
            port=12345,
            plugin_uuid="runtime-registration-uuid",
            register_event="registerPlugin",
            info=registration_info_data(),
        )

        self.assertEqual(arguments.plugin_uuid, "runtime-registration-uuid")
        self.assertEqual(arguments.info.plugin.uuid, "plugin-uuid")


class JsonCodecTests(unittest.TestCase):
    def test_validated_json_object_creates_isolated_owned_payloads(self) -> None:
        source: JsonObject = {"profile": {"levels": [1, 2]}}
        validated = ValidatedJsonObject(source)
        first = validated.owned_payload()
        second = validated.owned_payload()

        source_profile = source["profile"]
        first_profile = first["profile"]
        assert isinstance(source_profile, dict)
        assert isinstance(first_profile, dict)
        source_profile["levels"] = [3]
        first_profile["levels"] = [4]

        self.assertEqual(second, {"profile": {"levels": [1, 2]}})
        self.assertEqual(first.isolated_copy(), {"profile": {"levels": [4]}})
        self.assertIsInstance(first, OwnedJsonPayload)

    def test_validated_wire_message_requires_owned_payload_for_shallow_composition(
        self,
    ) -> None:
        with self.assertRaisesRegex(TypeError, "OwnedJsonPayload"):
            ValidatedWireMessage.from_owned_payload(  # type: ignore[arg-type]
                {"invalid": object()},
                event="customEvent",
            )

    def test_decodes_typed_settings_from_event(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "keyDown",
                "action": "action-uuid",
                "context": "button",
                "device": "device-uuid",
                "payload": {
                    "settings": {"channelId": "microphone"},
                    "coordinates": {"column": 0, "row": 0},
                    "isInMultiAction": False,
                },
            }
        )
        self.assertIsInstance(event, KeyDownEvent)

        settings = event.decode_settings(EXAMPLE_SETTINGS_CODEC)

        self.assertEqual(settings, ExampleSettings("microphone"))

    def test_codec_error_includes_event_settings_path(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "keyDown",
                "action": "action-uuid",
                "context": "button",
                "device": "device-uuid",
                "payload": {
                    "settings": {"channelId": 7},
                    "coordinates": {"column": 0, "row": 0},
                    "isInMultiAction": False,
                },
            }
        )
        self.assertIsInstance(event, KeyDownEvent)

        with self.assertRaises(JsonCodecDecodeError) as caught:
            event.decode_settings(EXAMPLE_SETTINGS_CODEC)

        self.assertEqual(caught.exception.event_name, "keyDown")
        self.assertEqual(
            caught.exception.path,
            ("payload", "settings", "channelId"),
        )

    def test_builds_settings_command_from_typed_object(self) -> None:
        command = SetSettingsCommand.from_settings(
            "button",
            ExampleSettings("microphone"),
            EXAMPLE_SETTINGS_CODEC,
        )

        self.assertEqual(
            command.to_wire(),
            {
                "event": "setSettings",
                "context": "button",
                "payload": {"channelId": "microphone"},
            },
        )

    def test_global_settings_command_preserves_dataclass_serialization(self) -> None:
        command = SetGlobalSettingsCommand(
            "plugin",
            {"profiles": [{"level": 1}]},
        )

        self.assertEqual(
            asdict(command),
            {
                "context": "plugin",
                "settings": {"profiles": [{"level": 1}]},
            },
        )

    def test_extensible_commands_own_direct_payloads_and_validate_mutations(self) -> None:
        source: JsonObject = {"nested": {"value": 1}}
        settings_command = SetSettingsCommand("button", source)
        global_command = SetGlobalSettingsCommand("plugin", source)
        inspector_command = SendToPropertyInspectorCommand("action", "button", source)

        source_nested = source["nested"]
        assert isinstance(source_nested, dict)
        source_nested["value"] = 2

        self.assertEqual(settings_command.settings, {"nested": {"value": 1}})
        self.assertEqual(global_command.settings, {"nested": {"value": 1}})
        self.assertEqual(inspector_command.payload, {"nested": {"value": 1}})
        with self.assertRaises(ValueError):
            settings_command.settings["invalid"] = object()  # type: ignore[assignment]

    def test_converts_typed_property_inspector_payloads_both_ways(self) -> None:
        event = SendToPluginEvent(
            action="action-uuid",
            context="button",
            message=PropertyInspectorMessage(
                name="selectChannel",
                value={"channelId": "microphone"},
            ),
        )

        decoded = event.decode_message(EXAMPLE_SETTINGS_CODEC)
        command = SendToPropertyInspectorCommand.from_payload(
            "action-uuid",
            "button",
            decoded,
            EXAMPLE_SETTINGS_CODEC,
        )

        self.assertEqual(decoded, ExampleSettings("microphone"))
        self.assertEqual(
            command.to_wire(),
            {
                "event": "sendToPropertyInspector",
                "action": "action-uuid",
                "context": "button",
                "payload": {"channelId": "microphone"},
            },
        )

    def test_rejects_non_json_codec_output(self) -> None:
        invalid_codec = FunctionalJsonCodec(
            decoder=decode_example_settings,
            encoder=lambda _value: {"invalid": object()},  # type: ignore[dict-item]
        )

        with self.assertRaises(JsonCodecEncodeError):
            SetSettingsCommand.from_settings(
                "button",
                ExampleSettings("microphone"),
                invalid_codec,
            )

    def test_builtin_codec_helpers_copy_json_objects_once(self) -> None:
        identity_codec = FunctionalJsonCodec[JsonObject](
            decoder=lambda value: value,
            encoder=lambda value: value,
        )

        for codec in (identity_codec, JSON_OBJECT_CODEC):
            with self.subTest(codec=type(codec).__name__, direction="decode"):
                source: JsonObject = {"nested": {"value": 1}}
                with patch(
                    "mirabox_sdk.codecs.clone_json_object",
                    wraps=clone_json_object,
                ) as copy:
                    decoded = decode_with_codec(source, codec)

                self.assertEqual(copy.call_count, 1)
                self.assertIsNot(decoded, source)
                self.assertIsNot(decoded["nested"], source["nested"])

            with self.subTest(codec=type(codec).__name__, direction="encode"):
                source = {"nested": {"value": 1}}
                with patch(
                    "mirabox_sdk.codecs.clone_json_object",
                    wraps=clone_json_object,
                ) as copy:
                    encoded = encode_with_codec(source, codec)

                self.assertEqual(copy.call_count, 1)
                self.assertIsNot(encoded, source)
                self.assertIsNot(encoded["nested"], source["nested"])

    def test_codec_helpers_isolate_values_for_builtin_subclasses(self) -> None:
        class PassthroughJsonObjectCodec(JsonObjectCodec):
            def decode(self, value: JsonObject) -> JsonObject:
                return value

            def encode(self, value: JsonObject) -> JsonObject:
                return value

        codec = PassthroughJsonObjectCodec()
        for direction, operation in (
            ("decode", decode_with_codec),
            ("encode", encode_with_codec),
        ):
            with self.subTest(direction=direction):
                source: JsonObject = {"nested": {"value": 1}}
                result = operation(source, codec)

                self.assertIsNot(result, source)
                self.assertIsNot(result["nested"], source["nested"])


class StreamDockEventParsingTests(unittest.TestCase):
    def test_event_registry_covers_parser_dispatch_callback_and_exports(self) -> None:
        self.assertEqual(
            set(EVENT_REGISTRY),
            {event_type.value for event_type in StreamDockEventType},
        )
        for wire_name, descriptor in EVENT_REGISTRY.items():
            with self.subTest(event=wire_name):
                self.assertEqual(descriptor.wire_name, wire_name)
                self.assertEqual(str(descriptor.event_class.event), wire_name)
                self.assertIn(descriptor.scope, EventScope)
                self.assertTrue(callable(descriptor.parser))
                self.assertTrue(hasattr(Action, descriptor.callback))
                self.assertIs(
                    getattr(mirabox_sdk, descriptor.event_class.__name__),
                    descriptor.event_class,
                )
                if descriptor.runtime_handler is not None:
                    self.assertTrue(hasattr(StreamDockPlugin, descriptor.runtime_handler))
        with self.assertRaises(TypeError):
            EVENT_REGISTRY["futureEvent"] = next(iter(EVENT_REGISTRY.values()))  # type: ignore[index]

    def test_parses_every_event_registered_for_runtime_dispatch(self) -> None:
        identity: JsonObject = {
            "action": "action-uuid",
            "context": "button",
            "device": "device-uuid",
        }

        def action_payload_event(event: str, **fields: object) -> JsonObject:
            return {
                "event": event,
                **identity,
                "payload": {
                    "settings": {},
                    "coordinates": {"column": 0, "row": 0},
                    **fields,
                },
            }

        visibility = {"controller": "Keypad", "isInMultiAction": False}
        key = {"isInMultiAction": False}
        title_parameters = {
            "fontFamily": "Arial",
            "fontSize": 12,
            "fontStyle": "Regular",
            "fontUnderline": False,
            "showTitle": True,
            "titleAlignment": "middle",
            "titleColor": "#ffffffff",
        }
        envelopes: dict[str, JsonObject] = {
            "willAppear": action_payload_event("willAppear", **visibility),
            "willDisappear": action_payload_event("willDisappear", **visibility),
            "didReceiveSettings": action_payload_event(
                "didReceiveSettings",
                isInMultiAction=False,
            ),
            "titleParametersDidChange": action_payload_event(
                "titleParametersDidChange",
                title="Channel",
                titleParameters=title_parameters,
            ),
            "keyDown": action_payload_event("keyDown", **key),
            "keyUp": action_payload_event("keyUp", **key),
            "touchTap": action_payload_event("touchTap", **key),
            "dialDown": action_payload_event("dialDown", controller="Encoder"),
            "dialUp": action_payload_event("dialUp", controller="Encoder"),
            "dialRotate": action_payload_event("dialRotate", ticks=1, pressed=False),
            "propertyInspectorDidAppear": {
                "event": "propertyInspectorDidAppear",
                **identity,
            },
            "propertyInspectorDidDisappear": {
                "event": "propertyInspectorDidDisappear",
                **identity,
            },
            "sendToPlugin": {
                "event": "sendToPlugin",
                "action": "action-uuid",
                "context": "button",
                "payload": {"event": "refresh"},
            },
            "didReceiveGlobalSettings": {
                "event": "didReceiveGlobalSettings",
                "payload": {"settings": {}},
            },
            "deviceDidConnect": {
                "event": "deviceDidConnect",
                "device": "device-uuid",
                "deviceInfo": {
                    "name": "Stream Dock",
                    "type": 1,
                    "size": {"columns": 5, "rows": 3},
                },
            },
            "deviceDidDisconnect": {
                "event": "deviceDidDisconnect",
                "device": "device-uuid",
            },
            "applicationDidLaunch": {
                "event": "applicationDidLaunch",
                "payload": {"application": "com.example.app"},
            },
            "applicationDidTerminate": {
                "event": "applicationDidTerminate",
                "payload": {"application": "com.example.app"},
            },
            "systemDidWakeUp": {"event": "systemDidWakeUp"},
        }

        self.assertEqual(set(envelopes), set(EVENT_REGISTRY))
        for wire_name, envelope in envelopes.items():
            with self.subTest(event=wire_name):
                event = parse_stream_dock_event(envelope)
                self.assertIsInstance(event, EVENT_REGISTRY[wire_name].event_class)

    def test_isolates_nested_event_data_from_parser_input(self) -> None:
        settings: JsonObject = {"audio": {"threshold": 0.5}}
        message: JsonObject = {
            "event": "didReceiveGlobalSettings",
            "payload": {"settings": settings},
        }

        event = parse_stream_dock_event(message)
        audio = settings["audio"]
        assert isinstance(audio, dict)
        audio["threshold"] = 0.75

        self.assertEqual(
            event,
            DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.5}}),
        )

    def test_copies_only_retained_json_from_known_event(self) -> None:
        settings: JsonObject = {"audio": {"threshold": 0.5}}
        message: JsonObject = {
            "event": "didReceiveGlobalSettings",
            "payload": {"settings": settings},
            "unused": {"values": list(range(100))},
        }

        with (
            patch(
                "mirabox_sdk.parser.clone_json_object",
                wraps=clone_json_object,
            ) as copy,
            patch("mirabox_sdk.parser.is_json_value") as validate_entire_message,
        ):
            event = parse_stream_dock_event(message)

        validate_entire_message.assert_not_called()
        self.assertEqual(copy.call_count, 1)
        self.assertIs(copy.call_args.args[0], settings)
        self.assertEqual(
            event,
            DidReceiveGlobalSettingsEvent(settings={"audio": {"threshold": 0.5}}),
        )

    def test_builds_dial_event_with_typed_payload_fields(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "dialRotate",
                "action": "action-uuid",
                "context": "dial",
                "device": "device-uuid",
                "payload": {
                    "coordinates": {"column": 2, "row": 0},
                    "settings": {"channelId": "microphone"},
                    "ticks": -3,
                    "pressed": True,
                },
            }
        )

        self.assertEqual(
            event,
            DialRotateEvent(
                action="action-uuid",
                context="dial",
                device="device-uuid",
                coordinates=Coordinates(2, 0),
                settings={"channelId": "microphone"},
                ticks=-3,
                pressed=True,
            ),
        )

    def test_wraps_property_inspector_message(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "sendToPlugin",
                "action": "action-uuid",
                "context": "button",
                "payload": {"event": "getChannels", "requestId": 7},
            }
        )

        self.assertEqual(
            event,
            SendToPluginEvent(
                action="action-uuid",
                context="button",
                message=PropertyInspectorMessage(
                    name="getChannels",
                    value={"event": "getChannels", "requestId": 7},
                ),
            ),
        )

    def test_parses_touch_tap_from_mirabox_sdk(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "touchTap",
                "action": "action-uuid",
                "context": "touch",
                "device": "device-uuid",
                "payload": {
                    "settings": {"channelId": "microphone"},
                    "coordinates": {"column": 1, "row": 0},
                    "state": 0,
                    "userDesiredState": 1,
                    "isInMultiAction": False,
                },
            }
        )

        self.assertEqual(
            event,
            TouchTapEvent(
                action="action-uuid",
                context="touch",
                device="device-uuid",
                settings={"channelId": "microphone"},
                coordinates=Coordinates(1, 0),
                state=0,
                user_desired_state=1,
                is_in_multi_action=False,
            ),
        )

    def test_parses_mirabox_controller_types(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "willAppear",
                "action": "action-uuid",
                "context": "dial",
                "device": "device-uuid",
                "payload": {
                    "controller": "Knob",
                    "settings": {},
                    "coordinates": {"column": 0, "row": 0},
                    "isInMultiAction": False,
                },
            }
        )

        self.assertEqual(
            event,
            WillAppearEvent(
                action="action-uuid",
                context="dial",
                device="device-uuid",
                settings={},
                coordinates=Coordinates(0, 0),
                controller=Controller.KNOB,
                is_in_multi_action=False,
            ),
        )

    def test_builds_title_parameters_event(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "titleParametersDidChange",
                "action": "action-uuid",
                "context": "button",
                "device": "device-uuid",
                "payload": {
                    "settings": {"channelId": "microphone"},
                    "coordinates": {"column": 1, "row": 2},
                    "state": 0,
                    "title": "Microphone",
                    "titleParameters": {
                        "fontFamily": "Arial",
                        "fontSize": 12,
                        "fontStyle": "Bold",
                        "fontUnderline": False,
                        "showTitle": True,
                        "titleAlignment": "bottom",
                        "titleColor": "#ffffff",
                    },
                },
            }
        )

        self.assertEqual(
            event,
            TitleParametersDidChangeEvent(
                action="action-uuid",
                context="button",
                device="device-uuid",
                settings={"channelId": "microphone"},
                coordinates=Coordinates(1, 2),
                state=0,
                title="Microphone",
                title_parameters=TitleParameters(
                    font_family="Arial",
                    font_size=12,
                    font_style="Bold",
                    font_underline=False,
                    show_title=True,
                    alignment=TitleAlignment.BOTTOM,
                    color="#ffffff",
                ),
            ),
        )

    def test_parses_optional_device_info_on_disconnect(self) -> None:
        event = parse_stream_dock_event(
            {
                "event": "deviceDidDisconnect",
                "device": "device-uuid",
                "deviceInfo": {
                    "name": "Stream Dock",
                    "type": 0,
                    "size": {"columns": 5, "rows": 3},
                },
            }
        )

        self.assertEqual(
            event,
            DeviceDidDisconnectEvent(
                device="device-uuid",
                info=DeviceInfo(
                    name="Stream Dock",
                    type=0,
                    size=DeviceSize(columns=5, rows=3),
                ),
            ),
        )

    def test_serializes_optional_title_state(self) -> None:
        self.assertEqual(
            SetTitleCommand("button", "Microphone", target=1, state=2).to_wire(),
            {
                "event": "setTitle",
                "context": "button",
                "payload": {"title": "Microphone", "target": 1, "state": 2},
            },
        )

    def test_serializes_remaining_mirabox_sdk_commands(self) -> None:
        self.assertEqual(
            GetSettingsCommand("button").to_wire(),
            {"event": "getSettings", "context": "button"},
        )
        self.assertEqual(
            LogMessageCommand("Channel updated").to_wire(),
            {"event": "logMessage", "payload": {"message": "Channel updated"}},
        )

    def test_preserves_unknown_event_for_forward_compatibility(self) -> None:
        data = {"event": "futureEvent", "payload": {"version": 2}}

        event = parse_stream_dock_event(data)
        payload = data["payload"]
        assert isinstance(payload, dict)
        payload["version"] = 3

        self.assertEqual(
            event,
            UnknownStreamDockEvent(
                event="futureEvent",
                data={"event": "futureEvent", "payload": {"version": 2}},
            ),
        )
        self.assertEqual(event.event_name, "futureEvent")

    def test_can_reject_unknown_event_explicitly(self) -> None:
        with self.assertRaises(UnsupportedEventError) as caught:
            parse_stream_dock_event({"event": "futureEvent"}, allow_unknown=False)

        self.assertEqual(caught.exception.event_name, "futureEvent")
        self.assertEqual(caught.exception.path, ("event",))
        self.assertEqual(
            str(caught.exception),
            "event 'futureEvent', $.event: unsupported Stream Dock event",
        )

    def test_reports_exact_path_for_missing_known_event_field(self) -> None:
        with self.assertRaises(InvalidFieldError) as caught:
            parse_stream_dock_event(
                {
                    "event": "keyDown",
                    "action": "action-uuid",
                    "context": "button",
                    "device": "device-uuid",
                    "payload": {
                        "settings": {},
                        "coordinates": {"column": 0, "row": 0},
                    },
                }
            )

        self.assertEqual(caught.exception.event_name, "keyDown")
        self.assertEqual(caught.exception.path, ("payload", "isInMultiAction"))
        self.assertEqual(caught.exception.reason, "required field is missing")
        self.assertEqual(
            str(caught.exception),
            "event 'keyDown', $.payload.isInMultiAction: required field is missing",
        )

    def test_rejects_non_object_event_envelope(self) -> None:
        with self.assertRaisesRegex(MalformedEventError, r"\$: expected event object"):
            parse_stream_dock_event([])

    def test_rejects_non_finite_number_outside_json_standard(self) -> None:
        with self.assertRaisesRegex(MalformedEventError, "non-JSON value"):
            parse_stream_dock_event({"event": "futureEvent", "payload": float("nan")})

    def test_reports_invalid_property_inspector_payload(self) -> None:
        with self.assertRaises(InvalidFieldError) as caught:
            parse_stream_dock_event(
                {
                    "event": "sendToPlugin",
                    "action": "action-uuid",
                    "context": "button",
                    "payload": "not-an-object",
                }
            )

        self.assertEqual(caught.exception.path, ("payload",))
        self.assertEqual(caught.exception.reason, "expected object")

    def test_does_not_default_missing_dial_rotation_values(self) -> None:
        data = {
            "event": "dialRotate",
            "action": "action-uuid",
            "context": "dial",
            "device": "device-uuid",
            "payload": {
                "settings": {},
                "coordinates": {"column": 0, "row": 0},
                "pressed": False,
            },
        }

        with self.assertRaises(InvalidFieldError) as caught:
            parse_stream_dock_event(data)

        self.assertEqual(caught.exception.path, ("payload", "ticks"))

    def test_rejects_boolean_where_dial_ticks_requires_integer(self) -> None:
        with self.assertRaises(InvalidFieldError) as caught:
            parse_stream_dock_event(
                {
                    "event": "dialRotate",
                    "action": "action-uuid",
                    "context": "dial",
                    "device": "device-uuid",
                    "payload": {
                        "settings": {},
                        "coordinates": {"column": 0, "row": 0},
                        "ticks": True,
                        "pressed": False,
                    },
                }
            )

        self.assertEqual(caught.exception.path, ("payload", "ticks"))
        self.assertEqual(caught.exception.reason, "expected integer")


class WebSocketStreamDockConnectionTests(unittest.TestCase):
    def test_declares_stream_dock_contract(self) -> None:
        self.assertIn(StreamDockConnection, WebSocketStreamDockConnection.__mro__)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_send_async_returns_before_writer_io_completes(self, app_factory: Mock) -> None:
        write_started = Event()
        release_write = Event()

        def write(_raw_message: str) -> None:
            write_started.set()
            if not release_write.wait(1):
                raise AssertionError("timed out waiting to release write")

        app_factory.return_value.send.side_effect = write
        connection = WebSocketStreamDockConnection(12345)
        future = connection.send_async(SetTitleCommand("button", "Count"))

        self.assertIsInstance(future, CommandFuture)
        self.assertTrue(write_started.wait(1))
        self.assertFalse(future.done())
        self.assertFalse(future.wait(0))
        with self.assertRaisesRegex(TimeoutError, "did not complete"):
            future.result(0)

        release_write.set()
        future.result(1)
        self.assertTrue(future.done())
        self.assertIsNone(future.exception())
        connection.close()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_send_async_returns_before_serialization_completes(
        self,
        app_factory: Mock,
    ) -> None:
        serialization_started = Event()
        release_serialization = Event()

        class BlockingCommand(StreamDockCommand):
            def to_wire(self) -> JsonObject:
                serialization_started.set()
                if not release_serialization.wait(1):
                    raise AssertionError("timed out waiting to release serialization")
                return {"event": "blockingCommand"}

        connection = WebSocketStreamDockConnection(12345)
        future = connection.send_async(BlockingCommand())

        self.assertTrue(serialization_started.wait(1))
        self.assertFalse(future.done())
        release_serialization.set()
        future.result(1)
        app_factory.return_value.send.assert_called_once()
        connection.close()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_send_async_reports_writer_failure_through_future(self, app_factory: Mock) -> None:
        app_factory.return_value.send.side_effect = RuntimeError("transport failed")
        connection = WebSocketStreamDockConnection(12345)

        with self.assertLogs("mirabox_sdk.outbound", level="ERROR"):
            future = connection.send_async(SetStateCommand("button", 2))
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                future.result(1)

        error = future.exception()
        self.assertIsInstance(error, RuntimeError)
        connection.close()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_rejects_non_json_command_before_sending(self, app_factory: Mock) -> None:
        class CustomCommand(StreamDockCommand):
            def __init__(self, message: object) -> None:
                self.message = message

            def to_wire(self) -> JsonObject:
                return self.message  # type: ignore[return-value]

        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)

        invalid_commands = (
            CustomCommand({"threshold": float("nan")}),
            CustomCommand({"unsupported": object()}),
            CustomCommand({1: "x"}),
            CustomCommand({"items": (1, 2)}),
        )
        for command in invalid_commands:
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(
                    ValueError,
                    "non-JSON value",
                ),
            ):
                connection.send(command)

        web_socket.send.assert_not_called()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_requires_explicit_validated_wire_result(self, app_factory: Mock) -> None:
        class InvalidCommand(StreamDockCommand):
            def to_wire(self) -> JsonObject:
                return {"event": "customEvent"}

            def to_validated_wire(self) -> ValidatedWireMessage:
                return self.to_wire()  # type: ignore[return-value]

        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)

        with self.assertRaisesRegex(TypeError, "ValidatedWireMessage"):
            connection.send(InvalidCommand())

        web_socket.send.assert_not_called()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_trusts_all_owned_command_payloads(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)
        payload: JsonObject = {"profiles": [{"levels": list(range(100))}]}
        commands = (
            SetGlobalSettingsCommand.from_settings("plugin", payload, JSON_OBJECT_CODEC),
            SetSettingsCommand.from_settings("button", payload, JSON_OBJECT_CODEC),
            SendToPropertyInspectorCommand.from_payload(
                "action",
                "button",
                payload,
                JSON_OBJECT_CODEC,
            ),
        )

        with patch("mirabox_sdk.commands.clone_json_object") as validate:
            for command in commands:
                connection.send(command)

        validate.assert_not_called()
        self.assertEqual(
            json.loads(web_socket.send.call_args_list[0].args[0]),
            {
                "event": "setGlobalSettings",
                "context": "plugin",
                "payload": payload,
            },
        )

    def test_rejects_invalid_extensible_payloads_when_commands_take_ownership(self) -> None:
        factories = (
            lambda: SetGlobalSettingsCommand(
                "plugin",
                {"items": (1, 2)},  # type: ignore[dict-item]
            ),
            lambda: SetSettingsCommand(
                "button",
                {"items": (1, 2)},  # type: ignore[dict-item]
            ),
            lambda: SendToPropertyInspectorCommand(
                "action",
                "button",
                {"items": (1, 2)},  # type: ignore[dict-item]
            ),
        )

        for factory in factories:
            with (
                self.subTest(factory=factory),
                self.assertRaisesRegex(ValueError, "finite JSON object"),
            ):
                factory()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_reuses_serialized_command_for_debug_logging(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)
        command = SetSettingsCommand("button", {"nested": {"value": 1}})

        with (
            patch("mirabox_sdk.connection.json.dumps", wraps=json.dumps) as serialize,
            patch(
                "mirabox_sdk.connection._protocol_payload_logging_enabled",
                return_value=True,
            ),
            self.assertLogs("mirabox_sdk.connection", level="DEBUG") as logs,
        ):
            connection.send(command)

        self.assertEqual(serialize.call_count, 1)
        self.assertIn('"nested": {"value": 1}', "\n".join(logs.output))
        web_socket.send.assert_called_once()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_writes_commands_in_order_from_one_owned_thread(self, app_factory: Mock) -> None:
        serialization_thread_ids: list[int] = []
        writer_thread_ids: list[int] = []
        producer_thread_ids: list[int] = []
        sent_sequences: list[int] = []
        failures: list[Exception] = []
        first_write_started = Event()
        release_first_write = Event()

        class SequencedCommand(StreamDockCommand):
            def __init__(self, sequence: int) -> None:
                self.sequence = sequence

            def to_wire(self) -> JsonObject:
                serialization_thread_ids.append(get_ident())
                return {"event": "sequenced", "payload": {"sequence": self.sequence}}

        def write(raw_message: str) -> None:
            message = json.loads(raw_message)
            sequence = message["payload"]["sequence"]
            writer_thread_ids.append(get_ident())
            sent_sequences.append(sequence)
            if sequence == 1:
                first_write_started.set()
                if not release_first_write.wait(1):
                    raise AssertionError("timed out waiting to release first write")

        def submit(connection: WebSocketStreamDockConnection, sequence: int) -> None:
            producer_thread_ids.append(get_ident())
            try:
                connection.send(SequencedCommand(sequence))
            except Exception as exc:  # pragma: no cover - asserted through failures
                failures.append(exc)

        app_factory.return_value.send.side_effect = write
        connection = WebSocketStreamDockConnection(12345)
        producers = [Thread(target=submit, args=(connection, sequence)) for sequence in range(1, 4)]

        producers[0].start()
        self.assertTrue(first_write_started.wait(1))
        producers[1].start()
        self.assertTrue(wait_for(lambda: connection.outbound_queue_metrics.current_depth == 1))
        producers[2].start()
        self.assertTrue(wait_for(lambda: connection.outbound_queue_metrics.current_depth == 2))
        release_first_write.set()
        for producer in producers:
            producer.join(1)
            self.assertFalse(producer.is_alive())
        connection.close()

        self.assertEqual(failures, [])
        self.assertEqual(sent_sequences, [1, 2, 3])
        self.assertEqual(len(set(serialization_thread_ids + writer_thread_ids)), 1)
        self.assertTrue(set(producer_thread_ids).isdisjoint(writer_thread_ids))
        metrics = connection.outbound_queue_metrics
        self.assertEqual(metrics.peak_depth, 2)
        self.assertEqual(metrics.serialized, 3)
        self.assertEqual(metrics.sent, 3)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_sends_one_immutable_command_from_concurrent_threads(
        self,
        app_factory: Mock,
    ) -> None:
        command = LogMessageCommand("shared")
        connection = WebSocketStreamDockConnection(12345)
        failures: list[Exception] = []

        def submit() -> None:
            try:
                connection.send(command)
            except Exception as exc:  # pragma: no cover - asserted through failures
                failures.append(exc)

        producers = [Thread(target=submit) for _ in range(16)]
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join(1)
            self.assertFalse(producer.is_alive())
        connection.close()

        self.assertEqual(failures, [])
        self.assertEqual(app_factory.return_value.send.call_count, 16)
        self.assertEqual(connection.outbound_queue_metrics.sent, 16)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_rejects_command_when_outbound_queue_is_full(self, app_factory: Mock) -> None:
        first_write_started = Event()
        release_first_write = Event()
        producer_failures: list[Exception] = []

        def write(_raw_message: str) -> None:
            if not first_write_started.is_set():
                first_write_started.set()
                if not release_first_write.wait(1):
                    raise AssertionError("timed out waiting to release first write")

        def submit(connection: WebSocketStreamDockConnection, title: str) -> None:
            try:
                connection.send(SetTitleCommand("button", title))
            except Exception as exc:  # pragma: no cover - asserted through failures
                producer_failures.append(exc)

        app_factory.return_value.send.side_effect = write
        connection = WebSocketStreamDockConnection(12345, outbound_queue_limit=1)
        first = Thread(target=submit, args=(connection, "first"))
        second = Thread(target=submit, args=(connection, "second"))

        first.start()
        self.assertTrue(first_write_started.wait(1))
        second.start()
        self.assertTrue(wait_for(lambda: connection.outbound_queue_metrics.current_depth == 1))
        with self.assertRaisesRegex(OutboundQueueFullError, "limit=1"):
            connection.send(SetStateCommand("button", 2))
        with self.assertRaisesRegex(OutboundQueueFullError, "limit=1"):
            connection.send_async(SetStateCommand("button", 3))

        release_first_write.set()
        first.join(1)
        second.join(1)
        connection.close()

        self.assertEqual(producer_failures, [])
        metrics = connection.outbound_queue_metrics
        self.assertEqual(metrics.rejected_full, 2)
        self.assertEqual(metrics.rejected, 2)
        self.assertEqual(metrics.sent, 2)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_coalesces_adjacent_pending_outbound_state_commands(
        self,
        app_factory: Mock,
    ) -> None:
        first_write_started = Event()
        release_first_write = Event()
        producer_failures: list[Exception] = []

        def write(raw_message: str) -> None:
            message = json.loads(raw_message)
            if message["event"] == "logMessage":
                first_write_started.set()
                if not release_first_write.wait(1):
                    raise AssertionError("timed out waiting to release first write")

        def submit(connection: WebSocketStreamDockConnection, title: str) -> None:
            try:
                connection.send(SetTitleCommand("button", title, target=1, state=2))
            except Exception as exc:  # pragma: no cover - asserted through failures
                producer_failures.append(exc)

        web_socket = app_factory.return_value
        web_socket.send.side_effect = write
        connection = WebSocketStreamDockConnection(
            12345,
            outbound_queue_limit=1,
            coalesce_outbound_commands=True,
        )
        blocker = Thread(target=connection.send, args=(LogMessageCommand("block"),))
        old_title = Thread(target=submit, args=(connection, "old"))
        new_title = Thread(target=submit, args=(connection, "new"))

        blocker.start()
        self.assertTrue(first_write_started.wait(1))
        old_title.start()
        self.assertTrue(wait_for(lambda: connection.outbound_queue_metrics.current_depth == 1))
        new_title.start()
        self.assertTrue(wait_for(lambda: connection.outbound_queue_metrics.coalesced == 1))
        self.assertEqual(connection.outbound_queue_metrics.current_depth, 1)

        release_first_write.set()
        for producer in (blocker, old_title, new_title):
            producer.join(1)
            self.assertFalse(producer.is_alive())
        connection.close()

        self.assertEqual(producer_failures, [])
        messages = [json.loads(call.args[0]) for call in web_socket.send.call_args_list]
        self.assertEqual([message["event"] for message in messages], ["logMessage", "setTitle"])
        self.assertEqual(messages[1]["payload"]["title"], "new")
        metrics = connection.outbound_queue_metrics
        self.assertEqual(metrics.submitted, 3)
        self.assertEqual(metrics.enqueued, 2)
        self.assertEqual(metrics.coalesced, 1)
        self.assertEqual(metrics.sent, 2)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_isolates_outbound_failures_and_keeps_writer_running(
        self,
        app_factory: Mock,
    ) -> None:
        class InvalidCommand(StreamDockCommand):
            def to_wire(self) -> JsonObject:
                return {  # type: ignore[return-value]
                    "event": "invalid",
                    "payload": {"value": object()},
                }

        web_socket = app_factory.return_value
        web_socket.send.side_effect = [RuntimeError("transport failed"), None]
        connection = WebSocketStreamDockConnection(12345)

        with self.assertLogs("mirabox_sdk.outbound", level="ERROR"):
            with self.assertRaisesRegex(ValueError, "non-JSON value"):
                connection.send(InvalidCommand())
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                connection.send(LogMessageCommand("first"))
            connection.send(LogMessageCommand("second"))
        connection.close()

        metrics = connection.outbound_queue_metrics
        self.assertEqual(metrics.serialization_failures, 1)
        self.assertEqual(metrics.transport_failures, 1)
        self.assertEqual(metrics.failures, 2)
        self.assertEqual(metrics.sent, 1)
        self.assertEqual(web_socket.send.call_count, 2)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_rejects_outbound_command_after_connection_close(self, app_factory: Mock) -> None:
        connection = WebSocketStreamDockConnection(12345)

        connection.close()
        with self.assertRaises(OutboundCommandBusClosedError):
            connection.send(LogMessageCommand("too late"))
        with self.assertRaises(OutboundCommandBusClosedError):
            connection.send_async(LogMessageCommand("also too late"))

        metrics = connection.outbound_queue_metrics
        self.assertEqual(metrics.rejected_after_shutdown, 2)
        self.assertEqual(metrics.rejected, 2)
        app_factory.return_value.send.assert_not_called()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_close_is_idempotent_across_concurrent_callers(self, app_factory: Mock) -> None:
        close_started = Event()
        release_close = Event()

        def close_web_socket() -> None:
            close_started.set()
            if not release_close.wait(1):
                raise AssertionError("timed out waiting to release close")

        app_factory.return_value.close.side_effect = close_web_socket
        connection = WebSocketStreamDockConnection(12345)
        callers = [Thread(target=connection.close) for _ in range(8)]
        callers[0].start()
        self.assertTrue(close_started.wait(1))
        for caller in callers[1:]:
            caller.start()
        release_close.set()
        for caller in callers:
            caller.join(1)
            self.assertFalse(caller.is_alive())

        app_factory.return_value.close.assert_called_once_with()
        with self.assertRaises(OutboundCommandBusClosedError):
            connection.send(LogMessageCommand("too late"))

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_callback_close_does_not_wait_for_overlapping_external_close(
        self,
        app_factory: Mock,
    ) -> None:
        external_shutdown_started = Event()
        allow_callback_close = Event()
        callback_started = Event()
        callback_close_returned = Event()
        callback_state_at_web_socket_close: list[bool] = []

        def on_event(_event: object) -> None:
            callback_started.set()
            if not allow_callback_close.wait(1):
                raise AssertionError("timed out waiting to call close from callback")
            connection.close()
            callback_close_returned.set()

        def close_web_socket() -> None:
            callback_state_at_web_socket_close.append(callback_close_returned.is_set())

        listener = Mock()
        listener.on_stream_dock_event.side_effect = on_event
        app_factory.return_value.close.side_effect = close_web_socket
        connection = WebSocketStreamDockConnection(
            12345,
            inbound_shutdown_timeout=0.05,
        )
        connection.set_listener(listener)
        original_stop_accepting = connection._inbound.stop_accepting

        def stop_accepting() -> None:
            original_stop_accepting()
            external_shutdown_started.set()

        with patch.object(
            connection._inbound,
            "stop_accepting",
            side_effect=stop_accepting,
        ):
            connection._inbound.start()
            connection._on_message(app_factory.return_value, '{"event":"firstEvent"}')
            self.assertTrue(callback_started.wait(1))

            external_close = Thread(target=connection.close, daemon=True)
            external_close.start()
            self.assertTrue(external_shutdown_started.wait(1))
            allow_callback_close.set()
            external_close.join(1)

        self.assertFalse(external_close.is_alive())
        self.assertTrue(callback_close_returned.wait(1))
        self.assertEqual(callback_state_at_web_socket_close, [True])
        app_factory.return_value.close.assert_called_once_with()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_callback_close_waits_for_other_in_flight_context(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        other_started = Event()
        closer_started = Event()
        close_returned = Event()
        release_other = Event()

        def on_event(event: KeyDownEvent) -> None:
            if event.context == "other":
                other_started.set()
                release_other.wait(1)
                return
            closer_started.set()
            connection.close()
            close_returned.set()

        def read_frames() -> None:
            connection._on_message(app_factory.return_value, key_down_message("other"))
            self.assertTrue(other_started.wait(1))
            connection._on_message(app_factory.return_value, key_down_message("closer"))
            self.assertTrue(closer_started.wait(1))
            self.assertFalse(close_returned.is_set())
            release_other.set()
            self.assertTrue(close_returned.wait(1))

        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)
        listener.on_stream_dock_event.side_effect = on_event
        app_factory.return_value.run_forever.side_effect = read_frames

        connection.run_forever()

        self.assertEqual(connection.inbound_queue_metrics.dispatched, 2)
        app_factory.return_value.close.assert_called_once_with()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_outbound_shutdown_timeout_discards_waiting_commands(
        self,
        app_factory: Mock,
    ) -> None:
        write_started = Event()
        release_write = Event()
        producer_failures: list[Exception] = []

        def write(_raw_message: str) -> None:
            write_started.set()
            release_write.wait(1)

        def submit(connection: WebSocketStreamDockConnection, message: str) -> None:
            try:
                connection.send(LogMessageCommand(message))
            except Exception as exc:  # pragma: no cover - asserted through failures
                producer_failures.append(exc)

        web_socket = app_factory.return_value
        web_socket.send.side_effect = write
        web_socket.close.side_effect = release_write.set
        connection = WebSocketStreamDockConnection(
            12345,
            outbound_shutdown_timeout=0.01,
        )
        in_flight = Thread(target=submit, args=(connection, "in flight"))
        waiting = Thread(target=submit, args=(connection, "waiting"))

        in_flight.start()
        self.assertTrue(write_started.wait(1))
        waiting.start()
        self.assertTrue(wait_for(lambda: connection.outbound_queue_metrics.current_depth == 1))
        with self.assertLogs("mirabox_sdk.connection", level="WARNING"):
            connection.close()

        for producer in (in_flight, waiting):
            producer.join(1)
            self.assertFalse(producer.is_alive())
        self.assertEqual(len(producer_failures), 2)
        self.assertTrue(
            all(isinstance(error, OutboundCommandBusClosedError) for error in producer_failures)
        )
        self.assertEqual(connection.outbound_queue_metrics.discarded_after_shutdown, 1)
        self.assertTrue(wait_for(lambda: connection.outbound_queue_metrics.sent == 1))

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_rejects_non_finite_incoming_json(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        listener = Mock()
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)

        with self.assertLogs("mirabox_sdk.connection", level="WARNING"):
            connection._on_message(
                web_socket,
                '{"event":"systemDidWakeUp","unused":NaN}',
            )

        listener.on_stream_dock_event.assert_not_called()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_translates_messages_across_websocket_boundary(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        listener = Mock()
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)
        incoming = (
            '{"event":"keyDown","action":"action-uuid","context":"button",'
            '"device":"device-uuid","payload":{"controller":"Keypad",'
            '"settings":{"channelId":"microphone"},'
            '"coordinates":{"column":0,"row":0},"isInMultiAction":false}}'
        )

        connection._on_open(web_socket)
        with self.assertNoLogs("mirabox_sdk.connection", level="INFO"):
            connection._on_message(web_socket, incoming)
            connection.send(SetTitleCommand("button", "Микрофон"))

        listener.on_stream_dock_event.assert_not_called()
        connection.run_forever()

        listener.on_stream_dock_connected.assert_called_once_with()
        listener.on_stream_dock_event.assert_called_once_with(
            KeyDownEvent(
                action="action-uuid",
                context="button",
                device="device-uuid",
                controller=Controller.KEYPAD,
                settings={"channelId": "microphone"},
                coordinates=Coordinates(0, 0),
                is_in_multi_action=False,
            )
        )
        self.assertEqual(
            json.loads(web_socket.send.call_args.args[0]),
            {
                "event": "setTitle",
                "context": "button",
                "payload": {"title": "Микрофон", "target": 0},
            },
        )

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_redacts_payloads_from_debug_protocol_logs(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(Mock())
        incoming_secret = "incoming-secret-value"
        outgoing_secret = "outgoing-secret-value"
        property_secret = "property-secret-value"

        with self.assertLogs("mirabox_sdk.connection", level="DEBUG") as logs:
            connection._on_message(
                web_socket,
                json.dumps(
                    {
                        "event": "didReceiveGlobalSettings",
                        "payload": {"settings": {"arbitraryName": incoming_secret}},
                    }
                ),
            )
            connection.send(SetGlobalSettingsCommand("plugin", {"arbitraryName": outgoing_secret}))
            connection.send(
                SendToPropertyInspectorCommand(
                    action="action-uuid",
                    context="button",
                    payload={"accessToken": property_secret, "label": "visible"},
                )
            )

        output = "\n".join(logs.output)
        self.assertNotIn(incoming_secret, output)
        self.assertNotIn(outgoing_secret, output)
        self.assertNotIn(property_secret, output)
        self.assertNotIn('"label": "visible"', output)
        self.assertIn("'payload': '<redacted>'", output)
        self.assertIn("'action': 'action-uuid'", output)
        self.assertIn("'context': 'button'", output)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_logs_full_payloads_only_when_explicitly_enabled(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(Mock())
        incoming_secret = "incoming-secret-value"
        outgoing_secret = "outgoing-secret-value"
        stream = StringIO()
        redacted_stream = StringIO()

        try:
            configure_logging(level="DEBUG", stream=stream, include_payload=True)
            connection._on_message(
                web_socket,
                json.dumps(
                    {
                        "event": "didReceiveGlobalSettings",
                        "payload": {"settings": {"accessToken": incoming_secret}},
                    }
                ),
            )
            connection.send(
                SendToPropertyInspectorCommand(
                    action="action-uuid",
                    context="button",
                    payload={"accessToken": outgoing_secret, "label": "visible"},
                )
            )
            configure_logging(level="DEBUG", stream=redacted_stream)
            connection.send(
                SendToPropertyInspectorCommand(
                    action="action-uuid",
                    context="button",
                    payload={"accessToken": outgoing_secret},
                )
            )
        finally:
            configure_logging(enabled=False)

        output = stream.getvalue()
        self.assertIn(incoming_secret, output)
        self.assertIn(outgoing_secret, output)
        self.assertIn('"label": "visible"', output)
        self.assertNotIn("'payload': '<redacted>'", output)
        self.assertNotIn(outgoing_secret, redacted_stream.getvalue())
        self.assertIn("'payload': '<redacted>'", redacted_stream.getvalue())

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_rejects_invalid_inbound_messages(self, app_factory: Mock) -> None:
        listener = Mock()
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)

        with self.assertLogs("mirabox_sdk.connection", level="WARNING") as logs:
            connection._on_message(app_factory.return_value, "not-json")
            connection._on_message(app_factory.return_value, "[]")
            connection._on_message(
                app_factory.return_value,
                '{"event":"dialRotate","action":"action-uuid",'
                '"context":"dial","device":"device-uuid","payload":'
                '{"settings":{},"coordinates":{"column":0,"row":0},'
                '"pressed":false}}',
            )

        listener.on_stream_dock_event.assert_not_called()
        self.assertTrue(any("expected event object" in line for line in logs.output))
        self.assertTrue(any("$.payload.ticks" in line for line in logs.output))

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_forwards_unknown_inbound_event(self, app_factory: Mock) -> None:
        listener = Mock()
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)

        connection._on_message(
            app_factory.return_value,
            '{"event":"futureEvent","payload":{"version":2}}',
        )

        listener.on_stream_dock_event.assert_not_called()
        connection.run_forever()

        listener.on_stream_dock_event.assert_called_once_with(
            UnknownStreamDockEvent(
                event="futureEvent",
                data={"event": "futureEvent", "payload": {"version": 2}},
            )
        )

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_dispatches_callbacks_outside_websocket_reader(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        listener = Mock()
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)
        reader_returned = Event()
        callback_finished = Event()
        callback_thread_ids: list[int] = []
        callback_observed_reader_return: list[bool] = []
        reader_thread_ids: list[int] = []

        def on_event(_event: object) -> None:
            callback_thread_ids.append(get_ident())
            callback_observed_reader_return.append(reader_returned.wait(1))
            callback_finished.set()

        def read_frame() -> None:
            reader_thread_ids.append(get_ident())
            connection._on_message(
                web_socket,
                '{"event":"systemDidWakeUp"}',
            )
            reader_returned.set()

        listener.on_stream_dock_event.side_effect = on_event
        web_socket.run_forever.side_effect = read_frame

        connection.run_forever()

        self.assertTrue(callback_finished.is_set())
        self.assertEqual(callback_observed_reader_return, [True])
        self.assertNotEqual(callback_thread_ids, reader_thread_ids)
        self.assertEqual(
            asdict(connection.inbound_queue_metrics),
            {
                "queue_limit": 1024,
                "current_depth": 0,
                "peak_depth": 1,
                "received": 1,
                "enqueued": 1,
                "coalesced": 0,
                "backpressured": 0,
                "dispatched": 1,
                "dropped_newest": 0,
                "dropped_oldest": 0,
                "dropped_after_shutdown": 0,
                "dropped_without_listener": 0,
                "callback_failures": 0,
                "callback_timeouts": 0,
            },
        )

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_drops_newest_event_when_inbound_queue_is_full(self, app_factory: Mock) -> None:
        listener = Mock()
        received: list[str] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(event.context)
        connection = WebSocketStreamDockConnection(
            12345,
            inbound_queue_limit=2,
            overflow_policy=InboundOverflowPolicy.DROP_NEWEST,
        )
        connection.set_listener(listener)

        with self.assertLogs("mirabox_sdk.connection", level="WARNING"):
            for context in ("first", "second", "third"):
                connection._on_message(
                    app_factory.return_value,
                    dial_rotate_message(context),
                )

        pending_metrics = connection.inbound_queue_metrics
        self.assertEqual(pending_metrics.current_depth, 2)
        self.assertEqual(pending_metrics.peak_depth, 2)
        self.assertEqual(pending_metrics.dropped_newest, 1)
        self.assertEqual(pending_metrics.dropped, 1)

        connection.run_forever()

        self.assertEqual(received, ["first", "second"])
        self.assertEqual(connection.inbound_queue_metrics.dispatched, 2)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_drops_oldest_event_when_inbound_queue_is_full(self, app_factory: Mock) -> None:
        listener = Mock()
        received: list[str] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(event.context)
        connection = WebSocketStreamDockConnection(
            12345,
            inbound_queue_limit=2,
            overflow_policy=InboundOverflowPolicy.DROP_OLDEST,
        )
        connection.set_listener(listener)

        for context in ("first", "second", "third"):
            connection._on_message(
                app_factory.return_value,
                dial_rotate_message(context),
            )

        connection.run_forever()

        self.assertEqual(received, ["second", "third"])
        metrics = connection.inbound_queue_metrics
        self.assertEqual(metrics.dropped_oldest, 1)
        self.assertEqual(metrics.dropped, 1)
        self.assertEqual(metrics.dispatched, 2)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_drop_newest_evicts_rotation_instead_of_lifecycle_event(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        received: list[tuple[str, str]] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(
            (event.event_name, event.context)
        )
        connection = WebSocketStreamDockConnection(
            12345,
            inbound_queue_limit=2,
            overflow_policy=InboundOverflowPolicy.DROP_NEWEST,
        )
        connection.set_listener(listener)

        connection._on_message(app_factory.return_value, dial_rotate_message("first"))
        connection._on_message(app_factory.return_value, dial_rotate_message("second"))
        connection._on_message(app_factory.return_value, will_appear_message("button"))
        connection.run_forever()

        self.assertEqual(
            received,
            [("dialRotate", "first"), ("willAppear", "button")],
        )
        self.assertEqual(connection.inbound_queue_metrics.dropped_newest, 1)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_drop_oldest_never_evicts_queued_lifecycle_event(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        received: list[tuple[str, str]] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(
            (event.event_name, event.context)
        )
        connection = WebSocketStreamDockConnection(
            12345,
            inbound_queue_limit=2,
            overflow_policy=InboundOverflowPolicy.DROP_OLDEST,
        )
        connection.set_listener(listener)

        connection._on_message(app_factory.return_value, will_appear_message("button"))
        connection._on_message(app_factory.return_value, dial_rotate_message("first"))
        connection._on_message(app_factory.return_value, dial_rotate_message("second"))
        connection.run_forever()

        self.assertEqual(
            received,
            [("willAppear", "button"), ("dialRotate", "second")],
        )
        self.assertEqual(connection.inbound_queue_metrics.dropped_oldest, 1)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_backpressures_instead_of_dropping_lifecycle_events(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        callback_started = Event()
        release_callback = Event()
        third_submit_started = Event()
        reader_resumed = Event()
        received: list[str] = []
        lifecycle_failures: list[BaseException] = []

        def on_event(event: WillAppearEvent) -> None:
            received.append(event.context)
            if event.context == "first":
                callback_started.set()
                release_callback.wait(1)

        connection = WebSocketStreamDockConnection(
            12345,
            inbound_queue_limit=1,
            overflow_policy=InboundOverflowPolicy.DROP_NEWEST,
        )
        connection.set_listener(listener)
        listener.on_stream_dock_event.side_effect = on_event

        def read_frames() -> None:
            connection._on_message(app_factory.return_value, will_appear_message("first"))
            callback_started.wait(1)
            connection._on_message(app_factory.return_value, will_appear_message("second"))
            third_submit_started.set()
            connection._on_message(app_factory.return_value, will_appear_message("third"))
            reader_resumed.set()

        def run_connection() -> None:
            try:
                connection.run_forever()
            except BaseException as exc:  # pragma: no cover - asserted through failures
                lifecycle_failures.append(exc)

        app_factory.return_value.run_forever.side_effect = read_frames
        lifecycle_thread = Thread(target=run_connection)
        lifecycle_thread.start()
        self.addCleanup(lambda: lifecycle_thread.join(1))
        self.addCleanup(release_callback.set)

        self.assertTrue(third_submit_started.wait(1))
        self.assertTrue(wait_for(lambda: connection.inbound_queue_metrics.backpressured == 1))
        self.assertFalse(reader_resumed.wait(0.01))

        release_callback.set()
        lifecycle_thread.join(1)
        self.assertFalse(lifecycle_thread.is_alive())
        self.assertTrue(reader_resumed.is_set())
        self.assertEqual(received, ["first", "second", "third"])
        self.assertEqual(connection.inbound_queue_metrics.dropped, 0)
        self.assertEqual(lifecycle_failures, [])

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_slow_context_does_not_block_another_context(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        slow_started = Event()
        fast_finished = Event()
        release_slow = Event()

        def on_event(event: KeyDownEvent) -> None:
            if event.context == "slow":
                slow_started.set()
                release_slow.wait(1)
            else:
                fast_finished.set()

        def read_frames() -> None:
            connection._on_message(app_factory.return_value, key_down_message("slow"))
            self.assertTrue(slow_started.wait(1))
            connection._on_message(app_factory.return_value, key_down_message("fast"))
            try:
                self.assertTrue(fast_finished.wait(0.1))
            finally:
                release_slow.set()

        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)
        listener.on_stream_dock_event.side_effect = on_event
        app_factory.return_value.run_forever.side_effect = read_frames

        connection.run_forever()

        self.assertEqual(connection.inbound_queue_metrics.dispatched, 2)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_does_not_overlap_callbacks_for_same_context(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        first_started = Event()
        second_started = Event()
        release_first = Event()
        received: list[int] = []

        def on_event(event: KeyDownEvent) -> None:
            sequence = event.settings["sequence"]
            received.append(sequence)
            if sequence == 1:
                first_started.set()
                release_first.wait(1)
            else:
                second_started.set()

        def read_frames() -> None:
            connection._on_message(
                app_factory.return_value,
                key_down_message("button", sequence=1),
            )
            self.assertTrue(first_started.wait(1))
            connection._on_message(
                app_factory.return_value,
                key_down_message("button", sequence=2),
            )
            try:
                self.assertFalse(second_started.wait(0.02))
            finally:
                release_first.set()

        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)
        listener.on_stream_dock_event.side_effect = on_event
        app_factory.return_value.run_forever.side_effect = read_frames

        connection.run_forever()

        self.assertEqual(received, [1, 2])

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_lifecycle_and_broadcast_events_are_exclusive_barriers(
        self,
        app_factory: Mock,
    ) -> None:
        def assert_barrier(barrier_message: str, barrier_name: str) -> None:
            listener = Mock()
            slow_started = Event()
            barrier_started = Event()
            after_started = Event()
            release_slow = Event()
            received: list[str] = []

            def on_event(event: object) -> None:
                if event.event_name == "keyDown":
                    label = event.context
                else:
                    label = event.event_name
                received.append(label)
                if label == "slow":
                    slow_started.set()
                    release_slow.wait(1)
                elif label == barrier_name:
                    barrier_started.set()
                elif label == "after":
                    after_started.set()

            def read_frames() -> None:
                connection._on_message(
                    app_factory.return_value,
                    key_down_message("slow"),
                )
                self.assertTrue(slow_started.wait(1))
                connection._on_message(app_factory.return_value, barrier_message)
                connection._on_message(
                    app_factory.return_value,
                    key_down_message("after"),
                )
                try:
                    self.assertFalse(barrier_started.wait(0.02))
                    self.assertFalse(after_started.is_set())
                finally:
                    release_slow.set()

            connection = WebSocketStreamDockConnection(12345)
            connection.set_listener(listener)
            listener.on_stream_dock_event.side_effect = on_event
            app_factory.return_value.run_forever.side_effect = read_frames

            connection.run_forever()

            self.assertEqual(received, ["slow", barrier_name, "after"])

        for barrier_message, barrier_name in (
            (will_appear_message("lifecycle"), "willAppear"),
            ('{"event":"systemDidWakeUp"}', "systemDidWakeUp"),
        ):
            with self.subTest(barrier=barrier_name):
                assert_barrier(barrier_message, barrier_name)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_preserves_per_context_order(self, app_factory: Mock) -> None:
        listener = Mock()
        received: list[tuple[str, int]] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(
            (event.context, event.settings["sequence"])
        )
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)

        for context, sequence in (
            ("dial-a", 1),
            ("dial-b", 1),
            ("dial-a", 2),
            ("dial-b", 2),
            ("dial-a", 3),
        ):
            connection._on_message(
                app_factory.return_value,
                key_down_message(context, sequence=sequence),
            )

        connection.run_forever()

        self.assertEqual(
            [sequence for context, sequence in received if context == "dial-a"],
            [1, 2, 3],
        )
        self.assertEqual(
            [sequence for context, sequence in received if context == "dial-b"],
            [1, 2],
        )

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_coalesces_compatible_dial_rotations_per_context(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        received: list[tuple[str, int]] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(
            (event.context, event.ticks)
        )
        connection = WebSocketStreamDockConnection(
            12345,
            coalesce_dial_rotations=True,
        )
        connection.set_listener(listener)

        for context, ticks in (("dial-a", 1), ("dial-b", 5), ("dial-a", 2)):
            connection._on_message(
                app_factory.return_value,
                json.dumps(
                    {
                        "event": "dialRotate",
                        "action": "action-uuid",
                        "context": context,
                        "device": "device-uuid",
                        "payload": {
                            "settings": {},
                            "coordinates": {"column": 0, "row": 0},
                            "ticks": ticks,
                            "pressed": False,
                        },
                    }
                ),
            )

        pending_metrics = connection.inbound_queue_metrics
        self.assertEqual(pending_metrics.current_depth, 2)
        self.assertEqual(pending_metrics.coalesced, 1)

        connection.run_forever()

        self.assertEqual(received, [("dial-a", 3), ("dial-b", 5)])
        self.assertEqual(connection.inbound_queue_metrics.dispatched, 2)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_does_not_coalesce_rotations_across_same_context_event(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        received: list[str] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(event.event_name)
        connection = WebSocketStreamDockConnection(
            12345,
            coalesce_dial_rotations=True,
        )
        connection.set_listener(listener)
        rotation = {
            "event": "dialRotate",
            "action": "action-uuid",
            "context": "dial-a",
            "device": "device-uuid",
            "payload": {
                "settings": {},
                "coordinates": {"column": 0, "row": 0},
                "ticks": 1,
                "pressed": False,
            },
        }
        key_down = {
            "event": "keyDown",
            "action": "action-uuid",
            "context": "dial-a",
            "device": "device-uuid",
            "payload": {
                "settings": {},
                "coordinates": {"column": 0, "row": 0},
                "isInMultiAction": False,
            },
        }

        for message in (rotation, key_down, rotation):
            connection._on_message(app_factory.return_value, json.dumps(message))

        connection.run_forever()

        self.assertEqual(received, ["dialRotate", "keyDown", "dialRotate"])
        self.assertEqual(connection.inbound_queue_metrics.coalesced, 0)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_does_not_coalesce_rotations_across_broadcast_event(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        received: list[str] = []
        listener.on_stream_dock_event.side_effect = lambda event: received.append(event.event_name)
        connection = WebSocketStreamDockConnection(
            12345,
            coalesce_dial_rotations=True,
        )
        connection.set_listener(listener)
        rotation = json.dumps(
            {
                "event": "dialRotate",
                "action": "action-uuid",
                "context": "dial-a",
                "device": "device-uuid",
                "payload": {
                    "settings": {},
                    "coordinates": {"column": 0, "row": 0},
                    "ticks": 1,
                    "pressed": False,
                },
            }
        )

        for message in (rotation, '{"event":"systemDidWakeUp"}', rotation):
            connection._on_message(app_factory.return_value, message)

        connection.run_forever()

        self.assertEqual(received, ["dialRotate", "systemDidWakeUp", "dialRotate"])
        self.assertEqual(connection.inbound_queue_metrics.coalesced, 0)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_records_callback_failures_without_stopping_dispatch(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        listener.on_stream_dock_event.side_effect = [
            RuntimeError("callback failed"),
            None,
        ]
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)
        connection._on_message(app_factory.return_value, '{"event":"firstEvent"}')
        connection._on_message(app_factory.return_value, '{"event":"secondEvent"}')

        with self.assertLogs("mirabox_sdk.inbound", level="ERROR"):
            connection.run_forever()

        metrics = connection.inbound_queue_metrics
        self.assertEqual(listener.on_stream_dock_event.call_count, 2)
        self.assertEqual(metrics.callback_failures, 1)
        self.assertEqual(metrics.dispatched, 1)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_shutdown_timeout_discards_events_still_queued(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        callback_started = Event()
        callback_finished = Event()
        release_callback = Event()

        def on_event(_event: object) -> None:
            callback_started.set()
            release_callback.wait(1)
            callback_finished.set()

        def read_frames() -> None:
            connection._on_message(app_factory.return_value, key_down_message("hung"))
            self.assertTrue(callback_started.wait(1))
            connection._on_message(
                app_factory.return_value,
                key_down_message("hung", sequence=1),
            )

        listener.on_stream_dock_event.side_effect = on_event
        connection = WebSocketStreamDockConnection(
            12345,
            inbound_shutdown_timeout=0.01,
        )
        connection.set_listener(listener)
        app_factory.return_value.run_forever.side_effect = read_frames

        with self.assertLogs("mirabox_sdk.connection", level="WARNING") as logs:
            connection.run_forever()

        metrics = connection.inbound_queue_metrics
        self.assertEqual(metrics.current_depth, 0)
        self.assertEqual(metrics.dropped_after_shutdown, 1)
        self.assertEqual(metrics.dispatched, 0)
        self.assertEqual(metrics.callback_timeouts, 1)
        self.assertIn("event='keyDown' context='hung'", "\n".join(logs.output))

        release_callback.set()
        self.assertTrue(callback_finished.wait(1))
        connection.close()
        self.assertEqual(connection.inbound_queue_metrics.dispatched, 1)

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_close_from_callback_discards_later_events_without_deadlock(
        self,
        app_factory: Mock,
    ) -> None:
        listener = Mock()
        received: list[str] = []
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)

        def on_event(event: object) -> None:
            received.append(event.event_name)
            connection.close()

        listener.on_stream_dock_event.side_effect = on_event
        connection._on_message(app_factory.return_value, '{"event":"firstEvent"}')
        connection._on_message(app_factory.return_value, '{"event":"secondEvent"}')

        connection.run_forever()

        self.assertEqual(received, ["firstEvent"])
        self.assertEqual(connection.inbound_queue_metrics.dropped_after_shutdown, 1)

    def test_rejects_invalid_queue_configuration(self) -> None:
        default_connection = WebSocketStreamDockConnection(12345)
        self.assertEqual(default_connection._inbound_shutdown_timeout, 5.0)
        default_connection.close()

        for queue_limit in (0, -1, True, 1.5):
            with (
                self.subTest(queue_limit=queue_limit),
                self.assertRaisesRegex(ValueError, "positive integer"),
            ):
                WebSocketStreamDockConnection(
                    12345,
                    inbound_queue_limit=queue_limit,  # type: ignore[arg-type]
                )

        for worker_count in (0, -1, True, 1.5):
            with (
                self.subTest(inbound_worker_count=worker_count),
                self.assertRaisesRegex(ValueError, "positive integer"),
            ):
                WebSocketStreamDockConnection(
                    12345,
                    inbound_worker_count=worker_count,  # type: ignore[arg-type]
                )

        with self.assertRaisesRegex(ValueError, "overflow_policy"):
            WebSocketStreamDockConnection(
                12345,
                overflow_policy="block",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "boolean"):
            WebSocketStreamDockConnection(
                12345,
                coalesce_dial_rotations=1,  # type: ignore[arg-type]
            )
        for timeout in (-1, True, float("nan"), float("inf")):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(ValueError, "finite non-negative"),
            ):
                WebSocketStreamDockConnection(
                    12345,
                    inbound_shutdown_timeout=timeout,
                )
        for queue_limit in (0, -1, True, 1.5):
            with (
                self.subTest(outbound_queue_limit=queue_limit),
                self.assertRaisesRegex(ValueError, "positive integer"),
            ):
                WebSocketStreamDockConnection(
                    12345,
                    outbound_queue_limit=queue_limit,  # type: ignore[arg-type]
                )
        with self.assertRaisesRegex(ValueError, "boolean"):
            WebSocketStreamDockConnection(
                12345,
                coalesce_outbound_commands=1,  # type: ignore[arg-type]
            )
        for timeout in (-1, True, float("nan"), float("inf")):
            with (
                self.subTest(outbound_shutdown_timeout=timeout),
                self.assertRaisesRegex(ValueError, "finite non-negative"),
            ):
                WebSocketStreamDockConnection(
                    12345,
                    outbound_shutdown_timeout=timeout,
                )

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_delegates_connection_lifecycle(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)

        connection.run_forever()
        connection.close()

        web_socket.run_forever.assert_called_once_with()
        web_socket.close.assert_called_once_with()

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_connected_callback_runs_on_websocket_loop_thread(self, app_factory: Mock) -> None:
        loop_thread_ids: list[int] = []
        callback_thread_ids: list[int] = []
        listener = Mock()
        listener.on_stream_dock_connected.side_effect = lambda: callback_thread_ids.append(
            get_ident()
        )
        connection = WebSocketStreamDockConnection(12345)
        connection.set_listener(listener)

        def run_websocket_loop() -> None:
            loop_thread_ids.append(get_ident())
            connection._on_open(app_factory.return_value)

        app_factory.return_value.run_forever.side_effect = run_websocket_loop

        connection.run_forever()

        self.assertEqual(callback_thread_ids, loop_thread_ids)

"""Tests for the internal typed MiraBox SDK package."""

from __future__ import annotations

import ast
import json
import unittest
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import mirabox_sdk
from mirabox_sdk import (
    EVENT_REGISTRY,
    JSON_OBJECT_CODEC,
    Action,
    Controller,
    Coordinates,
    DeviceDidDisconnectEvent,
    DeviceInfo,
    DeviceSize,
    DialRotateEvent,
    DidReceiveGlobalSettingsEvent,
    EventScope,
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
    SetTitleCommand,
    StreamDockCommand,
    StreamDockConnection,
    StreamDockEventType,
    StreamDockPlugin,
    TitleAlignment,
    TitleParameters,
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    UnknownStreamDockEvent,
    UnsupportedEventError,
    ValidatedJsonObject,
    ValidatedWireMessage,
    WebSocketStreamDockConnection,
    WillAppearEvent,
    configure_logging,
    decode_with_codec,
    encode_with_codec,
    parse_plugin_launch_arguments,
    parse_registration_info,
    parse_stream_dock_event,
)
from mirabox_sdk.json_types import clone_json_object


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

        listener.on_stream_dock_event.assert_called_once_with(
            UnknownStreamDockEvent(
                event="futureEvent",
                data={"event": "futureEvent", "payload": {"version": 2}},
            )
        )

    @patch("mirabox_sdk.connection.websocket.WebSocketApp")
    def test_delegates_connection_lifecycle(self, app_factory: Mock) -> None:
        web_socket = app_factory.return_value
        connection = WebSocketStreamDockConnection(12345)

        connection.run_forever()
        connection.close()

        web_socket.run_forever.assert_called_once_with()
        web_socket.close.assert_called_once_with()

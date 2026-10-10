"""Tests for the internal typed MiraBox SDK package."""

from __future__ import annotations

import ast
import copy
import json
import pickle
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import patch

import mirabox_sdk
from mirabox_sdk import (
    JSON_OBJECT_CODEC,
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
    JsonValue,
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
    decode_with_codec,
    encode_with_codec,
    parse_plugin_launch_arguments,
    parse_registration_info,
    parse_stream_dock_event,
)
from mirabox_sdk.json_types import clone_json_object, is_json_value
from mirabox_sdk.parser import EVENT_CODEC_REGISTRY


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


class JsonDepthTests(unittest.TestCase):
    @staticmethod
    def nested_object(depth: int, kind: str, leaf: JsonValue = 1) -> JsonObject:
        value = leaf
        for level in range(depth - 1):
            value = (
                [value] if kind == "lists" or (kind == "mixed" and level % 2) else {"child": value}
            )
        return {"child": value}

    def test_accepts_json_at_the_container_depth_limit(self) -> None:
        for kind in ("dicts", "lists", "mixed"):
            for leaf in (1, {}, []):
                depth = 63 if isinstance(leaf, (dict, list)) else 64
                source = self.nested_object(depth, kind, leaf)
                with self.subTest(kind=kind, leaf=leaf):
                    self.assertTrue(is_json_value(source))
                    for copy in (
                        clone_json_object(source),
                        ValidatedJsonObject(source).isolated_copy(),
                        OwnedJsonPayload(source).isolated_copy(),
                    ):
                        self.assertEqual(copy, source)
                        self.assertIsNot(copy["child"], source["child"])

    def test_rejects_json_beyond_the_container_depth_limit(self) -> None:
        for depth in (65, 600):
            for kind in ("dicts", "lists", "mixed"):
                source = self.nested_object(depth, kind)
                with self.subTest(depth=depth, kind=kind):
                    self.assertFalse(is_json_value(source))
                    for validate in (clone_json_object, ValidatedJsonObject, OwnedJsonPayload):
                        with self.assertRaisesRegex(ValueError, "JSON nesting depth.*64"):
                            validate(source)

    def test_json_predicate_rejects_cycles_and_accepts_shared_containers(self) -> None:
        cyclic_list: list[JsonValue] = []
        cyclic_list.append(cyclic_list)
        cyclic_dict: JsonObject = {}
        cyclic_dict["self"] = cyclic_dict
        for value in (cyclic_list, cyclic_dict):
            with self.subTest(container=type(value).__name__):
                self.assertFalse(is_json_value(value))

        shared = self.nested_object(63, "mixed")
        source: JsonObject = {"first": shared, "second": shared}
        self.assertTrue(is_json_value(source))
        cloned = clone_json_object(source)
        self.assertEqual(cloned, source)
        self.assertIsNot(cloned["first"], cloned["second"])

    def test_extensible_commands_preserve_depth_diagnostics(self) -> None:
        source = self.nested_object(600, "dicts")
        for build in (
            lambda: SetSettingsCommand("button", source),
            lambda: SetGlobalSettingsCommand("plugin", source),
            lambda: SendToPropertyInspectorCommand("action", "button", source),
        ):
            with self.assertRaisesRegex(ValueError, "JSON nesting depth.*64"):
                build()

    def test_codecs_preserve_depth_diagnostics(self) -> None:
        source = self.nested_object(600, "mixed")
        for codec in (
            JSON_OBJECT_CODEC,
            FunctionalJsonCodec[JsonObject](
                decoder=lambda value: value, encoder=lambda value: value
            ),
        ):
            with self.subTest(codec=type(codec).__name__):
                with self.assertRaisesRegex(JsonCodecDecodeError, "JSON nesting depth.*64"):
                    decode_with_codec(source, codec)
                with self.assertRaisesRegex(JsonCodecEncodeError, "JSON nesting depth.*64"):
                    encode_with_codec(source, codec)
                with self.assertRaisesRegex(JsonCodecEncodeError, "JSON nesting depth.*64"):
                    SetSettingsCommand.from_settings("button", source, codec)

    def test_event_parser_preserves_depth_diagnostics(self) -> None:
        source = self.nested_object(600, "dicts")
        with self.assertRaisesRegex(MalformedEventError, "JSON nesting depth.*64"):
            parse_stream_dock_event(
                {"event": "didReceiveGlobalSettings", "payload": {"settings": source}}
            )


class JsonCodecTests(unittest.TestCase):
    def test_public_payloads_support_native_container_operations(self) -> None:
        source: JsonObject = {
            "values": [1, 2],
            "nested": {"items": [{"count": 1}]},
            "empty_object": {},
            "empty_list": [],
        }
        snapshot = ValidatedJsonObject(source)
        payloads = (
            SetSettingsCommand("button", source).settings,
            SetGlobalSettingsCommand("plugin", source).settings,
            SendToPropertyInspectorCommand("action", "button", source).payload,
            snapshot.owned_payload(),
            snapshot.isolated_copy(),
        )
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__):
                values = dict.__getitem__(payload, "values")
                self.assertIs(type(values), list)
                self.assertEqual(list.copy(values), [1, 2])
                self.assertEqual(list.__getitem__(values, slice(None)), [1, 2])
                self.assertEqual(list.__add__(values, [3]), [1, 2, 3])
                self.assertEqual(list.__mul__(values, 2), [1, 2, 1, 2])
                self.assertEqual(list(list.__iter__(values)), [1, 2])
                self.assertEqual(dict.get(payload, "nested"), source["nested"])
                self.assertEqual(dict.copy(payload), source)
                self.assertEqual(list(dict.items(payload)), list(source.items()))
                self.assertEqual(payload.copy(), source)
                self.assertIs(payload.copy()["values"], values)
                self.assertEqual(dict.__or__(payload, {"extra": True}), source | {"extra": True})
                for copied in (copy.deepcopy(payload), pickle.loads(pickle.dumps(payload))):
                    self.assertEqual(copied, source)
                    self.assertIsNot(copied["values"], values)
                for indent in (None, 2):
                    self.assertEqual(json.loads(json.dumps(payload, indent=indent)), source)

                list.append(values, 3)
                dict.__setitem__(payload, "extra", {"count": 2})
                self.assertEqual(payload["values"], [1, 2, 3])
                self.assertEqual(payload["extra"], {"count": 2})
        self.assertEqual(source["values"], [1, 2])
        self.assertEqual(snapshot.isolated_copy(), source)

    def test_public_snapshots_are_plain_and_isolated_after_native_mutations(self) -> None:
        validated = ValidatedJsonObject({"nested": {"values": [{"count": 1}]}})
        payload = validated.owned_payload()
        first = payload.isolated_copy()
        second = payload.isolated_copy()
        self.assertIs(type(first), dict)
        self.assertIs(type(first["nested"]), dict)
        values = first["nested"]["values"]
        self.assertIs(type(values), list)
        self.assertIs(type(values[0]), dict)
        dict.__setitem__(values[0], "count", 2)
        list.append(values, {"count": 3})
        self.assertEqual(second, {"nested": {"values": [{"count": 1}]}})
        self.assertEqual(payload, second)
        self.assertEqual(validated.isolated_copy(), second)

    def test_owned_payload_insertions_and_shallow_copies_share_native_references(self) -> None:
        payload = OwnedJsonPayload({})
        inserted = {"values": [1]}
        payload["inserted"] = inserted
        shallow = copy.copy(payload)
        isolated = payload.isolated_copy()

        self.assertIs(payload["inserted"], inserted)
        self.assertIs(shallow["inserted"], inserted)
        inserted["values"].append(2)
        self.assertEqual(payload, {"inserted": {"values": [1, 2]}})
        self.assertEqual(isolated, {"inserted": {"values": [1]}})

    def test_deep_snapshots_accept_repeated_references_and_reject_cycles(self) -> None:
        shared = {"count": 1}
        payload = OwnedJsonPayload({})
        payload.update(first=shared, second=shared)
        snapshot = payload.isolated_copy()
        self.assertEqual(snapshot, {"first": {"count": 1}, "second": {"count": 1}})
        self.assertIsNot(snapshot["first"], snapshot["second"])
        payload["self"] = payload
        with self.assertRaisesRegex(ValueError, "acyclic"):
            payload.isolated_copy()

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

    def test_validated_wire_message_requires_owned_payload_for_snapshot_composition(
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

    def test_extensible_commands_own_inputs_and_validate_current_contents_for_sending(self) -> None:
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
        settings_command.settings["invalid"] = object()  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            settings_command.to_validated_wire()

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
    def test_event_codec_registry_covers_parser_and_exports(self) -> None:
        self.assertEqual(
            set(EVENT_CODEC_REGISTRY),
            {event_type.value for event_type in StreamDockEventType},
        )
        for wire_name, descriptor in EVENT_CODEC_REGISTRY.items():
            with self.subTest(event=wire_name):
                self.assertEqual(descriptor.wire_name, wire_name)
                self.assertEqual(str(descriptor.event_class.event), wire_name)
                self.assertTrue(callable(descriptor.parser))
                self.assertIs(
                    getattr(mirabox_sdk, descriptor.event_class.__name__),
                    descriptor.event_class,
                )
        with self.assertRaises(TypeError):
            EVENT_CODEC_REGISTRY["futureEvent"] = next(  # type: ignore[index]
                iter(EVENT_CODEC_REGISTRY.values())
            )

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

        self.assertEqual(set(envelopes), set(EVENT_CODEC_REGISTRY))
        for wire_name, envelope in envelopes.items():
            with self.subTest(event=wire_name):
                event = parse_stream_dock_event(envelope)
                self.assertIsInstance(event, EVENT_CODEC_REGISTRY[wire_name].event_class)

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

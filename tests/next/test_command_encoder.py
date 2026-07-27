from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mirabox_sdk import (
    JsonObject,
    LogMessageCommand,
    SetSettingsCommand,
    StreamDockCommand,
    ValidatedWireMessage,
)
from mirabox_sdk._next.protocol.encoder import JsonStreamDockCommandEncoder
from mirabox_sdk._next.protocol.ports import StreamDockCommandEncoder

from .wire_fixtures import known_command_wire_fixtures


class JsonStreamDockCommandEncoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.encoder = JsonStreamDockCommandEncoder()

    def test_implementation_explicitly_inherits_encoder_port(self) -> None:
        self.assertIn(StreamDockCommandEncoder, JsonStreamDockCommandEncoder.__mro__)
        self.assertIsInstance(self.encoder, StreamDockCommandEncoder)

    def test_encodes_every_known_command_with_exact_legacy_wire_output(self) -> None:
        fixtures = known_command_wire_fixtures()
        sdk_command_type_names = {
            command_type.__name__
            for command_type in StreamDockCommand.__subclasses__()
            if command_type.__module__ == "mirabox_sdk.commands"
        }
        self.assertEqual(
            {type(command).__name__ for command, _expected in fixtures},
            sdk_command_type_names,
        )

        for command, expected in fixtures:
            with self.subTest(command=type(command).__name__):
                encoded = self.encoder.encode(command)

                self.assertEqual(
                    encoded,
                    json.dumps(expected, ensure_ascii=False, allow_nan=False),
                )
                self.assertEqual(json.loads(encoded), command.to_wire())

    def test_rejects_non_json_custom_command_values(self) -> None:
        invalid_messages = (
            {"threshold": float("nan")},
            {"unsupported": object()},
            {1: "value"},
            {"items": (1, 2)},
        )
        for message in invalid_messages:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(
                    ValueError,
                    "^Stream Dock command contains a non-JSON value$",
                ),
            ):
                self.encoder.encode(_CustomCommand(message))

    def test_requires_explicit_validated_wire_result(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            r"^command\.to_validated_wire\(\) must return ValidatedWireMessage$",
        ):
            self.encoder.encode(_InvalidValidatedCommand())

    def test_preserves_nested_payload_ownership(self) -> None:
        source: JsonObject = {"profile": {"level": 1}}
        command = SetSettingsCommand("button", source)
        profile = source["profile"]
        assert isinstance(profile, dict)
        profile["level"] = 2

        encoded = self.encoder.encode(command)

        self.assertEqual(
            json.loads(encoded),
            {
                "event": "setSettings",
                "context": "button",
                "payload": {"profile": {"level": 1}},
            },
        )

    def test_preserves_unicode_in_text_frame(self) -> None:
        encoded = self.encoder.encode(LogMessageCommand("Микрофон 🎛️"))

        self.assertIn("Микрофон 🎛️", encoded)
        self.assertNotIn("\\u", encoded)
        self.assertEqual(
            json.loads(encoded),
            {
                "event": "logMessage",
                "payload": {"message": "Микрофон 🎛️"},
            },
        )

    def test_validates_and_serializes_exactly_once(self) -> None:
        command = _CountingCommand()
        json_encoder = self.encoder._json_encoder

        with patch.object(
            json_encoder,
            "encode",
            wraps=json_encoder.encode,
        ) as encode:
            encoded = self.encoder.encode(command)

        self.assertEqual(command.to_wire_calls, 1)
        encode.assert_called_once()
        self.assertEqual(json.loads(encoded), {"event": "counted"})

    def test_reuses_strict_json_encoder_without_circular_recheck(self) -> None:
        json_encoder = self.encoder._json_encoder

        self.encoder.encode(LogMessageCommand("first"))
        self.encoder.encode(LogMessageCommand("second"))

        self.assertIs(self.encoder._json_encoder, json_encoder)
        self.assertFalse(json_encoder.ensure_ascii)
        self.assertFalse(json_encoder.allow_nan)
        self.assertFalse(json_encoder.check_circular)


class _CustomCommand(StreamDockCommand):
    def __init__(self, message: object) -> None:
        self.message = message

    def to_wire(self) -> JsonObject:
        return self.message  # type: ignore[return-value]


class _InvalidValidatedCommand(StreamDockCommand):
    def to_wire(self) -> JsonObject:
        return {"event": "invalidValidated"}

    def to_validated_wire(self) -> ValidatedWireMessage:
        return self.to_wire()  # type: ignore[return-value]


class _CountingCommand(StreamDockCommand):
    def __init__(self) -> None:
        self.to_wire_calls = 0

    def to_wire(self) -> JsonObject:
        self.to_wire_calls += 1
        return {"event": "counted"}


if __name__ == "__main__":
    unittest.main()

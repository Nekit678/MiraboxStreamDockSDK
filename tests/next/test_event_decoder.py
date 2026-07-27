from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mirabox_sdk import (
    EVENT_REGISTRY,
    DidReceiveGlobalSettingsEvent,
    InvalidFieldError,
    MalformedEventError,
    SendToPluginEvent,
    UnknownStreamDockEvent,
    parse_stream_dock_event,
)
from mirabox_sdk._next.protocol.adapters.legacy import LegacyEventParserAdapter
from mirabox_sdk._next.protocol.decoder import JsonStreamDockEventDecoder
from mirabox_sdk._next.protocol.ports import (
    DecodedEventParser,
    StreamDockEventDecoder,
)

from .wire_fixtures import known_event_envelopes


class JsonStreamDockEventDecoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event_parser = LegacyEventParserAdapter()
        self.decoder = JsonStreamDockEventDecoder(self.event_parser)

    def test_implementations_explicitly_inherit_their_ports(self) -> None:
        self.assertIn(DecodedEventParser, LegacyEventParserAdapter.__mro__)
        self.assertIn(StreamDockEventDecoder, JsonStreamDockEventDecoder.__mro__)
        self.assertIsInstance(self.event_parser, DecodedEventParser)
        self.assertIsInstance(self.decoder, StreamDockEventDecoder)

    def test_decodes_every_known_event_with_legacy_parity(self) -> None:
        envelopes = known_event_envelopes()
        self.assertEqual(set(envelopes), set(EVENT_REGISTRY))

        for wire_name, envelope in envelopes.items():
            with self.subTest(event=wire_name):
                frame = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
                decoded = self.decoder.decode(frame)

                self.assertEqual(decoded, parse_stream_dock_event(envelope))
                self.assertIsInstance(decoded, EVENT_REGISTRY[wire_name].event_class)

    def test_reports_malformed_json_as_structured_protocol_error(self) -> None:
        with self.assertRaises(MalformedEventError) as caught:
            self.decoder.decode('{"event":"keyDown","payload":}')

        self.assertIsNone(caught.exception.event_name)
        self.assertEqual(caught.exception.path, ())
        self.assertIn("invalid JSON", caught.exception.reason)
        self.assertIn("line 1 column", caught.exception.reason)
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)

    def test_preserves_invalid_known_field_path(self) -> None:
        frame = json.dumps(
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

        with self.assertRaises(InvalidFieldError) as caught:
            self.decoder.decode(frame)

        self.assertEqual(caught.exception.event_name, "keyDown")
        self.assertEqual(caught.exception.path, ("payload", "isInMultiAction"))
        self.assertEqual(caught.exception.reason, "required field is missing")

    def test_preserves_unknown_event_and_nested_payload(self) -> None:
        frame = json.dumps(
            {
                "event": "futureEvent",
                "payload": {"version": 2, "features": ["unicode", "typed"]},
            }
        )

        event = self.decoder.decode(frame)

        self.assertEqual(
            event,
            UnknownStreamDockEvent(
                event="futureEvent",
                data={
                    "event": "futureEvent",
                    "payload": {
                        "version": 2,
                        "features": ["unicode", "typed"],
                    },
                },
            ),
        )

    def test_rejects_every_non_finite_json_constant(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with (
                self.subTest(constant=constant),
                self.assertRaises(MalformedEventError) as caught,
            ):
                self.decoder.decode(f'{{"event":"futureEvent","payload":{{"value":{constant}}}}}')

            self.assertEqual(
                caught.exception.reason,
                f"invalid JSON: non-finite JSON constant {constant!r}",
            )

    def test_preserves_unicode_without_ascii_round_trip(self) -> None:
        frame = (
            '{"event":"sendToPlugin","action":"действие","context":"кнопка",'
            '"payload":{"event":"обновить","title":"Микрофон 🎛️"}}'
        )

        event = self.decoder.decode(frame)

        self.assertIsInstance(event, SendToPluginEvent)
        self.assertEqual(event.action, "действие")
        self.assertEqual(event.context, "кнопка")
        self.assertEqual(
            event.message.value["title"],
            "Микрофон 🎛️",
        )

    def test_decoded_nested_payloads_are_independently_owned(self) -> None:
        frame = json.dumps(
            {
                "event": "didReceiveGlobalSettings",
                "payload": {"settings": {"audio": {"threshold": 0.5}}},
            }
        )

        first = self.decoder.decode(frame)
        second = self.decoder.decode(frame)
        self.assertIsInstance(first, DidReceiveGlobalSettingsEvent)
        self.assertIsInstance(second, DidReceiveGlobalSettingsEvent)
        first_settings = first.settings
        first_audio = first_settings["audio"]
        assert isinstance(first_audio, dict)
        first_audio["threshold"] = 0.75

        self.assertEqual(second.settings, {"audio": {"threshold": 0.5}})

    def test_decodes_json_once(self) -> None:
        frame = '{"event":"systemDidWakeUp"}'
        json_decoder = self.decoder._json_decoder

        with patch.object(
            json_decoder,
            "decode",
            wraps=json_decoder.decode,
        ) as decode:
            self.decoder.decode(frame)

        decode.assert_called_once_with(frame)

    def test_reuses_strict_json_decoder(self) -> None:
        json_decoder = self.decoder._json_decoder

        self.decoder.decode('{"event":"systemDidWakeUp"}')
        self.decoder.decode('{"event":"systemDidWakeUp"}')

        self.assertIs(self.decoder._json_decoder, json_decoder)

    def test_requires_a_text_frame(self) -> None:
        with self.assertRaisesRegex(TypeError, "^frame must be a string$"):
            self.decoder.decode(b'{"event":"systemDidWakeUp"}')  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

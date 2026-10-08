"""Production-stack error diagnostics and privacy regression tests."""

from __future__ import annotations

import unittest
from io import StringIO
from pathlib import Path

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    FunctionalJsonCodec,
    InvalidFieldError,
    JsonCodecDecodeError,
    JsonObject,
    KeyDownEvent,
    Plugin,
    SdkDiagnostic,
    SystemDidWakeUpEvent,
    UnknownStreamDockEvent,
    configure_logging,
    create_stream_dock_application,
)
from mirabox_sdk.testing import StreamDockHarness
from tests.test_testing import _launch_arguments

_SECRET = "diagnostic-secret-must-not-be-logged"
_LOCAL_SECRET = "callback-local-must-not-be-logged"
_ACTION = "com.example.diagnostics"


def _send_action_event(
    harness: StreamDockHarness,
    event: str,
    context: str = "button",
    settings: JsonObject | None = None,
) -> None:
    harness.send_event(
        event,
        action=_ACTION,
        context=context,
        device="device",
        payload={
            "settings": {} if settings is None else settings,
            "coordinates": {"column": 0, "row": 0},
            "controller": "Keypad",
            "isInMultiAction": False,
        },
    )


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_logging(enabled=False)

    def test_parser_logs_schema_field_and_not_untrusted_reason(self) -> None:
        output = StringIO()
        configure_logging(level="DEBUG", stream=output)
        with StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
        ) as harness:
            harness.send_event(
                "keyDown", action=_ACTION, context="button", payload={"token": _SECRET}
            )
            harness.send_event(
                "dialDown",
                action=_ACTION,
                context="button",
                device="device",
                payload={
                    "settings": {},
                    "coordinates": {"column": 0, "row": 0},
                    "controller": _SECRET,
                },
            )
            harness.send_event("systemDidWakeUp")
            harness.wait_for_events(1)
            self.assertEqual(
                harness.application.metrics().boundary.event_reader.protocol_failures, 2
            )
        configure_logging(enabled=False)
        logs = output.getvalue()
        self.assertIn("category=protocol_error event keyDown", logs)
        self.assertIn("field_path=$.device", logs)
        self.assertIn("field_path=$.payload.controller", logs)
        self.assertNotIn(_SECRET, logs)

    def test_action_error_has_source_location_and_original_exception_for_observer(self) -> None:
        failure = RuntimeError(_SECRET)
        diagnostics: list[SdkDiagnostic] = []
        registry = ActionRegistry[ApplicationContext]()

        @registry.register(_ACTION)
        class BrokenAction(Action[JsonObject, ApplicationContext]):
            def on_key_down(self, event: KeyDownEvent) -> None:
                local_value = _LOCAL_SECRET
                assert local_value and event.settings["token"] == _SECRET
                raise failure

        output = StringIO()
        configure_logging(level="DEBUG", stream=output)
        with StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            error_observer=diagnostics.append,
        ) as harness:
            _send_action_event(harness, "willAppear")
            _send_action_event(harness, "keyDown", settings={"token": _SECRET})
            harness.wait_for_events(2)
            self.assertEqual(harness.application.metrics().scheduler.callback_failures, 1)
        configure_logging(enabled=False)
        self.assertEqual(len(diagnostics), 1)
        diagnostic = diagnostics[0]
        self.assertEqual(diagnostic.category, "callback_error")
        self.assertEqual(diagnostic.event_name, "keyDown")
        self.assertEqual(diagnostic.callback, "on_key_down")
        self.assertEqual(diagnostic.context, "button")
        self.assertIs(diagnostic.error, failure)
        location = diagnostic.source_locations[-1]
        self.assertEqual(Path(location.filename), Path(__file__))
        self.assertEqual(location.function, "on_key_down")
        self.assertGreater(location.line, 0)
        logs = output.getvalue()
        self.assertIn(f"{location.filename}:{location.line} in on_key_down", logs)
        for secret in (_SECRET, _LOCAL_SECRET):
            self.assertNotIn(secret, logs)
            self.assertNotIn(secret, repr(diagnostic))

    def test_observer_receives_protocol_errors_with_logging_disabled(self) -> None:
        configure_logging(enabled=False)
        diagnostics: list[SdkDiagnostic] = []
        with StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
            error_observer=diagnostics.append,
        ) as harness:
            harness.send_json("{broken")
            harness.send_event("keyDown", context="button")
            harness.send_event("systemDidWakeUp")
            harness.wait_for_events(1)
        self.assertEqual(len(diagnostics), 2)
        self.assertIsInstance(diagnostics[1].error, InvalidFieldError)
        self.assertEqual(diagnostics[1].field_path, ("action",))
        self.assertEqual(diagnostics[1].event_name, "keyDown")

    def test_plugin_and_each_broadcast_failure_are_observed_without_blocking_other_actions(
        self,
    ) -> None:
        diagnostics: list[SdkDiagnostic] = []
        delivered: list[str] = []
        registry = ActionRegistry[ApplicationContext]()

        @registry.register(_ACTION)
        class BroadcastAction(Action[JsonObject, ApplicationContext]):
            def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
                if self.context.startswith("broken"):
                    raise RuntimeError(_SECRET)
                delivered.append(self.context)

        class BrokenPlugin(Plugin):
            def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
                raise RuntimeError(_SECRET)

            def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
                raise ValueError(_SECRET)

        with StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            plugin=BrokenPlugin(),
            error_observer=diagnostics.append,
        ) as harness:
            for context in ("broken-1", "broken-2", "healthy"):
                _send_action_event(harness, "willAppear", context)
            harness.send_event("systemDidWakeUp")
            harness.send_event("futureEvent", payload={"token": _SECRET})
            harness.wait_for_events(5)
        self.assertEqual(delivered, ["healthy"])
        self.assertEqual(
            [(item.callback, item.context) for item in diagnostics],
            [
                ("on_system_did_wake_up", None),
                ("on_system_did_wake_up", "broken-1"),
                ("on_system_did_wake_up", "broken-2"),
                ("on_unhandled_event", None),
            ],
        )

    def test_codec_path_redacts_arbitrary_keys_and_retains_cause_location(self) -> None:
        diagnostics: list[SdkDiagnostic] = []
        registry = ActionRegistry[ApplicationContext]()

        def decode(settings: JsonObject) -> JsonObject:
            if settings.get("fail"):
                raise JsonCodecDecodeError(_SECRET, path=(_SECRET, 0, "title"))
            return settings

        @registry.register(_ACTION)
        class TypedAction(Action[JsonObject, ApplicationContext]):
            settings_codec = FunctionalJsonCodec(decoder=decode, encoder=lambda value: value)

        output = StringIO()
        configure_logging(level="DEBUG", stream=output)
        with StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            error_observer=diagnostics.append,
        ) as harness:
            _send_action_event(harness, "willAppear", "bad", {"fail": True})
            _send_action_event(harness, "willAppear")
            _send_action_event(harness, "didReceiveSettings", settings={"fail": True})
            harness.wait_for_events(3)
        configure_logging(enabled=False)
        self.assertEqual(len(diagnostics), 2)
        for diagnostic in diagnostics:
            self.assertEqual(diagnostic.field_path, ("payload", "settings", "<key>", 0, "<key>"))
            self.assertEqual(diagnostic.source_locations[-1].function, "decode")
            self.assertIn(_SECRET, str(diagnostic.error))
        self.assertNotIn(_SECRET, output.getvalue())
        self.assertIn("field_path=$.payload.settings.<key>[0].<key>", output.getvalue())

    def test_observer_failure_is_isolated_and_does_not_recurse(self) -> None:
        observed: list[SdkDiagnostic] = []
        delivered: list[str] = []

        def observer(diagnostic: SdkDiagnostic) -> None:
            observed.append(diagnostic)
            raise RuntimeError(_SECRET)

        class BrokenPlugin(Plugin):
            def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
                if event.event_name == "broken":
                    raise ValueError(_SECRET)
                delivered.append(event.event_name)

        output = StringIO()
        configure_logging(level="INFO", stream=output)
        with StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
            plugin=BrokenPlugin(),
            error_observer=observer,
        ) as harness:
            harness.send_event("keyDown")
            harness.send_event("broken")
            harness.send_event("healthy")
            harness.wait_for_events(2)
        configure_logging(enabled=False)
        self.assertEqual(len(observed), 2)
        self.assertEqual(delivered, ["healthy"])
        self.assertEqual(output.getvalue().count("SDK error observer failed"), 2)
        self.assertNotIn(_SECRET, output.getvalue())

    def test_plugin_lifecycle_errors_reach_observer(self) -> None:
        diagnostics: list[SdkDiagnostic] = []

        class BrokenPlugin(Plugin):
            def on_ready(self) -> None:
                raise RuntimeError(_SECRET)

            def on_stop(self) -> None:
                raise ValueError(_SECRET)

        with StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
            plugin=BrokenPlugin(),
            error_observer=diagnostics.append,
        ) as harness:
            harness.send_event("systemDidWakeUp")
            harness.wait_for_events(1)
        self.assertEqual([item.callback for item in diagnostics], ["on_ready", "on_stop"])

    def test_public_factory_rejects_invalid_observer_before_constructing_boundary(self) -> None:
        with self.assertRaisesRegex(TypeError, "error_observer must be callable or None"):
            create_stream_dock_application(
                _launch_arguments(),
                action_factory=ActionRegistry[ApplicationContext](),
                error_observer=object(),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()

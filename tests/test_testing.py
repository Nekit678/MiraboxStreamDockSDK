"""Consumer integration tests using only supported SDK imports."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from threading import Event
from threading import enumerate as enumerate_threads
from unittest.mock import patch

from mirabox_sdk import (
    Action,
    ActionRegistry,
    JsonObject,
    LogMessageCommand,
    Plugin,
    PluginLaunchArguments,
    SendToPropertyInspectorCommand,
    StreamDockCommand,
    StreamDockSender,
    UnknownStreamDockEvent,
    WillAppearEvent,
    parse_plugin_launch_arguments,
)
from mirabox_sdk.testing import FakeStreamDockSender, StreamDockHarness


def _launch_arguments() -> PluginLaunchArguments:
    return parse_plugin_launch_arguments(
        port=12345,
        plugin_uuid="plugin-uuid",
        register_event="registerPlugin",
        info={
            "application": {
                "language": "en",
                "platform": "windows",
                "platformVersion": "11",
                "version": "2.10",
            },
            "colors": {},
            "devicePixelRatio": 1,
            "devices": [],
            "plugin": {"uuid": "plugin-uuid", "version": "0.1.0"},
        },
    )


@dataclass(frozen=True)
class Dependencies:
    stream_dock: StreamDockSender


class FakeSenderTests(unittest.TestCase):
    def test_sync_and_async_commands_record_isolated_messages(self) -> None:
        sender = FakeStreamDockSender()
        sender.send(LogMessageCommand("sync"))
        completion = sender.send_async(LogMessageCommand("async"))
        self.assertTrue(completion.done())
        self.assertIsNone(completion.result(0))
        self.assertEqual(
            [message["payload"] for message in sender.messages],
            [{"message": "sync"}, {"message": "async"}],
        )
        sender.messages[0]["payload"] = {}
        self.assertEqual(sender.messages[0]["payload"], {"message": "sync"})

    def test_serialization_failure_is_preserved_in_async_completion(self) -> None:
        class InvalidCommand(StreamDockCommand):
            def to_wire(self) -> JsonObject:
                raise RuntimeError("invalid command")

        sender = FakeStreamDockSender()
        completion = sender.send_async(InvalidCommand())
        self.assertTrue(completion.done())
        with self.assertRaisesRegex(RuntimeError, "invalid command"):
            completion.result(0)
        with self.assertRaisesRegex(RuntimeError, "invalid command"):
            sender.send(InvalidCommand())
        self.assertEqual(sender.messages, ())


class HarnessTests(unittest.TestCase):
    def make_harness(self, *, plugin: Plugin | None = None) -> StreamDockHarness:
        return StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[Dependencies](),
            action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
            plugin=plugin,
        )

    def test_construction_is_idle_and_context_exit_joins_workers_without_websocket(self) -> None:
        before = {thread.ident for thread in enumerate_threads()}
        harness = self.make_harness()
        self.assertEqual(before, {thread.ident for thread in enumerate_threads()})
        self.assertFalse(harness.context.session_readiness.ready)
        with patch("websocket.WebSocketApp", side_effect=AssertionError("socket opened")):
            with harness:
                harness.assert_registration()
                self.assertEqual(
                    harness.receive(), {"event": "registerPlugin", "uuid": "plugin-uuid"}
                )
                self.assertEqual(
                    harness.receive(), {"event": "getGlobalSettings", "context": "plugin-uuid"}
                )
                self.assertTrue(harness.context.session_readiness.ready)
                harness.messages[0]["uuid"] = "mutated"
                harness.assert_registration()
        self.assertTrue(harness.context.session_readiness.terminal)
        self.assertEqual(before, {thread.ident for thread in enumerate_threads()})
        harness.stop()

    def test_inbound_waits_for_readiness_then_runs_real_action_and_command_pipeline(self) -> None:
        registry = ActionRegistry[Dependencies]()
        seen = Event()

        @registry.register("com.example.action")
        class ExampleAction(Action[JsonObject, Dependencies]):
            def on_will_appear(self, event: WillAppearEvent) -> None:
                self.set_title("ready")
                seen.set()

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
        )
        try:
            harness.start(connect=False)
            harness.send_event(
                "willAppear",
                action="com.example.action",
                context="button",
                device="device",
                payload={
                    "settings": {},
                    "coordinates": {"column": 0, "row": 0},
                    "isInMultiAction": False,
                    "controller": "Keypad",
                },
            )
            with self.assertRaises(TimeoutError):
                harness.wait_for_events(1, timeout=0.01)
            self.assertFalse(seen.is_set())
            harness.connect()
            harness.wait_ready()
            harness.wait_for_events(1)
            self.assertTrue(seen.is_set())
            harness.assert_registration()
            harness.assert_command(
                "setTitle", context="button", payload={"title": "ready", "target": 0}
            )
            with self.assertRaises(AssertionError):
                harness.assert_command("setTitle", context="wrong")
            with self.assertRaises(RuntimeError):
                harness.connect()
        finally:
            harness.stop()

    def test_malformed_json_is_rejected_and_next_valid_event_is_delivered(self) -> None:
        observed: list[str] = []

        class Observer(Plugin):
            def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
                observed.append(event.event)

        with self.make_harness(plugin=Observer()) as harness:
            harness.send_json("{broken")
            harness.send_event("futureEvent", payload={"value": 1})
            harness.wait_for_events(1)
            self.assertEqual(observed, ["futureEvent"])
            self.assertEqual(
                harness.application.metrics().boundary.event_reader.protocol_failures, 1
            )

    def test_waits_are_bounded_and_validate_inputs(self) -> None:
        harness = self.make_harness()
        try:
            harness.start(connect=False)
            with self.assertRaises(TimeoutError):
                harness.wait_ready(timeout=0.01)
            with self.assertRaises(TimeoutError):
                harness.receive(timeout=0.01)
            with self.assertRaises(ValueError):
                harness.wait_for_events(-1)
            with self.assertRaises(ValueError):
                harness.wait_ready(timeout=float("inf"))
            with self.assertRaises(ValueError):
                harness.send_event("")
        finally:
            harness.stop()

    def test_command_assertion_checks_action_uuid_and_payload(self) -> None:
        with self.make_harness() as harness:
            harness.context.stream_dock.send(
                SendToPropertyInspectorCommand("com.example.action", "button", {"value": 1})
            )
            harness.assert_command(
                "sendToPropertyInspector",
                action="com.example.action",
                context="button",
                payload={"value": 1},
            )
            with self.assertRaises(AssertionError):
                harness.assert_command("sendToPropertyInspector", action="com.example.wrong")

    def test_stop_reports_timeout_and_can_be_joined_after_service_unblocks(self) -> None:
        release = Event()

        class SlowService:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                release.wait(2)

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[Dependencies](),
            action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
            services=(SlowService(),),
        )
        harness.start()
        try:
            with self.assertRaises(TimeoutError):
                harness.stop(timeout=0.01)
        finally:
            release.set()
            harness.stop()

    def test_stop_before_start_is_terminal_and_start_cannot_be_repeated(self) -> None:
        harness = self.make_harness()
        harness.stop()
        harness.stop()
        with self.assertRaises(RuntimeError):
            harness.start()
        with self.assertRaises(RuntimeError):
            harness.send_event("futureEvent")
        with self.make_harness() as running:
            with self.assertRaises(RuntimeError):
                running.start()

    def test_ready_callback_failure_keeps_runtime_failure_isolation_and_cleanup(self) -> None:
        stopped = Event()
        failure = ValueError("ready failed")

        class BrokenPlugin(Plugin):
            def on_ready(self) -> None:
                raise failure

            def on_stop(self) -> None:
                stopped.set()

        harness = self.make_harness(plugin=BrokenPlugin())
        with harness:
            harness.send_event("futureEvent")
            harness.wait_for_events(1)
            self.assertTrue(harness.context.session_readiness.ready)
        self.assertTrue(stopped.is_set())

    def test_service_start_failure_is_propagated_without_waiting_for_transport(self) -> None:
        failure = ValueError("service failed")

        class BrokenService:
            def start(self) -> None:
                raise failure

            def stop(self) -> None:
                raise AssertionError("failed service must roll back its own startup")

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[Dependencies](),
            action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
            services=(BrokenService(),),
        )
        with self.assertRaises(ValueError) as raised:
            harness.start()
        self.assertIs(raised.exception, failure)
        self.assertTrue(harness.context.session_readiness.terminal)

    def test_context_exit_preserves_body_error_and_cleans_up_plugin(self) -> None:
        stopped = Event()

        class BrokenCleanupService:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                stopped.set()
                raise ValueError("cleanup failed")

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[Dependencies](),
            action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
            services=(BrokenCleanupService(),),
        )
        failure = LookupError("test body failed")
        with self.assertRaises(LookupError) as raised:
            with harness:
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertTrue(stopped.is_set())
        self.assertTrue(any("cleanup failed" in note for note in failure.__notes__))

    def test_shutdown_finishes_accepted_commands(self) -> None:
        harness = self.make_harness()
        harness.start()
        completions = [
            harness.context.stream_dock.send_async(LogMessageCommand(str(i))) for i in range(10)
        ]
        harness.stop()
        self.assertTrue(all(completion.done() for completion in completions))
        for completion in completions:
            completion.result(0)
        self.assertEqual(len(harness.messages), 12)

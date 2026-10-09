"""Reproducible service lifecycle tests using only public SDK testing APIs."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from heartbeat_plugin import __main__ as entrypoint
from heartbeat_plugin import bootstrap
from heartbeat_plugin.service import HeartbeatService

from mirabox_sdk import (
    CommandFuture,
    JsonObject,
    OutboundCommandBusClosedError,
    PluginLaunchArguments,
    StreamDockCommand,
    parse_registration_info,
    validate_plugin,
)
from mirabox_sdk.testing import FakeStreamDockSender, StreamDockHarness


def registration_info() -> JsonObject:
    return {
        "application": {
            "language": "en",
            "platform": "windows",
            "platformVersion": "11",
            "version": "2.10.179.426",
        },
        "colors": {},
        "devicePixelRatio": 1,
        "devices": [],
        "plugin": {"uuid": bootstrap.PLUGIN_UUID, "version": "0.1.0"},
    }


def launch_arguments() -> PluginLaunchArguments:
    return PluginLaunchArguments(
        port=12345,
        plugin_uuid=bootstrap.PLUGIN_UUID,
        register_event="registerPlugin",
        info=parse_registration_info(registration_info()),
    )


class _Ready:
    ready = True
    terminal = False
    failure = None

    def wait(self, timeout: float | None = None) -> bool:
        return self.ready


class _FailedSender(FakeStreamDockSender):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        raise self.error


class _PendingSender(FakeStreamDockSender):
    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        return CommandFuture()


class BackgroundServiceTests(unittest.TestCase):
    def _harness(self) -> StreamDockHarness:
        with patch.object(bootstrap, "create_stream_dock_application", StreamDockHarness):
            return bootstrap.build_application(launch_arguments())

    def _service(self, sender: FakeStreamDockSender | None = None) -> HeartbeatService:
        harness = StreamDockHarness(
            launch_arguments(),
            action_factory=bootstrap.ACTION_REGISTRY,
            action_dependencies_factory=lambda context: context,
        )
        return HeartbeatService(
            replace(
                harness.context,
                stream_dock=sender if sender is not None else FakeStreamDockSender(),
                session_readiness=_Ready(),
            )
        )

    def test_bootstrap_waits_for_readiness_replays_actions_and_joins(self) -> None:
        harness = self._harness()
        harness.start(connect=False)
        try:
            self.assertFalse(harness.context.session_readiness.ready)
            self.assertEqual(harness.messages, ())
            harness.connect()
            harness.wait_ready()
            harness.assert_registration()
            messages = [harness.receive() for _ in range(3)]
            self.assertEqual(messages[-1]["event"], "logMessage")
            self.assertEqual(messages[-1]["payload"], {"message": "Heartbeat 1"})
            identity = {
                "action": bootstrap.ACTION_UUID,
                "context": "button",
                "device": "device",
            }
            payload = {
                "settings": {},
                "coordinates": {"column": 0, "row": 0},
                "isInMultiAction": False,
            }
            harness.send_event(
                "willAppear", **identity, payload={**payload, "controller": "Keypad"}
            )
            harness.send_event("keyDown", **identity, payload=payload)
            harness.wait_for_events(2)
            harness.assert_command(
                "setTitle", context="button", payload={"title": "Pressed", "target": 0}
            )
        finally:
            harness.stop()
        self.assertTrue(harness.application.shutdown_outcome.successful)
        self.assertFalse(any(thread.name == "plugin-heartbeat" for thread in threading.enumerate()))

    def test_shutdown_before_readiness_does_not_send_a_heartbeat(self) -> None:
        harness = self._harness()
        harness.start(connect=False)
        harness.stop()
        harness.stop()
        self.assertFalse(any(message.get("event") == "logMessage" for message in harness.messages))
        self.assertTrue(harness.application.shutdown_outcome.successful)

    def test_observer_reports_protocol_and_callback_errors_without_exception_text(self) -> None:
        with patch.object(bootstrap, "observe_error", wraps=bootstrap.observe_error) as observer:
            harness = self._harness()
        with patch.object(
            bootstrap.StatusAction,
            "on_key_down",
            side_effect=RuntimeError("sensitive callback text"),
        ):
            with self.assertLogs("mirabox_sdk.examples.heartbeat", level="ERROR") as records:
                with harness:
                    identity = {
                        "action": bootstrap.ACTION_UUID,
                        "context": "button",
                        "device": "device",
                    }
                    payload = {
                        "settings": {},
                        "coordinates": {"column": 0, "row": 0},
                        "isInMultiAction": False,
                    }
                    harness.send_event("keyDown", **identity, payload={})
                    harness.send_event(
                        "willAppear", **identity, payload={**payload, "controller": "Keypad"}
                    )
                    harness.send_event("keyDown", **identity, payload=payload)
                    harness.wait_for_events(2)
        self.assertEqual(
            [call.args[0].category for call in observer.call_args_list],
            ["protocol_error", "callback_error"],
        )
        self.assertNotIn("sensitive callback text", " ".join(records.output))

    def test_source_bundle_has_matching_registry_and_only_lacks_the_executable(self) -> None:
        source = Path(__file__).resolve().parents[1] / "com.example.heartbeat.sdPlugin"
        # Keep this test valid after a developer has assembled the real bundle.
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / source.name
            shutil.copytree(source, bundle, ignore=shutil.ignore_patterns("HeartbeatPlugin.exe"))
            issues = validate_plugin(bundle, action_uuids=bootstrap.ACTION_REGISTRY.action_uuids)
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("HeartbeatPlugin.exe", str(issues[0]))

    def test_cli_returns_failure_without_logging_sensitive_worker_exception(self) -> None:
        application = Mock()
        application.run.side_effect = OSError("sensitive worker failure")
        with (
            patch.object(entrypoint, "build_application", return_value=application),
            patch.object(entrypoint, "configure_logging"),
            self.assertLogs("mirabox_sdk.examples.heartbeat.lifecycle", level="ERROR") as records,
        ):
            result = entrypoint.main(
                [
                    "-port",
                    "12345",
                    "-pluginUUID",
                    bootstrap.PLUGIN_UUID,
                    "-registerEvent",
                    "registerPlugin",
                    "-info",
                    json.dumps(registration_info()),
                ]
            )
        self.assertEqual(result, 1)
        application.stop.assert_called_once()
        self.assertIn("exception_type=OSError", " ".join(records.output))
        self.assertNotIn("sensitive worker failure", " ".join(records.output))

    def test_shared_stop_signal_ends_worker_before_service_cleanup(self) -> None:
        harness = StreamDockHarness(
            launch_arguments(),
            action_factory=bootstrap.ACTION_REGISTRY,
            action_dependencies_factory=lambda context: context,
        )
        sender = FakeStreamDockSender()
        service = HeartbeatService(
            replace(harness.context, stream_dock=sender, session_readiness=_Ready())
        )
        service.start()
        try:
            harness.application.stop()
            self.assertTrue(service.finished.wait(2))
            self.assertIsNone(service.failure)
        finally:
            service.stop()

    def test_worker_failure_reaches_application_shutdown_outcome_without_secret_logs(self) -> None:
        failure = OSError("sensitive failure text")
        services = []

        def factory(context):
            service = HeartbeatService(replace(context, stream_dock=_FailedSender(failure)))
            services.append(service)
            return service

        with patch.object(bootstrap, "HeartbeatService", factory):
            harness = self._harness()
        with self.assertLogs("mirabox_sdk.examples.heartbeat", level="ERROR") as records:
            harness.start()
            self.assertTrue(services[0].finished.wait(2))
        self.assertNotIn("sensitive failure text", " ".join(records.output))
        with self.assertRaisesRegex(RuntimeError, "Heartbeat worker failed") as caught:
            harness.stop()
        self.assertIs(caught.exception.__cause__, failure)
        outcome = harness.application.shutdown_outcome
        self.assertTrue(outcome.complete)
        self.assertFalse(outcome.successful)
        self.assertIs(outcome.cleanup_failures[0].error.__cause__, failure)

    def test_command_wait_timeout_is_retained_and_joined(self) -> None:
        service = self._service(_PendingSender())
        with self.assertLogs("mirabox_sdk.examples.heartbeat", level="ERROR"):
            service.start()
            self.assertTrue(service.finished.wait(2))
        self.assertIsInstance(service.failure, TimeoutError)
        with self.assertRaisesRegex(RuntimeError, "Heartbeat worker failed"):
            service.stop()

    def test_closed_bus_without_cancellation_is_a_failure(self) -> None:
        service = self._service(_FailedSender(OutboundCommandBusClosedError("closed")))
        with self.assertLogs("mirabox_sdk.examples.heartbeat", level="ERROR"):
            service.start()
            self.assertTrue(service.finished.wait(2))
        with self.assertRaisesRegex(RuntimeError, "Heartbeat worker failed"):
            service.stop()

    def test_closed_bus_during_shared_cancellation_is_expected(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class ClosingSender(FakeStreamDockSender):
            def send_async(self, command: StreamDockCommand) -> CommandFuture:
                entered.set()
                release.wait(2)
                raise OutboundCommandBusClosedError("closed during shutdown")

        harness = StreamDockHarness(
            launch_arguments(),
            action_factory=bootstrap.ACTION_REGISTRY,
            action_dependencies_factory=lambda context: context,
        )
        service = HeartbeatService(
            replace(harness.context, stream_dock=ClosingSender(), session_readiness=_Ready())
        )
        service.start()
        try:
            self.assertTrue(entered.wait(2))
            harness.application.stop()
            release.set()
            self.assertTrue(service.finished.wait(2))
            self.assertIsNone(service.failure)
        finally:
            release.set()
            service.stop()

    def test_stop_before_start_is_safe_and_terminal(self) -> None:
        service = self._service()
        service.stop()
        service.stop()
        with self.assertRaises(RuntimeError):
            service.start()

    def test_terminal_readiness_preserves_initialization_failure(self) -> None:
        failure = ConnectionError("initialization failed")
        service = self._service()
        readiness = _Ready()
        readiness.ready = False
        readiness.terminal = True
        readiness.failure = failure
        service._context = replace(service._context, session_readiness=readiness)
        with self.assertLogs("mirabox_sdk.examples.heartbeat", level="ERROR"):
            service.start()
            self.assertTrue(service.finished.wait(2))
        self.assertIs(service.failure, failure)
        with self.assertRaises(RuntimeError) as caught:
            service.stop()
        self.assertIs(caught.exception.__cause__, failure)

    def test_partial_startup_failure_rolls_back_and_stop_is_idempotent(self) -> None:
        service = self._service()
        with patch("heartbeat_plugin.service.Thread.start", side_effect=OSError("cannot start")):
            with self.assertRaises(OSError):
                service.start()
        service.stop()
        service.stop()
        self.assertTrue(service.finished.is_set())
        with self.assertRaises(RuntimeError):
            service.start()

    def test_failed_join_retains_worker_until_it_can_be_joined(self) -> None:
        service = self._service()
        entered = threading.Event()
        release = threading.Event()

        def blocked_worker():
            entered.set()
            release.wait(2)

        with patch.object(service, "_run", blocked_worker):
            service.start()
        self.assertTrue(entered.wait(2))
        try:
            with patch("heartbeat_plugin.service.JOIN_TIMEOUT", 0.01):
                with self.assertRaisesRegex(TimeoutError, "resources remain owned"):
                    service.stop()
        finally:
            release.set()
            service.stop()
        service.stop()


if __name__ == "__main__":
    unittest.main()

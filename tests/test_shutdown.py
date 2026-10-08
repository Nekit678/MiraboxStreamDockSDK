"""Observable shutdown guarantees exercised through the public harness."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from threading import Event
from time import monotonic, sleep

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    JsonObject,
    Plugin,
    RuntimeDispatcherConfig,
    RuntimeSchedulerKind,
)
from mirabox_sdk.testing import StreamDockHarness
from tests.test_testing import _launch_arguments


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = monotonic() + 1
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("shutdown did not finish")
        sleep(0.001)


def _appear(harness: StreamDockHarness) -> None:
    harness.send_event(
        "willAppear",
        action="test.action",
        context="button",
        device="device",
        payload={
            "settings": {},
            "coordinates": {"column": 0, "row": 0},
            "controller": "Keypad",
            "isInMultiAction": False,
        },
    )


def _key_down(harness: StreamDockHarness) -> None:
    harness.send_event(
        "keyDown",
        action="test.action",
        context="button",
        device="device",
        payload={
            "settings": {},
            "coordinates": {"column": 0, "row": 0},
            "controller": "Keypad",
            "isInMultiAction": False,
        },
    )


class ShutdownTests(unittest.TestCase):
    def test_system_exit_in_plugin_ready_reaches_supervisor_and_runs_plugin_cleanup(self) -> None:
        cause = SystemExit(3)
        entered, stopped = Event(), Event()

        class ExitingPlugin(Plugin):
            def on_ready(self) -> None:
                entered.set()
                raise cause

            def on_stop(self) -> None:
                stopped.set()

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
            plugin=ExitingPlugin(),
        )
        harness.start(connect=False)
        harness.connect()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(RuntimeError) as raised:
            harness.stop(timeout=1)
        self.assertIs(raised.exception.__cause__, cause)
        self.assertTrue(stopped.is_set())
        self.assertTrue(harness.application.shutdown_outcome.complete)

    def test_cooperative_callback_exits_before_resources_close_without_timeout(self) -> None:
        entered, finished, released = Event(), Event(), Event()
        registry = ActionRegistry[ApplicationContext]()

        @registry.register("test.action")
        class CooperativeAction(Action[JsonObject, ApplicationContext]):
            def on_will_appear(self, event: object) -> None:
                entered.set()
                self.dependencies.stop_signal.wait()
                finished.set()

            def on_will_disappear(self) -> None:
                if not finished.is_set():
                    raise AssertionError("resource closed before callback termination")
                released.set()

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            runtime_config=RuntimeDispatcherConfig(shutdown_timeout=0.5),
        )
        harness.start()
        _appear(harness)
        self.assertTrue(entered.wait(1))
        harness.stop(timeout=1)
        self.assertTrue(finished.is_set())
        self.assertTrue(released.is_set())
        self.assertTrue(harness.application.shutdown_outcome.successful)

    def test_system_exit_terminalizes_owned_event_and_reports_original_cause(self) -> None:
        for kind in (RuntimeSchedulerKind.KEYED_SERIAL, RuntimeSchedulerKind.SEQUENTIAL):
            for barrier in (False, True):
                with self.subTest(kind=kind, barrier=barrier):
                    self._assert_system_exit(kind, barrier)

    def _assert_system_exit(self, kind: RuntimeSchedulerKind, barrier: bool) -> None:
        entered = Event()
        cause = SystemExit(3)
        registry = ActionRegistry[ApplicationContext]()

        @registry.register("test.action")
        class ExitingAction(Action[JsonObject, ApplicationContext]):
            def on_will_appear(self, event: object) -> None:
                if barrier:
                    entered.set()
                    raise cause

            def on_key_down(self, event: object) -> None:
                entered.set()
                raise cause

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            runtime_config=RuntimeDispatcherConfig(worker_count=1, scheduler_kind=kind),
        )
        harness.start()
        _appear(harness)
        if not barrier:
            harness.wait_for_events(1)
            _key_down(harness)
        self.assertTrue(entered.wait(1))
        with self.assertRaises(RuntimeError) as raised:
            harness.stop(timeout=1)
        self.assertIs(raised.exception.__cause__, cause)
        metrics = harness.application.metrics()
        self.assertEqual(metrics.scheduler.current_active_callbacks, 0)
        self.assertEqual(metrics.event_pump.current_owned, 0)
        self.assertEqual(metrics.scheduler.accepted, metrics.scheduler.completed)
        self.assertEqual(metrics.event_pump.events_received, metrics.event_pump.events_acknowledged)
        outcome = harness.application.shutdown_outcome
        self.assertIs(outcome.primary_failure, raised.exception)
        self.assertTrue(outcome.complete)
        self.assertFalse(outcome.successful)

    def test_cancellation_precedes_callback_exit_and_deferred_resource_cleanup(self) -> None:
        entered, release, released = Event(), Event(), Event()
        history: list[str] = []
        registry = ActionRegistry[ApplicationContext]()

        @registry.register("test.action")
        class BlockingAction(Action[JsonObject, ApplicationContext]):
            def on_key_down(self, event: object) -> None:
                entered.set()
                release.wait()
                self.assert_stop_requested()
                history.append("callback.exit")

            def assert_stop_requested(self) -> None:
                if not self.dependencies.stop_signal.requested:
                    raise AssertionError("callback exited before cancellation")

            def on_will_disappear(self) -> None:
                history.append("action.release")

        class SessionPlugin(Plugin):
            def on_stop(self) -> None:
                history.append("plugin.stop")

        class Service:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                history.append("service.stop")
                released.set()

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            plugin=SessionPlugin(),
            services=(Service(),),
            runtime_config=RuntimeDispatcherConfig(shutdown_timeout=0.08),
        )
        self.addCleanup(release.set)
        harness.start()
        _appear(harness)
        harness.wait_for_events(1)
        _key_down(harness)
        self.assertTrue(entered.wait(1))
        start = monotonic()
        harness.stop(timeout=1)
        self.assertLess(monotonic() - start, 0.5)
        self.assertTrue(harness.context.stop_signal.wait(0))
        before = harness.application.shutdown_outcome
        self.assertFalse(before.complete)
        self.assertFalse(before.workers_stopped)
        self.assertEqual(before.unfinished_callbacks, 1)
        self.assertTrue(before.pending_cleanup)
        self.assertEqual(history, [])
        with self.assertRaises(FrozenInstanceError):
            before.complete = True
        harness.stop(timeout=1)
        release.set()
        self.assertTrue(released.wait(1))
        self.assertEqual(
            history, ["callback.exit", "action.release", "plugin.stop", "service.stop"]
        )
        self.assertFalse(before.complete)
        _wait_until(lambda: harness.application.shutdown_outcome.complete)
        self.assertTrue(harness.application.shutdown_outcome.complete)

    def test_slow_cleanup_is_bounded_and_services_wait_for_plugin_cleanup(self) -> None:
        entered, release, stopped = Event(), Event(), Event()

        class SlowPlugin(Plugin):
            def on_stop(self) -> None:
                entered.set()
                release.wait()

        class Service:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                stopped.set()

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
            plugin=SlowPlugin(),
            services=(Service(),),
            runtime_config=RuntimeDispatcherConfig(shutdown_timeout=0.08),
        )
        self.addCleanup(release.set)
        harness.start()
        start = monotonic()
        harness.stop(timeout=1)
        self.assertLess(monotonic() - start, 0.5)
        self.assertTrue(entered.is_set())
        self.assertFalse(stopped.is_set())
        self.assertFalse(harness.application.shutdown_outcome.complete)
        release.set()
        self.assertTrue(stopped.wait(1))

    def test_cleanup_failures_are_public_and_repeat_stop_does_not_repeat_cleanup(self) -> None:
        action_error, plugin_error = RuntimeError("action cleanup"), ValueError("plugin cleanup")
        calls: list[str] = []
        registry = ActionRegistry[ApplicationContext]()

        @registry.register("test.action")
        class FailingAction(Action[JsonObject, ApplicationContext]):
            def on_will_disappear(self) -> None:
                calls.append("action")
                raise action_error

        class FailingPlugin(Plugin):
            def on_stop(self) -> None:
                calls.append("plugin")
                raise plugin_error

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            plugin=FailingPlugin(),
        )
        harness.start()
        _appear(harness)
        harness.wait_for_events(1)
        with self.assertLogs("mirabox_sdk", level="ERROR"):
            harness.stop(timeout=1)
        outcome = harness.application.shutdown_outcome
        self.assertTrue(outcome.complete)
        self.assertFalse(outcome.successful)
        self.assertIsNone(outcome.primary_failure)
        self.assertEqual(
            [failure.error for failure in outcome.cleanup_failures], [action_error, plugin_error]
        )
        self.assertIn("button", outcome.cleanup_failures[0].stage)
        self.assertIn("Plugin", outcome.cleanup_failures[1].stage)
        harness.stop(timeout=1)
        self.assertEqual(calls, ["action", "plugin"])
        self.assertEqual(harness.application.shutdown_outcome, outcome)

    def test_slow_service_stop_is_bounded_and_retains_reverse_cleanup_order(self) -> None:
        entered, release, stopped = Event(), Event(), Event()
        history: list[str] = []

        class FirstService:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                history.append("first.stop")
                stopped.set()

        class SecondService:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                entered.set()
                release.wait()
                history.append("second.stop")

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=ActionRegistry[ApplicationContext](),
            action_dependencies_factory=lambda ctx: ctx,
            services=(FirstService(), SecondService()),
            runtime_config=RuntimeDispatcherConfig(shutdown_timeout=0.05),
        )
        self.addCleanup(release.set)
        harness.start()
        start = monotonic()
        harness.stop(timeout=1)
        self.assertLess(monotonic() - start, 0.4)
        self.assertTrue(entered.is_set())
        self.assertFalse(stopped.is_set())
        outcome = harness.application.shutdown_outcome
        self.assertTrue(outcome.workers_stopped)
        self.assertFalse(outcome.complete)
        self.assertIn("Application service SecondService stop", outcome.pending_cleanup)
        release.set()
        self.assertTrue(stopped.wait(1))
        _wait_until(lambda: harness.application.shutdown_outcome.complete)
        self.assertEqual(history, ["second.stop", "first.stop"])

    def test_service_cleanup_failure_preserves_primary_failure_and_records_stage(self) -> None:
        cause, cleanup = SystemExit(3), ValueError("service cleanup")
        entered = Event()
        registry = ActionRegistry[ApplicationContext]()

        @registry.register("test.action")
        class ExitingAction(Action[JsonObject, ApplicationContext]):
            def on_will_appear(self, event: object) -> None:
                entered.set()
                raise cause

        class Service:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                raise cleanup

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            services=(Service(),),
        )
        harness.start()
        _appear(harness)
        self.assertTrue(entered.wait(1))
        with (
            self.assertLogs("mirabox_sdk", level="ERROR"),
            self.assertRaises(RuntimeError) as raised,
        ):
            harness.stop(timeout=1)
        self.assertIs(raised.exception.__cause__, cause)
        outcome = harness.application.shutdown_outcome
        self.assertIs(outcome.primary_failure, raised.exception)
        self.assertEqual(len(outcome.cleanup_failures), 1)
        self.assertIs(outcome.cleanup_failures[0].error, cleanup)
        self.assertIn("Application service", outcome.cleanup_failures[0].stage)
        self.assertTrue(any("Service" in note for note in raised.exception.__notes__))

    def test_callback_drain_timeout_does_not_claim_to_monitor_running_callbacks(self) -> None:
        # This setting is a shutdown wait; it must never interrupt live dispatch.
        entered, release = Event(), Event()
        registry = ActionRegistry[ApplicationContext]()

        @registry.register("test.action")
        class ActionWithWait(Action[JsonObject, ApplicationContext]):
            def on_will_appear(self, event: object) -> None:
                entered.set()
                release.wait()

        harness = StreamDockHarness(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=lambda ctx: ctx,
            runtime_config=RuntimeDispatcherConfig(callback_drain_timeout=0.01),
        )
        self.addCleanup(release.set)
        harness.start()
        _appear(harness)
        self.assertTrue(entered.wait(1))
        sleep(0.03)
        self.assertEqual(harness.application.metrics().scheduler.callback_timeouts, 0)
        self.assertIsNone(harness.application.shutdown_outcome)
        release.set()
        harness.wait_for_events(1)
        harness.stop(timeout=1)
        self.assertTrue(harness.application.shutdown_outcome.successful)

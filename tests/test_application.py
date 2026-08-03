"""Tests for the stable Stream Dock runtime application API."""

from __future__ import annotations

import sys
import unittest
from concurrent.futures import InvalidStateError
from threading import Event, Thread
from threading import enumerate as enumerate_threads

import mirabox_sdk
import mirabox_sdk.runtime as runtime
from mirabox_sdk import (
    ApplicationService,
    CommandFuture,
    OutboundCommandBusClosedError,
    OutboundQueueFullError,
    RuntimeDispatcherConfig,
    RuntimeSchedulerKind,
    StreamDockApplication,
)
from mirabox_sdk._next.messaging.models import CommandFuture as BoundaryCommandFuture
from mirabox_sdk._next.messaging.outbound import (
    OutboundCommandQueueClosedError,
)
from mirabox_sdk._next.messaging.outbound import (
    OutboundQueueFullError as BoundaryQueueFullError,
)


class _RecordingRuntime:
    def __init__(
        self,
        events: list[str],
        *,
        block: bool = False,
        run_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.run_error = run_error
        self.run_started = Event()
        self.release = Event()
        self.block = block
        self.closed = False

    def run_forever(self) -> None:
        self.events.append("runtime-run")
        self.run_started.set()
        if self.block:
            self.release.wait(1)
        if self.run_error is not None:
            raise self.run_error
        self.events.append("runtime-return")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.events.append("runtime-close")
        self.release.set()

    def metrics(self) -> object:
        raise AssertionError("metrics are not used by application lifecycle tests")


class _RecordingService:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        start_gate: Event | None = None,
    ) -> None:
        self.name = name
        self.events = events
        self.start_error = start_error
        self.stop_error = stop_error
        self.start_gate = start_gate
        self.start_entered = Event()

    def start(self) -> None:
        self.events.append(f"start-{self.name}")
        self.start_entered.set()
        if self.start_gate is not None:
            self.start_gate.wait(1)
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> None:
        self.events.append(f"stop-{self.name}")
        if self.stop_error is not None:
            raise self.stop_error


class StableRuntimeApiTests(unittest.TestCase):
    def test_runtime_package_exports_only_stable_application_capabilities(self) -> None:
        expected = {
            "ActionContextMetrics",
            "ActionFactory",
            "ApplicationService",
            "HandlerSchedulerMetrics",
            "InboundOverflowPolicy",
            "PluginHooks",
            "RuntimeDispatcherConfig",
            "RuntimeEventPumpMetrics",
            "RuntimeLifecycle",
            "RuntimeRouterMetrics",
            "RuntimeSchedulerKind",
            "SessionCoordinatorMetrics",
            "StreamDockApplication",
            "StreamDockBoundaryMetrics",
            "StreamDockQueueConfig",
            "StreamDockRuntime",
            "StreamDockRuntimeLifecycleError",
            "StreamDockRuntimeMetrics",
            "StreamDockSender",
            "StreamDockShutdownConfig",
            "create_stream_dock_application",
        }

        self.assertEqual(set(runtime.__all__), expected)
        self.assertTrue(all(hasattr(runtime, name) for name in expected))
        self.assertTrue(expected.issubset(mirabox_sdk.__all__))

    def test_legacy_and_experimental_runtime_surfaces_are_not_public(self) -> None:
        removed = {
            "EVENT_REGISTRY",
            "StreamDockConnection",
            "StreamDockListener",
            "StreamDockPlugin",
            "WebSocketStreamDockConnection",
            "create_experimental_stream_dock_application",
            "create_experimental_stream_dock_connection",
        }

        self.assertFalse(removed.intersection(mirabox_sdk.__all__))
        self.assertTrue(all(not hasattr(mirabox_sdk, name) for name in removed))
        self.assertNotIn("mirabox_sdk.experimental", sys.modules)

    def test_importing_runtime_does_not_start_workers(self) -> None:
        before = {thread.ident for thread in enumerate_threads()}

        __import__("mirabox_sdk.runtime")

        self.assertEqual({thread.ident for thread in enumerate_threads()}, before)

    def test_keyed_serial_scheduler_is_the_bounded_default(self) -> None:
        config = RuntimeDispatcherConfig()

        self.assertIs(config.scheduler_kind, RuntimeSchedulerKind.KEYED_SERIAL)
        self.assertEqual(config.worker_count, 4)
        self.assertEqual(config.scheduler_pending_limit, 64)


class ApplicationServiceLifecycleTests(unittest.TestCase):
    def test_accepts_structural_services_and_rejects_invalid_values(self) -> None:
        events: list[str] = []
        runtime_lifecycle = _RecordingRuntime(events)
        service = _RecordingService("service", events)

        self.assertIsInstance(service, ApplicationService)
        StreamDockApplication(runtime_lifecycle, services=(service,))  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, r"services\[0\]"):
            StreamDockApplication(runtime_lifecycle, services=(object(),))  # type: ignore[arg-type,list-item]
        invalid_methods = type("InvalidService", (), {"start": 1, "stop": 2})()
        with self.assertRaisesRegex(TypeError, r"services\[0\]"):
            StreamDockApplication(
                runtime_lifecycle,  # type: ignore[arg-type]
                services=(invalid_methods,),  # type: ignore[arg-type]
            )

    def test_starts_in_order_and_stops_in_reverse_exactly_once(self) -> None:
        events: list[str] = []
        runtime_lifecycle = _RecordingRuntime(events)
        first = _RecordingService("first", events)
        second = _RecordingService("second", events)
        application = StreamDockApplication(
            runtime_lifecycle,  # type: ignore[arg-type]
            services=(first, second),
        )

        application.run()
        application.stop()
        application.stop()

        self.assertEqual(
            events,
            [
                "start-first",
                "start-second",
                "runtime-run",
                "runtime-return",
                "stop-second",
                "stop-first",
                "runtime-close",
            ],
        )

    def test_startup_failure_stops_only_successfully_started_services(self) -> None:
        events: list[str] = []
        failure = RuntimeError("cannot start")
        runtime_lifecycle = _RecordingRuntime(events)
        application = StreamDockApplication(
            runtime_lifecycle,  # type: ignore[arg-type]
            services=(
                _RecordingService("first", events),
                _RecordingService("failing", events, start_error=failure),
            ),
        )

        with self.assertRaises(RuntimeError) as raised:
            application.run()

        self.assertIs(raised.exception, failure)
        self.assertEqual(events, ["start-first", "start-failing", "stop-first"])

    def test_cleanup_failure_does_not_replace_runtime_failure(self) -> None:
        events: list[str] = []
        primary = RuntimeError("runtime failed")
        cleanup = RuntimeError("cleanup failed")
        application = StreamDockApplication(
            _RecordingRuntime(events, run_error=primary),  # type: ignore[arg-type]
            services=(_RecordingService("service", events, stop_error=cleanup),),
        )

        with (
            self.assertLogs("mirabox_sdk.runtime.application", level="ERROR") as logs,
            self.assertRaises(RuntimeError) as raised,
        ):
            application.run()

        self.assertIs(raised.exception, primary)
        self.assertNotIn("cleanup failed", "\n".join(logs.output))
        self.assertEqual(
            events,
            ["start-service", "runtime-run", "stop-service"],
        )

    def test_cleanup_attempts_every_service_and_raises_first_failure(self) -> None:
        events: list[str] = []
        first_error = RuntimeError("first cleanup failed")
        second_error = RuntimeError("second cleanup failed")
        application = StreamDockApplication(
            _RecordingRuntime(events),  # type: ignore[arg-type]
            services=(
                _RecordingService("first", events, stop_error=first_error),
                _RecordingService("second", events, stop_error=second_error),
            ),
        )

        with (
            self.assertLogs("mirabox_sdk.runtime.application", level="ERROR"),
            self.assertRaises(RuntimeError) as raised,
        ):
            application.run()

        self.assertIs(raised.exception, second_error)
        self.assertEqual(events[-2:], ["stop-second", "stop-first"])

    def test_concurrent_stop_closes_runtime_before_releasing_services(self) -> None:
        events: list[str] = []
        runtime_lifecycle = _RecordingRuntime(events, block=True)
        application = StreamDockApplication(
            runtime_lifecycle,  # type: ignore[arg-type]
            services=(_RecordingService("service", events),),
        )
        errors: list[BaseException] = []

        def run() -> None:
            try:
                application.run()
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=run)
        thread.start()
        self.assertTrue(runtime_lifecycle.run_started.wait(1))

        application.stop()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            events,
            [
                "start-service",
                "runtime-run",
                "runtime-close",
                "runtime-return",
                "stop-service",
            ],
        )

    def test_stop_during_service_startup_skips_runtime_and_cleans_up(self) -> None:
        events: list[str] = []
        start_gate = Event()
        runtime_lifecycle = _RecordingRuntime(events)
        service = _RecordingService("service", events, start_gate=start_gate)
        application = StreamDockApplication(
            runtime_lifecycle,  # type: ignore[arg-type]
            services=(service,),
        )
        errors: list[BaseException] = []

        def run() -> None:
            try:
                application.run()
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=run)
        thread.start()
        self.assertTrue(service.start_entered.wait(1))

        application.stop()
        start_gate.set()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events, ["start-service", "runtime-close", "stop-service"])


class CanonicalCommandFutureTests(unittest.TestCase):
    def test_boundary_and_action_helpers_share_one_completion_type(self) -> None:
        self.assertIs(BoundaryCommandFuture, CommandFuture)

        completion = BoundaryCommandFuture()
        shared = completion._share()
        completion._finish()

        self.assertIsInstance(shared, CommandFuture)
        self.assertTrue(shared.wait(timeout=0))
        self.assertIsNone(shared.result(timeout=0))
        with self.assertRaises(InvalidStateError):
            completion._finish()

    def test_boundary_submission_errors_are_the_public_errors(self) -> None:
        self.assertIs(BoundaryQueueFullError, OutboundQueueFullError)
        self.assertIs(OutboundCommandQueueClosedError, OutboundCommandBusClosedError)


if __name__ == "__main__":
    unittest.main()

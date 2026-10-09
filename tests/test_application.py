"""Tests for the stable Stream Dock runtime application API."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Callable
from concurrent.futures import InvalidStateError
from contextlib import nullcontext
from functools import partial
from importlib import import_module
from threading import Event, Thread
from threading import enumerate as enumerate_threads
from typing import get_args, get_origin, get_type_hints
from unittest.mock import patch

import mirabox_sdk
import mirabox_sdk.runtime as runtime
from mirabox_sdk import (
    ApplicationContext,
    ApplicationRuntime,
    ApplicationService,
    CommandFuture,
    DependencyAwareActionRegistry,
    GlobalSettings,
    LogMessageCommand,
    OutboundCommandBusClosedError,
    OutboundCommandBusNotReadyError,
    OutboundQueueFullError,
    Plugin,
    PluginLaunchArguments,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    RuntimeDispatcherConfig,
    RuntimeLifecycle,
    RuntimeSchedulerKind,
    SessionReadiness,
    StreamDockApplication,
    StreamDockShutdownConfig,
)
from mirabox_sdk import (
    create_stream_dock_application as public_create_stream_dock_application,
)
from mirabox_sdk._internal.messaging.models import CommandFuture as BoundaryCommandFuture
from mirabox_sdk._internal.messaging.outbound import (
    OutboundCommandQueueClosedError,
)
from mirabox_sdk._internal.messaging.outbound import (
    OutboundQueueFullError as BoundaryQueueFullError,
)
from mirabox_sdk.runtime.application import (
    _create_stream_dock_application as create_stream_dock_application,
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
        self._global_settings = _RecordingGlobalSettings()

    @property
    def global_settings(self) -> GlobalSettings:
        return self._global_settings

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

    def update_global_settings(self, _update: object) -> None:
        raise AssertionError("global settings are not used by application lifecycle tests")

    def set_global_settings(self, _settings: object) -> None:
        raise AssertionError("global settings are not used by application lifecycle tests")

    def set_typed_global_settings(self, _settings: object, _codec: object) -> None:
        raise AssertionError("global settings are not used by application lifecycle tests")


class _RecordingGlobalSettings:
    @property
    def loaded(self) -> bool:
        return False

    def snapshot(self) -> dict[str, object]:
        return {}

    def update(self, _update: object) -> None:
        raise AssertionError("global settings are not used by application lifecycle tests")

    def set(self, _settings: object) -> None:
        raise AssertionError("global settings are not used by application lifecycle tests")

    def set_typed(self, _settings: object, _codec: object) -> None:
        raise AssertionError("global settings are not used by application lifecycle tests")


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


class _UnstartedConnector:
    def __init__(self) -> None:
        self.closed = False

    def run_forever(self) -> None:
        raise AssertionError("runtime should not reach the connector")

    def close(self) -> None:
        self.closed = True


class _NoopActionFactory:
    def create(self, _action: str, _context: str, _settings: object) -> None:
        return None


class _SenderCapturingActionRegistry:
    def create(
        self,
        _action: str,
        _context: str,
        _settings: object,
        _dependencies: object,
    ) -> None:
        return None


class _SenderDependencies:
    def __init__(self, stream_dock: object) -> None:
        self.stream_dock = stream_dock


class _ContextDependencies:
    def __init__(
        self,
        stream_dock: object,
        global_settings: GlobalSettings,
        session_readiness: SessionReadiness,
    ) -> None:
        self.stream_dock = stream_dock
        self.global_settings = global_settings
        self.session_readiness = session_readiness


def _launch_arguments() -> PluginLaunchArguments:
    return PluginLaunchArguments(
        port=12345,
        plugin_uuid="plugin-uuid",
        register_event="registerPlugin",
        info=RegistrationInfo(
            application=RegistrationApplicationInfo(
                language="en",
                platform="windows",
                platform_version="11",
                version="2.10",
            ),
            colors=RegistrationColors(),
            device_pixel_ratio=1.0,
            devices=(),
            plugin=RegistrationPluginInfo(uuid="plugin-uuid", version="0.1.0"),
        ),
    )


def _shutdown_config() -> StreamDockShutdownConfig:
    return StreamDockShutdownConfig(
        raw_inbound_drain_timeout=0,
        inbound_event_drain_timeout=0,
        outbound_command_drain_timeout=0,
        raw_outbound_drain_timeout=0,
        session_event_drain_timeout=0,
        worker_stop_timeout=0,
        connector_stop_timeout=0,
    )


class StableRuntimeApiTests(unittest.TestCase):
    def test_runtime_package_exports_only_stable_application_capabilities(self) -> None:
        expected = {
            "ActionContextMetrics",
            "CommandWriterMetrics",
            "EventReaderMetrics",
            "InboundEventQueueMetrics",
            "OutboundCommandQueueMetrics",
            "TransportQueueMetrics",
            "WebSocketConnectorMetrics",
            "ApplicationContext",
            "ApplicationRuntime",
            "ApplicationService",
            "ApplicationServiceFactory",
            "DependencyAwareActionRegistry",
            "GlobalSettings",
            "HandlerSchedulerMetrics",
            "InboundOverflowPolicy",
            "Plugin",
            "PluginHooks",
            "RuntimeDispatcherConfig",
            "RuntimeEventPumpMetrics",
            "RuntimeLifecycle",
            "RuntimeRouterMetrics",
            "RuntimeSchedulerKind",
            "SessionCoordinatorMetrics",
            "SessionReadiness",
            "ShutdownFailure",
            "ShutdownOutcome",
            "StopSignal",
            "StreamDockApplication",
            "StreamDockBoundaryMetrics",
            "StreamDockQueueConfig",
            "StreamDockRuntimeMetrics",
            "StreamDockSender",
            "StreamDockShutdownConfig",
            "create_stream_dock_application",
        }

        self.assertEqual(set(runtime.__all__), expected)
        self.assertTrue(all(hasattr(runtime, name) for name in expected))
        self.assertTrue(expected.issubset(mirabox_sdk.__all__))
        self.assertIs(
            get_type_hints(ApplicationContext)["session_readiness"],
            SessionReadiness,
        )

    def test_application_annotations_use_supported_public_types(self) -> None:
        application_hints = get_type_hints(StreamDockApplication.__init__)
        factory_hints = get_type_hints(public_create_stream_dock_application)

        self.assertIs(application_hints["runtime"], ApplicationRuntime)
        self.assertNotIn("scheduler_factory", factory_hints)
        self.assertNotIn("connector_factory", factory_hints)
        self.assertEqual(factory_hints["plugin"], mirabox_sdk.Plugin | None)
        self.assertEqual(factory_hints["plugin_hooks"], mirabox_sdk.PluginHooks | None)
        self.assertEqual(factory_hints["queue_config"], mirabox_sdk.StreamDockQueueConfig | None)
        self.assertEqual(
            factory_hints["shutdown_config"], mirabox_sdk.StreamDockShutdownConfig | None
        )
        self.assertEqual(factory_hints["runtime_config"], RuntimeDispatcherConfig | None)
        self.assertIs(get_origin(factory_hints["action_factory"]), DependencyAwareActionRegistry)
        for parameter, argument_type in (
            ("action_dependencies_factory", ApplicationContext),
            ("legacy_action_dependencies_factory", mirabox_sdk.StreamDockSender),
            ("plugin_factory", ApplicationContext),
        ):
            with self.subTest(parameter=parameter):
                factory_types = get_args(factory_hints[parameter])
                self.assertEqual(len(factory_types), 2)
                self.assertIs(get_origin(factory_types[0]), Callable)
                self.assertEqual(get_args(factory_types[0])[0], [argument_type])
                self.assertIs(factory_types[1], type(None))

    def test_legacy_and_experimental_runtime_surfaces_are_not_public(self) -> None:
        removed = {
            "EVENT_REGISTRY",
            "ActionFactory",
            "HandlerSchedulerFactory",
            "WebSocketConnectorFactory",
            "StreamDockRuntime",
            "StreamDockRuntimeLifecycleError",
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

    def test_removed_legacy_runtime_modules_cannot_be_imported(self) -> None:
        for module_name in (
            "mirabox_sdk._next",
            "mirabox_sdk.connection",
            "mirabox_sdk.inbound",
            "mirabox_sdk.outbound",
            "mirabox_sdk.plugin",
            "mirabox_sdk.stores",
        ):
            with self.subTest(module=module_name), self.assertRaises(ModuleNotFoundError):
                import_module(module_name)

    def test_importing_runtime_does_not_start_workers(self) -> None:
        before = {thread.ident for thread in enumerate_threads()}

        __import__("mirabox_sdk.runtime")

        self.assertEqual({thread.ident for thread in enumerate_threads()}, before)

    def test_keyed_serial_scheduler_is_the_bounded_default(self) -> None:
        config = RuntimeDispatcherConfig()

        self.assertIs(config.scheduler_kind, RuntimeSchedulerKind.KEYED_SERIAL)
        self.assertEqual(config.worker_count, 4)
        self.assertEqual(config.scheduler_pending_limit, 64)


class ApplicationDependencyFactoryTests(unittest.TestCase):
    def test_context_factory_forms_receive_the_shared_application_context(self) -> None:
        received: list[ApplicationContext] = []
        service_contexts: list[ApplicationContext] = []

        def capture(ctx: ApplicationContext) -> _ContextDependencies:
            received.append(ctx)
            return _ContextDependencies(ctx.stream_dock, ctx.global_settings, ctx.session_readiness)

        def postponed(ctx: ApplicationContext) -> _ContextDependencies:
            return capture(ctx)

        def qualified(ctx: runtime.ApplicationContext) -> _ContextDependencies:
            return capture(ctx)

        # Explicit quotes exercise nested strings with postponed annotations.
        def quoted(ctx: "runtime.ApplicationContext") -> _ContextDependencies:  # noqa: UP037
            return capture(ctx)

        def prefixed(_prefix: str, ctx) -> _ContextDependencies:
            return capture(ctx)

        class ContextFactory:
            def __call__(self, ctx) -> _ContextDependencies:
                return capture(ctx)

        def build_service(ctx: ApplicationContext) -> _RecordingService:
            service_contexts.append(ctx)
            return _RecordingService("factory", [])

        factories = {
            "lambda ctx": lambda ctx: capture(ctx),
            "lambda sender": lambda sender: capture(sender),
            "callable object": ContextFactory(),
            "partial": partial(prefixed, "dependencies"),
            "postponed annotation": postponed,
            "qualified annotation": qualified,
            "quoted qualified annotation": quoted,
        }
        for name, factory in factories.items():
            with self.subTest(factory=name):
                received.clear()
                service_contexts.clear()

                application = create_stream_dock_application(
                    _launch_arguments(),
                    action_factory=_SenderCapturingActionRegistry(),
                    action_dependencies_factory=factory,
                    service_factories=(build_service,),
                    shutdown_config=_shutdown_config(),
                    connector_factory=lambda *_: _UnstartedConnector(),
                )
                self.addCleanup(application.stop)

                self.assertEqual(len(received), 1)
                self.assertIsInstance(received[0], ApplicationContext)
                self.assertIs(received[0], service_contexts[0])
                self.assertIs(received[0].global_settings, application.global_settings)

    def test_legacy_factory_receives_sender_regardless_of_parameter_name(self) -> None:
        received: list[object] = []
        service_contexts: list[ApplicationContext] = []

        def capture(sender: object) -> _SenderDependencies:
            received.append(sender)
            return _SenderDependencies(sender)

        def build_service(ctx: ApplicationContext) -> _RecordingService:
            service_contexts.append(ctx)
            return _RecordingService("factory", [])

        factories = {
            "context": lambda context: capture(context),
            "application_context": lambda application_context: capture(application_context),
            "arbitrary": lambda value: capture(value),
        }
        for name, factory in factories.items():
            with self.subTest(parameter=name):
                received.clear()
                service_contexts.clear()

                with self.assertWarnsRegex(DeprecationWarning, "action_dependencies_factory"):
                    application = create_stream_dock_application(
                        _launch_arguments(),
                        action_factory=_SenderCapturingActionRegistry(),
                        legacy_action_dependencies_factory=factory,
                        service_factories=(build_service,),
                        shutdown_config=_shutdown_config(),
                        connector_factory=lambda *_: _UnstartedConnector(),
                    )
                self.addCleanup(application.stop)

                self.assertEqual(len(received), 1)
                self.assertIs(received[0], service_contexts[0].stream_dock)
                self.assertNotIsInstance(received[0], ApplicationContext)

    def test_rejects_conflicting_factories_before_creating_boundary(self) -> None:
        def unexpected_connector(*_args: object) -> _UnstartedConnector:
            self.fail("invalid factories must be rejected before creating the boundary")

        with self.assertRaisesRegex(TypeError, "mutually exclusive"):
            create_stream_dock_application(
                _launch_arguments(),
                action_factory=_SenderCapturingActionRegistry(),
                action_dependencies_factory=lambda ctx: _SenderDependencies(ctx.stream_dock),
                legacy_action_dependencies_factory=_SenderDependencies,
                connector_factory=unexpected_connector,
            )

    def test_rejects_non_callable_factories(self) -> None:
        for keyword in ("action_dependencies_factory", "legacy_action_dependencies_factory"):
            with self.subTest(parameter=keyword), self.assertRaisesRegex(TypeError, keyword):
                create_stream_dock_application(
                    _launch_arguments(),
                    action_factory=_SenderCapturingActionRegistry(),
                    **{keyword: object()},
                )

    def test_factory_failure_closes_boundary_and_preserves_error(self) -> None:
        failure = TypeError("dependency construction failed")
        received: list[object] = []

        def fail(value: object) -> _SenderDependencies:
            received.append(value)
            raise failure

        for keyword in ("action_dependencies_factory", "legacy_action_dependencies_factory"):
            with self.subTest(parameter=keyword):
                connector = _UnstartedConnector()
                received.clear()

                warning_check = (
                    self.assertWarns(DeprecationWarning)
                    if keyword == "legacy_action_dependencies_factory"
                    else nullcontext()
                )
                with warning_check, self.assertRaises(TypeError) as raised:
                    create_stream_dock_application(
                        _launch_arguments(),
                        action_factory=_SenderCapturingActionRegistry(),
                        connector_factory=lambda *_, result=connector: result,
                        shutdown_config=_shutdown_config(),
                        **{keyword: fail},
                    )

                self.assertIs(raised.exception, failure)
                self.assertEqual(len(received), 1)
                self.assertTrue(connector.closed)


class ApplicationServiceLifecycleTests(unittest.TestCase):
    def test_sender_in_service_start_fails_before_runtime_without_external_stop(self) -> None:
        connector = _UnstartedConnector()
        sender_holder: dict[str, object] = {}
        start_finished = Event()

        class SenderUsingService:
            error: Exception | None = None

            def start(self) -> None:
                try:
                    sender = sender_holder["sender"]
                    sender.send(LogMessageCommand("before runtime"))  # type: ignore[attr-defined]
                except Exception as exc:
                    self.error = exc
                    raise
                finally:
                    start_finished.set()

            def stop(self) -> None:
                return None

        service = SenderUsingService()

        def capture_sender(ctx: ApplicationContext) -> _SenderDependencies:
            sender_holder["sender"] = ctx.stream_dock
            return _SenderDependencies(ctx.stream_dock)

        application = create_stream_dock_application(
            _launch_arguments(),
            action_factory=_SenderCapturingActionRegistry(),
            action_dependencies_factory=capture_sender,
            services=(service,),
            shutdown_config=_shutdown_config(),
            connector_factory=lambda *_: connector,
        )
        errors: list[BaseException] = []

        def run() -> None:
            try:
                application.run()
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=run)
        thread.start()
        completed_before_stop = start_finished.wait(1)
        if not completed_before_stop:
            application.stop()
        thread.join(1)

        self.assertTrue(completed_before_stop)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(service.error, OutboundCommandBusNotReadyError)
        self.assertEqual(errors, [service.error])
        application.stop()
        self.assertTrue(connector.closed)

    def test_global_settings_setter_fails_before_runtime_starts(self) -> None:
        connector = _UnstartedConnector()
        application = create_stream_dock_application(
            _launch_arguments(),
            action_factory=_NoopActionFactory(),
            shutdown_config=_shutdown_config(),
            connector_factory=lambda *_: connector,
        )

        global_settings = application.global_settings
        with self.assertRaises(OutboundCommandBusNotReadyError):
            global_settings.set({"theme": "dark"})

        self.assertEqual(global_settings.snapshot(), {})
        application.stop()
        self.assertTrue(connector.closed)

    def test_global_settings_facade_does_not_expose_mutable_settings(self) -> None:
        application = create_stream_dock_application(
            _launch_arguments(),
            action_factory=_NoopActionFactory(),
            shutdown_config=_shutdown_config(),
        )
        self.addCleanup(application.stop)
        global_settings = application.global_settings
        metrics_before = application.metrics()

        with self.assertRaises(AttributeError):
            global_settings.settings["bypassed"] = True  # type: ignore[attr-defined]
        snapshot = global_settings.snapshot()
        snapshot["bypassed"] = True

        self.assertFalse(global_settings.loaded)
        self.assertEqual(global_settings.snapshot(), {})
        self.assertEqual(application.metrics(), metrics_before)

    def test_context_factories_receive_shared_runtime_dependencies(self) -> None:
        connector = _UnstartedConnector()
        dependencies_holder: dict[str, _ContextDependencies] = {}
        service_contexts: list[ApplicationContext] = []
        service_events: list[str] = []
        plugin_contexts: list[ApplicationContext] = []

        def build_plugin(context: ApplicationContext) -> Plugin:
            plugin_contexts.append(context)
            return Plugin()

        def build_dependencies(context: ApplicationContext) -> _ContextDependencies:
            dependencies = _ContextDependencies(
                context.stream_dock,
                context.global_settings,
                context.session_readiness,
            )
            dependencies_holder["value"] = dependencies
            return dependencies

        def build_service(context: ApplicationContext) -> _RecordingService:
            service_contexts.append(context)
            return _RecordingService("factory", service_events)

        application = create_stream_dock_application(
            _launch_arguments(),
            action_factory=_SenderCapturingActionRegistry(),
            action_dependencies_factory=build_dependencies,
            plugin_factory=build_plugin,
            service_factories=(build_service,),
            shutdown_config=_shutdown_config(),
            connector_factory=lambda *_: connector,
        )

        global_settings = application.global_settings
        snapshot = global_settings.snapshot()
        snapshot["nested"] = {"value": 1}

        self.assertIsInstance(global_settings, GlobalSettings)
        self.assertIs(global_settings, application.runtime.global_settings)
        self.assertIs(global_settings, dependencies_holder["value"].global_settings)
        self.assertIs(global_settings, service_contexts[0].global_settings)
        self.assertIs(plugin_contexts[0], service_contexts[0])
        self.assertIs(
            dependencies_holder["value"].stream_dock,
            service_contexts[0].stream_dock,
        )
        self.assertIsInstance(service_contexts[0].session_readiness, SessionReadiness)
        self.assertIs(
            service_contexts[0].session_readiness,
            dependencies_holder["value"].session_readiness,
        )
        self.assertFalse(service_contexts[0].session_readiness.ready)
        self.assertFalse(service_contexts[0].session_readiness.terminal)
        self.assertEqual(global_settings.snapshot(), {})

        application.stop()
        self.assertTrue(service_contexts[0].session_readiness.terminal)

    def test_accepts_structural_services_and_rejects_invalid_values(self) -> None:
        events: list[str] = []
        runtime_lifecycle = _RecordingRuntime(events)
        service = _RecordingService("service", events)

        self.assertIsInstance(runtime_lifecycle, ApplicationRuntime)
        self.assertIsInstance(runtime_lifecycle, RuntimeLifecycle)
        self.assertIsInstance(service, ApplicationService)
        StreamDockApplication(runtime_lifecycle, services=(service,))
        with self.assertRaisesRegex(TypeError, r"services\[0\]"):
            StreamDockApplication(runtime_lifecycle, services=(object(),))  # type: ignore[arg-type,list-item]
        invalid_methods = type("InvalidService", (), {"start": 1, "stop": 2})()
        with self.assertRaisesRegex(TypeError, r"services\[0\]"):
            StreamDockApplication(
                runtime_lifecycle,  # type: ignore[arg-type]
                services=(invalid_methods,),  # type: ignore[arg-type]
            )

    def test_rejects_runtime_that_only_implements_narrow_lifecycle(self) -> None:
        class LifecycleOnlyRuntime:
            def run_forever(self) -> None:
                return None

            def close(self) -> None:
                return None

            def metrics(self) -> object:
                return object()

        runtime_lifecycle = LifecycleOnlyRuntime()
        self.assertIsInstance(runtime_lifecycle, RuntimeLifecycle)
        self.assertNotIsInstance(runtime_lifecycle, ApplicationRuntime)

        with self.assertRaisesRegex(TypeError, "ApplicationRuntime"):
            StreamDockApplication(runtime_lifecycle)  # type: ignore[arg-type]

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


class ApplicationPluginFactoryTests(unittest.TestCase):
    def test_rejects_invalid_or_conflicting_plugin_factory_before_boundary_creation(self) -> None:
        for kwargs, message in (
            ({"plugin_factory": object()}, "plugin_factory must be callable"),
            (
                {"plugin": Plugin(), "plugin_factory": lambda _ctx: Plugin()},
                "plugin and plugin_factory are mutually exclusive",
            ),
            (
                {"plugin_hooks": Plugin(), "plugin_factory": lambda _ctx: Plugin()},
                "plugin_factory and plugin_hooks are mutually exclusive",
            ),
        ):
            with (
                self.subTest(kwargs=kwargs),
                patch(
                    "mirabox_sdk._internal.boundary.composition.create_stream_dock_boundary"
                ) as build,
                self.assertRaisesRegex(TypeError, message),
            ):
                create_stream_dock_application(
                    _launch_arguments(),
                    action_factory=_NoopActionFactory(),
                    **kwargs,
                )
            build.assert_not_called()

    def test_factory_failure_or_invalid_result_closes_boundary_and_preserves_error(self) -> None:
        failure = RuntimeError("plugin construction failed")

        def failing_factory(_context: ApplicationContext) -> Plugin:
            raise failure

        for factory, error_type, message in (
            (failing_factory, RuntimeError, "plugin construction failed"),
            (lambda _ctx: None, TypeError, "plugin_factory must return a Plugin"),
            (lambda _ctx: object(), TypeError, "plugin_factory must return a Plugin"),
        ):
            connector = _UnstartedConnector()
            with (
                self.subTest(factory=factory),
                self.assertRaisesRegex(error_type, message) as raised,
            ):
                create_stream_dock_application(
                    _launch_arguments(),
                    action_factory=_NoopActionFactory(),
                    plugin_factory=factory,
                    connector_factory=lambda *_, connector=connector: connector,
                    shutdown_config=_shutdown_config(),
                )
            self.assertTrue(connector.closed)
            if factory is failing_factory:
                self.assertIs(raised.exception, failure)


class CanonicalCommandFutureTests(unittest.TestCase):
    def test_result_timeout_leaves_command_pending_for_a_later_result(self) -> None:
        completion = CommandFuture()

        with self.assertRaisesRegex(
            TimeoutError, "^Outbound command did not complete before the timeout$"
        ):
            completion.result(timeout=0)
        self.assertFalse(completion.done())
        self.assertFalse(completion.wait(timeout=0))
        with self.assertRaisesRegex(
            TimeoutError, "^Outbound command did not complete before the timeout$"
        ):
            completion.exception(timeout=0)

        completion._finish()
        self.assertTrue(completion.done())
        self.assertTrue(completion.wait(timeout=0))
        self.assertIsNone(completion.result(timeout=0))
        self.assertIsNone(completion.exception(timeout=0))

    def test_result_preserves_recorded_failures_and_their_causes(self) -> None:
        for failure in (RuntimeError("send failed"), TimeoutError("transport write timed out")):
            cause = OSError("transport failure")
            failure.__cause__ = cause
            completion = CommandFuture()
            shared = completion._share()
            completion._finish(error=failure)

            for handle in (completion, shared):
                for timeout in (None, 0, 1):
                    with self.subTest(failure=type(failure), handle=handle, timeout=timeout):
                        self.assertTrue(handle.done())
                        self.assertTrue(handle.wait(timeout=timeout))
                        self.assertIs(handle.exception(timeout=timeout), failure)
                        with self.assertRaises(type(failure)) as raised:
                            handle.result(timeout=timeout)
                        self.assertIs(raised.exception, failure)
                        self.assertIs(raised.exception.__cause__, cause)

    def test_result_timeout_is_preserved_when_command_finishes_before_it_is_raised(self) -> None:
        def check_race(failure: Exception | None) -> None:
            completion = CommandFuture()
            finish_requested = Event()
            finished = Event()
            wait_for_completion = completion._future.exception

            def finish() -> None:
                if finish_requested.wait(1):
                    completion._finish(error=failure)
                    finished.set()

            def wait_then_finish(timeout: float | None = None) -> None:
                try:
                    wait_for_completion(timeout)
                except TimeoutError:
                    # Finish on another thread after the wait has expired, before
                    # CommandFuture can handle the wait's TimeoutError.
                    finish_requested.set()
                    self.assertTrue(finished.wait(1))
                    raise

            worker = Thread(target=finish)
            worker.start()
            try:
                with (
                    patch.object(completion._future, "result", side_effect=wait_then_finish),
                    patch.object(completion._future, "exception", side_effect=wait_then_finish),
                    self.assertRaisesRegex(
                        TimeoutError, "^Outbound command did not complete before the timeout$"
                    ),
                ):
                    completion.result(timeout=0)
            finally:
                finish_requested.set()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(completion.done())
            self.assertIs(completion.exception(timeout=0), failure)
            if failure is None:
                self.assertIsNone(completion.result(timeout=0))
            else:
                with self.assertRaises(type(failure)) as raised:
                    completion.result(timeout=0)
                self.assertIs(raised.exception, failure)

        for failure in (None, RuntimeError("send failed"), TimeoutError("transport timed out")):
            with self.subTest(failure=failure):
                check_race(failure)

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
        self.assertTrue(
            issubclass(OutboundCommandBusNotReadyError, mirabox_sdk.OutboundCommandBusError)
        )


if __name__ == "__main__":
    unittest.main()

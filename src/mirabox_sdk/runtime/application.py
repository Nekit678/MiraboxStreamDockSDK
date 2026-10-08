"""Stable application composition for the Stream Dock runtime."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable, Iterable
from functools import partial
from threading import Event, Lock, Thread
from typing import TypeVar

from .._internal.boundary.ports import WebSocketConnectorFactory
from .._internal.lifecycle import ShutdownState
from .._internal.runtime.composition import (
    HandlerSchedulerFactory,
    create_stream_dock_runtime,
)
from .._internal.runtime.global_settings import (
    DefaultGlobalSettingsState,
    GlobalSettingsCoordinator,
)
from .._internal.runtime.ports import ActionFactory
from .._internal.runtime.session import SessionReadinessGate
from ..codecs import JsonCodec
from ..diagnostics import SdkDiagnostic
from ..global_settings import GlobalSettings
from ..json_types import JsonObject
from ..json_types import JsonValue as JsonValue  # Resolve recursive JSON type hints.
from ..protocols import StreamDockActionDependencies, StreamDockSender
from ..registration import PluginLaunchArguments
from .config import RuntimeDispatcherConfig, StreamDockQueueConfig, StreamDockShutdownConfig
from .metrics import StreamDockRuntimeMetrics
from .ports import (
    ApplicationContext,
    ApplicationRuntime,
    ApplicationService,
    ApplicationServiceFactory,
    DependencyAwareActionRegistry,
    InboundOverflowPolicy,
    Plugin,
    PluginHooks,
)
from .shutdown import ShutdownOutcome

_DEFAULT_QUEUE_LIMIT = 1024
_DEFAULT_SESSION_QUEUE_LIMIT = 16

GlobalSettingsT = TypeVar("GlobalSettingsT")
ActionDependenciesT = TypeVar("ActionDependenciesT", bound=StreamDockActionDependencies)
logger = logging.getLogger(__name__)


class StreamDockApplication:
    """Executable application facade over managed services and the runtime."""

    __slots__ = (
        "_has_run",
        "_lifecycle_lock",
        "_runtime",
        "_services",
        "_started_services",
        "_stop_requested",
        "_shutdown",
        "_services_stopped",
        "_service_errors",
    )

    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        services: Iterable[ApplicationService] = (),
    ) -> None:
        if not isinstance(runtime, ApplicationRuntime):
            raise TypeError("runtime must implement ApplicationRuntime")
        self._runtime = runtime
        self._services = _resolve_services(services)
        self._started_services: list[ApplicationService] = []
        self._lifecycle_lock = Lock()
        self._has_run = False
        self._stop_requested = False
        self._shutdown = getattr(runtime, "_shutdown", ShutdownState())
        self._services_stopped = Event()
        self._service_errors: tuple[BaseException, ...] = ()

    @property
    def runtime(self) -> ApplicationRuntime:
        """Return the complete runtime facade used by this application."""

        return self._runtime

    @property
    def shutdown_outcome(self) -> ShutdownOutcome | None:
        """Inspect shutdown, including failures and work continuing after the deadline."""
        runtime_outcome = getattr(self._runtime, "shutdown_outcome", None)
        if runtime_outcome is not None:
            return runtime_outcome
        return self._shutdown.snapshot()

    def run(self) -> None:
        """Start services, run once, and release started services in reverse."""

        with self._lifecycle_lock:
            if self._has_run or self._stop_requested:
                raise RuntimeError("Stream Dock application can only be run once before stop")
            self._has_run = True

        self._shutdown.pending("Application services", True)
        primary_error: BaseException | None = None
        runtime_attempted = False
        try:
            for service in self._services:
                with self._lifecycle_lock:
                    if self._stop_requested:
                        break
                service.start()
                with self._lifecycle_lock:
                    self._started_services.append(service)

            with self._lifecycle_lock:
                run_runtime = not self._stop_requested
            if run_runtime:
                runtime_attempted = True
                self._runtime.run_forever()
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._shutdown.begin()
            if primary_error is not None:
                self._shutdown.primary_failure = primary_error
            self._shutdown.pending("Application services", True)
            cleanup_thread = Thread(
                target=self._cleanup_services,
                args=(runtime_attempted,),
                name="mirabox-application-cleanup",
                daemon=True,
            )
            try:
                cleanup_thread.start()
            except Exception as exc:
                self._shutdown.failed("Application cleanup worker start", exc)
                if primary_error is None:
                    raise
                self._services_stopped.set()
            outcome = self.shutdown_outcome
            wait_timeout = (
                0
                if outcome is not None and not outcome.workers_stopped
                else self._shutdown.remaining()
            )
            if not self._services_stopped.wait(wait_timeout):
                self._shutdown.timed_out("Application services")
            elif self._service_errors and primary_error is None:
                raise self._service_errors[0]

    def stop(self) -> None:
        """Idempotently request graceful shutdown.

        Services are released by a cleanup worker after runtime resources are released,
        so this method remains non-blocking when called from an action callback.
        """

        self._shutdown.begin()
        with self._lifecycle_lock:
            self._stop_requested = True
        self._runtime.close()

    def metrics(self) -> StreamDockRuntimeMetrics:
        """Return an immutable aggregate runtime and boundary snapshot."""

        return self._runtime.metrics()

    @property
    def global_settings(self) -> GlobalSettings:
        """Return the canonical runtime-owned plugin-wide settings facade."""

        return self._runtime.global_settings

    def update_global_settings(self, update: Callable[[JsonObject], None]) -> None:
        """Persist and commit one rollback-safe global-settings transaction."""

        self._runtime.update_global_settings(update)

    def set_global_settings(self, settings: JsonObject) -> None:
        """Persist raw plugin-wide settings and update replay state."""

        self._runtime.set_global_settings(settings)

    def set_typed_global_settings(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Persist typed plugin-wide settings and update replay state."""

        self._runtime.set_typed_global_settings(settings, codec)

    def _cleanup_services(self, runtime_attempted: bool) -> None:
        resources_released = getattr(self._runtime, "_resources_released", None)
        if resources_released is not None and runtime_attempted:
            resources_released.wait()
        try:
            self._service_errors = self._stop_started_services()
        finally:
            self._shutdown.pending("Application services", False)
            self._services_stopped.set()

    def _stop_started_services(self) -> tuple[BaseException, ...]:
        with self._lifecycle_lock:
            started_services = tuple(reversed(self._started_services))
            self._started_services.clear()

        errors: list[BaseException] = []
        for service in started_services:
            stage = f"Application service {type(service).__name__} stop"
            self._shutdown.pending(stage, True)
            try:
                service.stop()
            except BaseException as exc:
                errors.append(exc)
                self._shutdown.failed(stage, exc)
                logger.error(
                    "Failed to stop application service; service_type=%s exception_type=%s",
                    type(service).__name__,
                    type(exc).__name__,
                )
            finally:
                self._shutdown.pending(stage, False)
        return tuple(errors)


def create_stream_dock_application(
    launch_arguments: PluginLaunchArguments,
    *,
    action_factory: DependencyAwareActionRegistry[ActionDependenciesT],
    action_dependencies_factory: Callable[[ApplicationContext], ActionDependenciesT] | None = None,
    legacy_action_dependencies_factory: (
        Callable[[StreamDockSender], ActionDependenciesT] | None
    ) = None,
    plugin: Plugin | None = None,
    plugin_factory: Callable[[ApplicationContext], Plugin] | None = None,
    plugin_hooks: PluginHooks | None = None,
    queue_config: StreamDockQueueConfig | None = None,
    shutdown_config: StreamDockShutdownConfig | None = None,
    runtime_config: RuntimeDispatcherConfig | None = None,
    error_observer: Callable[[SdkDiagnostic], None] | None = None,
    services: Iterable[ApplicationService] = (),
    service_factories: Iterable[ApplicationServiceFactory] = (),
    inbound_overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
    coalesce_dial_rotations: bool = False,
    coalesce_commands: bool = False,
) -> StreamDockApplication:
    """Build one unstarted application over the production runtime stack.

    ``ActionRegistry`` users may provide ``action_dependencies_factory``, which
    always receives one :class:`ApplicationContext`, exposing the canonical
    sender and global-settings facade. Sender-only factories must use the
    deprecated ``legacy_action_dependencies_factory`` parameter instead. The
    two dependency-factory parameters are mutually exclusive.
    ``service_factories`` likewise receive that context; objects in
    ``services`` remain supported for already-constructed services. Services
    start before the runtime connects and stop in reverse order after it ends.
    ``plugin_factory`` receives the same context and must return a ``Plugin``;
    an already-constructed ``plugin`` remains supported. These parameters and
    ``plugin_hooks`` are mutually exclusive. Plugin ``on_ready()`` runs after
    mandatory session initialization; ``on_stop()`` releases its resources
    during runtime cleanup, including when ``on_ready()`` raises.
    ``plugin_hooks`` remains the legacy unknown-event-only adapter.
    ``error_observer`` receives structured parser and action/plugin callback
    failures, including the original exception, independently of logging.
    Observers run synchronously on the reporting worker and may run concurrently;
    they must be thread-safe and return promptly. Observer exceptions are logged
    by type and isolated. Protocol errors are discarded; callback failures retain
    the existing routing and rollback behavior. Exception data may be sensitive.
    """

    return _create_stream_dock_application(
        launch_arguments,
        action_factory=action_factory,
        action_dependencies_factory=action_dependencies_factory,
        legacy_action_dependencies_factory=legacy_action_dependencies_factory,
        plugin=plugin,
        plugin_factory=plugin_factory,
        plugin_hooks=plugin_hooks,
        queue_config=queue_config,
        shutdown_config=shutdown_config,
        runtime_config=runtime_config,
        error_observer=error_observer,
        services=services,
        service_factories=service_factories,
        inbound_overflow_policy=inbound_overflow_policy,
        coalesce_dial_rotations=coalesce_dial_rotations,
        coalesce_commands=coalesce_commands,
    )


def _create_stream_dock_application(
    launch_arguments: PluginLaunchArguments,
    *,
    action_factory: ActionFactory | DependencyAwareActionRegistry[ActionDependenciesT],
    action_dependencies_factory: Callable[[ApplicationContext], ActionDependenciesT] | None = None,
    legacy_action_dependencies_factory: (
        Callable[[StreamDockSender], ActionDependenciesT] | None
    ) = None,
    plugin: Plugin | None = None,
    plugin_factory: Callable[[ApplicationContext], Plugin] | None = None,
    plugin_hooks: PluginHooks | None = None,
    queue_config: StreamDockQueueConfig | None = None,
    shutdown_config: StreamDockShutdownConfig | None = None,
    runtime_config: RuntimeDispatcherConfig | None = None,
    error_observer: Callable[[SdkDiagnostic], None] | None = None,
    scheduler_factory: HandlerSchedulerFactory | None = None,
    services: Iterable[ApplicationService] = (),
    service_factories: Iterable[ApplicationServiceFactory] = (),
    inbound_overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
    coalesce_dial_rotations: bool = False,
    coalesce_commands: bool = False,
    connector_factory: WebSocketConnectorFactory | None = None,
    _context_callback: Callable[[ApplicationContext], None] | None = None,
) -> StreamDockApplication:
    """Internal composition seam for SDK tests and the public testing harness."""

    if not isinstance(launch_arguments, PluginLaunchArguments):
        raise TypeError("launch_arguments must be PluginLaunchArguments")
    if error_observer is not None and not callable(error_observer):
        raise TypeError("error_observer must be callable or None")
    if action_dependencies_factory is not None and not callable(action_dependencies_factory):
        raise TypeError("action_dependencies_factory must be callable or None")
    if legacy_action_dependencies_factory is not None and not callable(
        legacy_action_dependencies_factory
    ):
        raise TypeError("legacy_action_dependencies_factory must be callable or None")
    if action_dependencies_factory is not None and legacy_action_dependencies_factory is not None:
        raise TypeError(
            "action_dependencies_factory and legacy_action_dependencies_factory "
            "are mutually exclusive"
        )
    if legacy_action_dependencies_factory is not None:
        warnings.warn(
            "legacy_action_dependencies_factory is deprecated; use "
            "action_dependencies_factory=lambda ctx: factory(ctx.stream_dock) instead",
            DeprecationWarning,
            stacklevel=2,
        )
    if plugin is not None and not isinstance(plugin, Plugin):
        raise TypeError("plugin must extend Plugin or be None")
    if plugin_factory is not None and not callable(plugin_factory):
        raise TypeError("plugin_factory must be callable or None")
    if plugin is not None and plugin_factory is not None:
        raise TypeError("plugin and plugin_factory are mutually exclusive")
    if plugin_factory is not None and plugin_hooks is not None:
        raise TypeError("plugin_factory and plugin_hooks are mutually exclusive")
    if plugin is not None and plugin_hooks is not None:
        raise TypeError("plugin and plugin_hooks are mutually exclusive")
    resolved_services = _resolve_services(services)
    resolved_service_factories = _resolve_service_factories(service_factories)

    from .._internal.boundary.composition import create_stream_dock_boundary
    from .._internal.messaging.reader import EventReader

    resolved_queue_config = queue_config or StreamDockQueueConfig(
        raw_inbound_limit=_DEFAULT_QUEUE_LIMIT,
        inbound_event_limit=_DEFAULT_QUEUE_LIMIT,
        outbound_command_limit=_DEFAULT_QUEUE_LIMIT,
        raw_outbound_limit=_DEFAULT_QUEUE_LIMIT,
        session_event_limit=_DEFAULT_SESSION_QUEUE_LIMIT,
    )
    resolved_runtime_config = runtime_config or RuntimeDispatcherConfig()
    shutdown = ShutdownState(resolved_runtime_config.shutdown_timeout)
    boundary = create_stream_dock_boundary(
        launch_arguments.port,
        resolved_queue_config,
        shutdown_config=shutdown_config,
        shutdown_state=shutdown,
        connector_factory=connector_factory,
        event_reader_factory=partial(EventReader, error_observer=error_observer),
        inbound_overflow_policy=inbound_overflow_policy,
        coalesce_dial_rotations=coalesce_dial_rotations,
        coalesce_commands=coalesce_commands,
    )
    global_settings = GlobalSettingsCoordinator(
        DefaultGlobalSettingsState(launch_arguments.plugin_uuid, boundary.commands)
    )
    session_readiness = SessionReadinessGate()
    context = ApplicationContext(
        stream_dock=boundary.commands,
        global_settings=global_settings,
        session_readiness=session_readiness,
        stop_signal=shutdown.signal,
    )
    try:
        if _context_callback is not None:
            _context_callback(context)
        if plugin_factory is not None:
            plugin = plugin_factory(context)
            if not isinstance(plugin, Plugin):
                raise TypeError("plugin_factory must return a Plugin")
        action_dependencies: ActionDependenciesT | None = None
        if action_dependencies_factory is not None:
            action_dependencies = action_dependencies_factory(context)
        elif legacy_action_dependencies_factory is not None:
            action_dependencies = legacy_action_dependencies_factory(context.stream_dock)
        factory_services = tuple(factory(context) for factory in resolved_service_factories)
        all_services = _resolve_services((*resolved_services, *factory_services))
        runtime = create_stream_dock_runtime(
            launch_arguments,
            boundary=boundary,
            action_factory=action_factory,
            action_dependencies=action_dependencies,
            global_settings=global_settings,
            session_readiness=session_readiness,
            plugin=plugin,
            plugin_hooks=plugin_hooks,
            config=resolved_runtime_config,
            scheduler_factory=scheduler_factory,
            error_observer=error_observer,
        )
    except BaseException:
        boundary.close()
        raise
    return StreamDockApplication(runtime, services=all_services)


def _resolve_services(
    services: Iterable[ApplicationService],
) -> tuple[ApplicationService, ...]:
    try:
        resolved = tuple(services)
    except TypeError as exc:
        raise TypeError("services must be an iterable of ApplicationService objects") from exc
    for index, service in enumerate(resolved):
        if (
            not isinstance(service, ApplicationService)
            or not callable(getattr(service, "start", None))
            or not callable(getattr(service, "stop", None))
        ):
            raise TypeError(f"services[{index}] must implement ApplicationService")
    return resolved


def _resolve_service_factories(
    service_factories: Iterable[ApplicationServiceFactory],
) -> tuple[ApplicationServiceFactory, ...]:
    try:
        resolved = tuple(service_factories)
    except TypeError as exc:
        raise TypeError("service_factories must be an iterable of callables") from exc
    for index, factory in enumerate(resolved):
        if not callable(factory):
            raise TypeError(f"service_factories[{index}] must be callable")
    return resolved


__all__ = [
    "StreamDockApplication",
    "create_stream_dock_application",
]

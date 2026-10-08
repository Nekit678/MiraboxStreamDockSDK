"""Stable application composition for the Stream Dock runtime."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable, Iterable
from threading import Lock
from typing import TypeVar

from .._internal.runtime.composition import (
    create_stream_dock_runtime,
)
from .._internal.runtime.global_settings import (
    DefaultGlobalSettingsState,
    GlobalSettingsCoordinator,
)
from .._internal.runtime.session import SessionReadinessGate
from ..codecs import JsonCodec
from ..global_settings import GlobalSettings
from ..json_types import JsonObject
from ..protocols import StreamDockActionDependencies, StreamDockSender
from ..registration import PluginLaunchArguments
from .config import RuntimeDispatcherConfig, StreamDockQueueConfig, StreamDockShutdownConfig
from .metrics import StreamDockRuntimeMetrics
from .ports import (
    ActionFactory,
    ApplicationContext,
    ApplicationRuntime,
    ApplicationService,
    ApplicationServiceFactory,
    DependencyAwareActionRegistry,
    HandlerSchedulerFactory,
    InboundOverflowPolicy,
    Plugin,
    PluginHooks,
    WebSocketConnectorFactory,
)

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

    @property
    def runtime(self) -> ApplicationRuntime:
        """Return the complete runtime facade used by this application."""

        return self._runtime

    def run(self) -> None:
        """Start services, run once, and release started services in reverse."""

        with self._lifecycle_lock:
            if self._has_run or self._stop_requested:
                raise RuntimeError("Stream Dock application can only be run once before stop")
            self._has_run = True

        primary_error: BaseException | None = None
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
                self._runtime.run_forever()
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_errors = self._stop_started_services()
            if cleanup_errors and primary_error is None:
                raise cleanup_errors[0]

    def stop(self) -> None:
        """Idempotently request graceful shutdown.

        Services are released by the lifecycle thread after the runtime stops,
        so this method remains non-blocking when called from an action callback.
        """

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

    def _stop_started_services(self) -> tuple[Exception, ...]:
        with self._lifecycle_lock:
            started_services = tuple(reversed(self._started_services))
            self._started_services.clear()

        errors: list[Exception] = []
        for service in started_services:
            try:
                service.stop()
            except Exception as exc:
                errors.append(exc)
                logger.error(
                    "Failed to stop application service; service_type=%s exception_type=%s",
                    type(service).__name__,
                    type(exc).__name__,
                )
        return tuple(errors)


def create_stream_dock_application(
    launch_arguments: PluginLaunchArguments,
    *,
    action_factory: ActionFactory | DependencyAwareActionRegistry[ActionDependenciesT],
    action_dependencies_factory: Callable[[ApplicationContext], ActionDependenciesT] | None = None,
    legacy_action_dependencies_factory: (
        Callable[[StreamDockSender], ActionDependenciesT] | None
    ) = None,
    plugin: Plugin | None = None,
    plugin_hooks: PluginHooks | None = None,
    queue_config: StreamDockQueueConfig | None = None,
    shutdown_config: StreamDockShutdownConfig | None = None,
    runtime_config: RuntimeDispatcherConfig | None = None,
    scheduler_factory: HandlerSchedulerFactory | None = None,
    services: Iterable[ApplicationService] = (),
    service_factories: Iterable[ApplicationServiceFactory] = (),
    inbound_overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
    coalesce_dial_rotations: bool = False,
    coalesce_commands: bool = False,
    connector_factory: WebSocketConnectorFactory | None = None,
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
    ``plugin`` receives known plugin-wide broadcast callbacks; ``plugin_hooks``
    remains the legacy unknown-event-only adapter. Native three-argument
    :class:`ActionFactory` implementations leave the dependency factory unset.
    """

    if not isinstance(launch_arguments, PluginLaunchArguments):
        raise TypeError("launch_arguments must be PluginLaunchArguments")
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
    if plugin is not None and plugin_hooks is not None:
        raise TypeError("plugin and plugin_hooks are mutually exclusive")
    resolved_services = _resolve_services(services)
    resolved_service_factories = _resolve_service_factories(service_factories)

    from .._internal.boundary.composition import create_stream_dock_boundary

    resolved_queue_config = queue_config or StreamDockQueueConfig(
        raw_inbound_limit=_DEFAULT_QUEUE_LIMIT,
        inbound_event_limit=_DEFAULT_QUEUE_LIMIT,
        outbound_command_limit=_DEFAULT_QUEUE_LIMIT,
        raw_outbound_limit=_DEFAULT_QUEUE_LIMIT,
        session_event_limit=_DEFAULT_SESSION_QUEUE_LIMIT,
    )
    boundary = create_stream_dock_boundary(
        launch_arguments.port,
        resolved_queue_config,
        shutdown_config=shutdown_config,
        connector_factory=connector_factory,
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
    )
    try:
        action_dependencies: StreamDockActionDependencies | None = None
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
            config=runtime_config,
            scheduler_factory=scheduler_factory,
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

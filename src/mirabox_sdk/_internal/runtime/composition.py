"""Composition root and single lifecycle owner for the runtime dispatcher."""

from __future__ import annotations

import logging
from collections.abc import Callable
from inspect import Signature, signature
from threading import Condition, Event, Lock, Thread, current_thread
from typing import Protocol, TypeVar, cast, runtime_checkable

from ...codecs import JsonCodec
from ...global_settings import GlobalSettings
from ...json_types import JsonObject
from ...protocols import StreamDockActionDependencies
from ...registration import PluginLaunchArguments
from ..boundary.ports import StreamDockBoundary
from ..lifecycle import RuntimeWorkerError, ShutdownOutcome, ShutdownState
from .adapters import ActionRegistryFactoryAdapter, DependencyAwareActionRegistry
from .config import RuntimeDispatcherConfig
from .global_settings import DefaultGlobalSettingsState, GlobalSettingsCoordinator
from .keyed_scheduler import KeyedSerialHandlerScheduler
from .metrics import ActionContextMetrics, RuntimeRouterMetrics, StreamDockRuntimeMetrics
from .models import RuntimeLifecycleState, RuntimeSchedulerKind, transition_runtime_state
from .plugin import Plugin, PluginSessionLifecycle
from .ports import (
    ActionContextManager,
    ActionFactory,
    HandlerScheduler,
    PluginHooks,
    RuntimeEventDispatcher,
    RuntimeEventPumpWorker,
    RuntimeLifecycle,
    SessionEventPumpWorker,
)
from .pumps import RuntimeEventPump, SessionEventPump
from .router import RuntimeEventRouter
from .scheduler import SequentialHandlerScheduler
from .session import SessionCoordinator, SessionReadinessGate

logger = logging.getLogger(__name__)

GlobalSettingsT = TypeVar("GlobalSettingsT")
DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)


class StreamDockRuntimeLifecycleError(RuntimeError):
    """Report invalid use or incomplete shutdown of one runtime instance."""


@runtime_checkable
class HandlerSchedulerFactory(Protocol):
    """Create an unstarted scheduler for one runtime event dispatcher."""

    def __call__(self, dispatcher: RuntimeEventDispatcher) -> HandlerScheduler: ...


@runtime_checkable
class _RuntimeRouterState(RuntimeEventDispatcher, Protocol):
    @property
    def contexts(self) -> ActionContextManager: ...

    def routing_metrics(self) -> RuntimeRouterMetrics: ...

    def action_metrics(self) -> ActionContextMetrics: ...


class _FatalErrorRelay:
    """Break the construction cycle between pumps and their supervisor."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._target: Callable[[Exception], None] | None = None
        self._pending: Exception | None = None

    def __call__(self, error: Exception) -> None:
        with self._lock:
            target = self._target
            if target is None:
                if self._pending is None:
                    self._pending = error
                return
        target(error)

    def bind(self, target: Callable[[Exception], None]) -> None:
        with self._lock:
            if self._target is not None:
                raise RuntimeError("fatal-error relay is already bound")
            self._target = target
            pending = self._pending
            self._pending = None
        if pending is not None:
            target(pending)


class ComposedStreamDockRuntime(RuntimeLifecycle):
    """Own runtime workers and expose only application lifecycle capabilities."""

    def __init__(
        self,
        *,
        boundary: StreamDockBoundary,
        scheduler: HandlerScheduler,
        event_pump: RuntimeEventPumpWorker,
        session_pump: SessionEventPumpWorker,
        router: _RuntimeRouterState,
        config: RuntimeDispatcherConfig | None = None,
        session_readiness: SessionReadinessGate | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(boundary, StreamDockBoundary):
            raise TypeError("boundary must implement StreamDockBoundary")
        if not isinstance(scheduler, HandlerScheduler):
            raise TypeError("scheduler must implement HandlerScheduler")
        if not isinstance(event_pump, RuntimeEventPumpWorker):
            raise TypeError("event_pump must implement RuntimeEventPumpWorker")
        if not isinstance(session_pump, SessionEventPumpWorker):
            raise TypeError("session_pump must implement SessionEventPumpWorker")
        if not isinstance(router, _RuntimeRouterState):
            raise TypeError("router must expose runtime dispatch state")
        resolved_config = config or RuntimeDispatcherConfig()
        if not isinstance(resolved_config, RuntimeDispatcherConfig):
            raise TypeError("config must be RuntimeDispatcherConfig or None")
        if session_readiness is not None and not isinstance(
            session_readiness, SessionReadinessGate
        ):
            raise TypeError("session_readiness must be a SessionReadinessGate or None")
        if on_stop is not None and not callable(on_stop):
            raise TypeError("on_stop must be callable or None")

        shared_shutdown = getattr(boundary, "_shutdown", None)
        self._shutdown = (
            shared_shutdown
            if isinstance(shared_shutdown, ShutdownState)
            else ShutdownState(resolved_config.shutdown_timeout)
        )
        if config is not None:
            self._shutdown._timeout = resolved_config.shutdown_timeout
        self._resources_released = Event()
        self._cleanup_thread: Thread | None = None
        self._cleanup_complete = Event()
        self._cleanup_wait_complete = Event()
        self._boundary_run_complete = Event()
        self._boundary_run_complete.set()
        self._boundary = boundary
        self._scheduler = scheduler
        self._event_pump = event_pump
        self._session_pump = session_pump
        self._router = router
        self._config = resolved_config
        self._session_readiness = session_readiness
        self._on_stop = on_stop

        self._condition = Condition()
        self._state = RuntimeLifecycleState.NEW
        self._lifecycle_thread: Thread | None = None
        self._shutdown_requested = False
        self._primary_failure: Exception | None = None
        self._failed_worker_stops: set[str] = set()
        self._terminal = Event()

        self._boundary_close_lock = Lock()
        self._boundary_close_started = False
        self._boundary_close_complete = Event()
        self._boundary_close_thread: Thread | None = None

    @property
    def state(self) -> RuntimeLifecycleState:
        """Return the current single-run lifecycle state."""

        with self._condition:
            return self._state

    @property
    def failure(self) -> Exception | None:
        """Return the first fatal runtime failure, if one was observed."""

        with self._condition:
            return self._primary_failure

    @property
    def shutdown_outcome(self) -> ShutdownOutcome | None:
        scheduler = self._scheduler.metrics()
        boundary = self._boundary.metrics()
        return self._shutdown.snapshot(
            unfinished_callbacks=scheduler.current_active_callbacks,
            callback_timeouts=scheduler.callback_timeouts,
            discarded_events=scheduler.discarded_during_shutdown
            + getattr(getattr(boundary, "inbound_events", None), "discarded_during_shutdown", 0),
            discarded_commands=getattr(
                getattr(boundary, "command_writer", None), "discarded_during_shutdown", 0
            )
            + getattr(getattr(boundary, "outbound_commands", None), "discarded_during_shutdown", 0),
        )

    def run_forever(self) -> None:
        """Start consumers before the boundary and supervise one complete run."""

        with self._condition:
            if self._state is not RuntimeLifecycleState.NEW or self._shutdown_requested:
                raise StreamDockRuntimeLifecycleError("runtime can only be run once")
            self._state = transition_runtime_state(
                self._state,
                RuntimeLifecycleState.STARTING,
            )
            self._lifecycle_thread = current_thread()

        scheduler_attempted = False
        event_pump_started = False
        session_pump_started = False
        try:
            scheduler_attempted = True
            self._shutdown.worker("Runtime scheduler stop", False)
            self._scheduler.start()
            if not self._shutdown_is_requested():
                self._event_pump.start()
                self._shutdown.worker("Runtime event pump stop", False)
                event_pump_started = True
            if not self._shutdown_is_requested():
                self._session_pump.start()
                self._shutdown.worker("Runtime session pump stop", False)
                session_pump_started = True
                with self._condition:
                    if not self._shutdown_requested:
                        self._state = transition_runtime_state(
                            self._state,
                            RuntimeLifecycleState.RUNNING,
                        )
            if not self._shutdown_is_requested():
                self._boundary_run_complete.clear()
                self._shutdown.worker("Boundary run", False)
                boundary_thread = Thread(
                    target=self._run_boundary,
                    name="mirabox-internal-boundary-run",
                    daemon=True,
                )
                try:
                    boundary_thread.start()
                except BaseException:
                    self._boundary_run_complete.set()
                    self._shutdown.worker("Boundary run", True)
                    raise
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._boundary_run_complete.is_set() or self._shutdown_requested
                    )
                if not self._boundary_run_complete.wait(self._shutdown.remaining()):
                    self._shutdown.timed_out("Boundary run")
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            if not self._shutdown_is_requested() or self.failure is not None:
                self._record_primary_failure(
                    exc
                    if isinstance(exc, Exception)
                    else RuntimeWorkerError("Runtime startup", exc)
                )
        finally:
            with self._condition:
                if self._state in (
                    RuntimeLifecycleState.STARTING,
                    RuntimeLifecycleState.RUNNING,
                ):
                    self._state = transition_runtime_state(
                        self._state,
                        RuntimeLifecycleState.STOPPING,
                    )
                self._shutdown_requested = True

            self._shutdown.begin()
            self._shutdown.pending("Runtime cleanup", True)
            cleanup_thread = Thread(
                target=self._cleanup_owned,
                kwargs=dict(
                    scheduler_attempted=scheduler_attempted,
                    event_pump_started=event_pump_started,
                    session_pump_started=session_pump_started,
                ),
                name="mirabox-internal-runtime-cleanup",
                daemon=True,
            )
            self._cleanup_thread = cleanup_thread
            try:
                cleanup_thread.start()
            except BaseException as exc:
                self._record_cleanup_failure("Runtime cleanup worker start", exc)
            if not self._cleanup_wait_complete.wait(self._shutdown.remaining()):
                self._shutdown.timed_out("Runtime cleanup")

            with self._condition:
                failure = self._primary_failure
                shared_failure = self._shutdown.primary_failure
                if failure is None and isinstance(shared_failure, Exception):
                    failure = shared_failure
                    self._primary_failure = failure
                target = (
                    RuntimeLifecycleState.FAILED
                    if failure is not None
                    else RuntimeLifecycleState.STOPPED
                )
                self._state = transition_runtime_state(self._state, target)
                self._lifecycle_thread = None
                self._condition.notify_all()
            self._terminal.set()

        if failure is not None:
            raise failure

    def _run_boundary(self) -> None:
        try:
            self._boundary.run_forever()
        except BaseException as exc:
            if not self._shutdown_is_requested() or self.failure is not None:
                error = (
                    exc if isinstance(exc, Exception) else RuntimeWorkerError("Boundary run", exc)
                )
                self._on_fatal_error(error)
        finally:
            self._shutdown.begin()
            self._shutdown.worker("Boundary run", True)
            self._boundary_run_complete.set()
            with self._condition:
                self._condition.notify_all()

    def close(self) -> None:
        """Idempotently close the boundary and wait outside runtime callbacks."""

        self._shutdown.begin()
        with self._condition:
            if self._state.terminal:
                return
            self._shutdown_requested = True
            self._condition.notify_all()
            close_before_run = self._state is RuntimeLifecycleState.NEW
            called_from_lifecycle = self._lifecycle_thread is current_thread()

        scheduler_thread_probe = getattr(self._scheduler, "is_dispatch_thread", None)
        called_from_scheduler = bool(
            scheduler_thread_probe() if callable(scheduler_thread_probe) else False
        )
        called_from_worker = (
            called_from_lifecycle
            or self._cleanup_thread is current_thread()
            or called_from_scheduler
            or self._event_pump.is_worker_thread()
            or self._session_pump.is_worker_thread()
        )
        self._ensure_boundary_closed(nonblocking=called_from_worker)
        if called_from_worker:
            return

        if close_before_run:
            self._boundary_close_complete.wait(self._shutdown.remaining())
            self._close_session_readiness()
            with self._condition:
                if self._state is RuntimeLifecycleState.NEW:
                    self._state = transition_runtime_state(
                        self._state,
                        RuntimeLifecycleState.STOPPED,
                    )
                    self._condition.notify_all()
            self._resources_released.set()
            self._terminal.set()
            return

        if not self._terminal.wait(self._shutdown.remaining()):
            self._shutdown.timed_out("Runtime close")

    def metrics(self) -> StreamDockRuntimeMetrics:
        """Aggregate immutable snapshots without exposing typed sources."""

        return StreamDockRuntimeMetrics(
            session=self._session_pump.metrics(),
            event_pump=self._event_pump.metrics(),
            scheduler=self._scheduler.metrics(),
            routing=self._router.routing_metrics(),
            actions=self._router.action_metrics(),
            boundary=self._boundary.metrics(),
        )

    @property
    def global_settings(self) -> GlobalSettings:
        """Return the canonical public plugin-wide settings facade."""

        router = cast(RuntimeEventRouter, self._router)
        return router.global_settings

    def update_global_settings(self, update: Callable[[JsonObject], None]) -> None:
        """Persist one rollback-safe transaction through :attr:`global_settings`."""

        self.global_settings.update(update)

    def set_global_settings(self, settings: JsonObject) -> None:
        """Persist raw settings through :attr:`global_settings`."""

        self.global_settings.set(settings)

    def set_typed_global_settings(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Encode and persist typed settings through :attr:`global_settings`."""

        self.global_settings.set_typed(settings, codec)

    def _shutdown_is_requested(self) -> bool:
        with self._condition:
            return self._shutdown_requested

    def _record_primary_failure(self, error: Exception) -> None:
        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception")
        with self._condition:
            if self._primary_failure is None:
                self._primary_failure = error
                self._shutdown.primary_failure = error
            self._shutdown_requested = True
            self._condition.notify_all()

    def _on_fatal_error(self, error: Exception) -> None:
        self._shutdown.begin()
        first_failure = self._shutdown.primary_failure
        self._record_primary_failure(
            first_failure if isinstance(first_failure, Exception) else error
        )
        self._ensure_boundary_closed(nonblocking=True)

    def _ensure_boundary_closed(self, *, nonblocking: bool) -> None:
        self._shutdown.begin()
        with self._boundary_close_lock:
            if not self._boundary_close_started:
                self._boundary_close_started = True
                self._shutdown.pending("Boundary close", True)
                thread = Thread(
                    target=self._close_boundary_owned,
                    name="mirabox-internal-runtime-close",
                    daemon=True,
                )
                self._boundary_close_thread = thread
                try:
                    thread.start()
                except Exception as exc:
                    self._record_cleanup_failure("Boundary close worker start", exc)
                    self._boundary_close_complete.set()
        if not nonblocking:
            if not self._boundary_close_complete.wait(self._shutdown.remaining()):
                self._shutdown.timed_out("Boundary close")

    def _close_boundary_owned(self) -> None:
        try:
            self._boundary.close()
        except BaseException as exc:
            self._record_cleanup_failure("Stream Dock boundary close", exc)
        finally:
            self._shutdown.pending("Boundary close", False)
            self._boundary_close_complete.set()

    def _cleanup_owned(self, **kwargs: bool) -> None:
        try:
            self._cleanup(**kwargs)
        except BaseException as exc:
            self._record_cleanup_failure("Runtime cleanup", exc)
        finally:
            self._shutdown.pending("Runtime cleanup", False)
            self._cleanup_complete.set()
            self._cleanup_wait_complete.set()

    def _cleanup(
        self,
        *,
        scheduler_attempted: bool,
        event_pump_started: bool,
        session_pump_started: bool,
    ) -> None:
        self._ensure_boundary_closed(nonblocking=False)
        if not session_pump_started:
            self._close_session_readiness()

        event_drained = True
        if event_pump_started:
            event_drained = self._safe_bool_cleanup(
                "Runtime event pump drain",
                self._event_pump.drain,
                timeout=self._shutdown.remaining(self._config.runtime_drain_timeout),
            )

        if scheduler_attempted:
            self._safe_cleanup("Runtime scheduler stop_accepting", self._scheduler.stop_accepting)

        if event_pump_started and not event_drained:
            logger.warning("Runtime event pump did not drain before shutdown timeout")
            self._safe_cleanup("Runtime event pump request_stop", self._event_pump.request_stop)

        session_stopped = True
        if session_pump_started:
            if not self._safe_bool_cleanup(
                "Runtime session pump drain",
                self._session_pump.drain,
                timeout=self._shutdown.remaining(self._config.runtime_drain_timeout),
            ):
                logger.warning("Runtime session pump did not drain before shutdown timeout")
            session_stopped = self._safe_bool_cleanup(
                "Runtime session pump stop",
                self._session_pump.stop,
                timeout=self._shutdown.remaining(self._config.worker_stop_timeout),
            )
            if session_stopped:
                # Stopping can skip source closure; wake readiness after initialization exits.
                self._close_session_readiness()

        scheduler_stopped = True
        if scheduler_attempted:
            scheduler_timeout = (
                self._config.callback_drain_timeout
                if self._config.callback_drain_timeout is not None
                else self._config.callback_timeout
                if self._config.callback_timeout is not None
                else self._config.runtime_drain_timeout
            )
            if not self._safe_bool_cleanup(
                "Runtime scheduler drain",
                self._scheduler.drain,
                timeout=self._shutdown.remaining(scheduler_timeout),
            ):
                logger.warning("Runtime scheduler did not drain before shutdown timeout")
            scheduler_stopped = self._safe_bool_cleanup(
                "Runtime scheduler stop",
                self._scheduler.stop,
                timeout=self._shutdown.remaining(self._config.worker_stop_timeout),
            )

        event_stopped = True
        if event_pump_started:
            event_stopped = self._safe_bool_cleanup(
                "Runtime event pump stop",
                self._event_pump.stop,
                timeout=self._shutdown.remaining(self._config.worker_stop_timeout),
            )

        # A timeout limits the caller's wait, never the lifetime of shared resources.
        # This daemon owns deferred cleanup until every resource user has exited.
        if not (scheduler_stopped and event_stopped and session_stopped):
            self._cleanup_wait_complete.set()
        self._boundary_close_complete.wait()
        boundary_complete = getattr(self._boundary, "_workers_stopped", None)
        if boundary_complete is not None:
            boundary_complete.wait()
        self._boundary_run_complete.wait()
        if self._failed_worker_stops:
            return
        if scheduler_attempted and not scheduler_stopped:
            if not self._safe_bool_cleanup(
                "Runtime scheduler stop", self._scheduler.stop, timeout=None
            ):
                return
        if session_pump_started and not session_stopped:
            if not self._safe_bool_cleanup(
                "Runtime session pump stop", self._session_pump.stop, timeout=None
            ):
                return
            self._close_session_readiness()
        if event_pump_started and not event_stopped:
            if not self._safe_bool_cleanup(
                "Runtime event pump stop", self._event_pump.stop, timeout=None
            ):
                return

        self._release_actions()
        if self._on_stop is not None:
            self._safe_cleanup("Plugin session stop", self._on_stop)
        self._resources_released.set()

    def _close_session_readiness(self) -> None:
        if self._session_readiness is not None:
            self._session_readiness.close()

    def _release_actions(self) -> None:
        try:
            actions = self._router.contexts.clear()
        except BaseException as exc:
            self._record_cleanup_failure("Runtime action clear", exc)
            return
        for action in actions:
            self._safe_cleanup(
                f"Runtime action release for context {action.context}", action.on_will_disappear
            )

    def _safe_cleanup(self, name: str, action: Callable[..., object], **kwargs: object) -> None:
        self._shutdown.pending(name, True)
        try:
            action(**kwargs)
        except BaseException as exc:
            self._record_cleanup_failure(name, exc)
        finally:
            self._shutdown.pending(name, False)

    def _safe_bool_cleanup(
        self,
        name: str,
        action: Callable[..., bool],
        **kwargs: object,
    ) -> bool:
        try:
            result = action(**kwargs)
            if name.endswith(" stop"):
                self._shutdown.worker(name, result)
            if not result:
                self._shutdown.timed_out(name)
            return result
        except BaseException as exc:
            self._record_cleanup_failure(name, exc)
            if name.endswith(" stop"):
                self._shutdown.worker(name, False)
                self._failed_worker_stops.add(name)
            return False

    def _record_cleanup_failure(self, stage: str, error: BaseException) -> None:
        self._shutdown.failed(stage, error)
        logger.error(
            "%s failed; exception_type=%s",
            stage,
            type(error).__name__,
        )


def create_stream_dock_runtime(
    launch_arguments: PluginLaunchArguments,
    *,
    boundary: StreamDockBoundary,
    action_factory: ActionFactory | DependencyAwareActionRegistry[DependenciesT],
    action_dependencies: DependenciesT | None = None,
    global_settings: GlobalSettingsCoordinator | None = None,
    session_readiness: SessionReadinessGate | None = None,
    plugin: Plugin | None = None,
    plugin_hooks: PluginHooks | None = None,
    config: RuntimeDispatcherConfig | None = None,
    scheduler_factory: HandlerSchedulerFactory | None = None,
) -> ComposedStreamDockRuntime:
    """Build one unstarted runtime over typed boundary ports."""

    if not isinstance(launch_arguments, PluginLaunchArguments):
        raise TypeError("launch_arguments must be PluginLaunchArguments")
    if not isinstance(boundary, StreamDockBoundary):
        raise TypeError("boundary must implement StreamDockBoundary")
    resolved_config = config or RuntimeDispatcherConfig()
    if not isinstance(resolved_config, RuntimeDispatcherConfig):
        raise TypeError("config must be RuntimeDispatcherConfig or None")
    if plugin_hooks is not None and not isinstance(plugin_hooks, PluginHooks):
        raise TypeError("plugin_hooks must implement PluginHooks or be None")
    if plugin is not None and not isinstance(plugin, Plugin):
        raise TypeError("plugin must extend Plugin or be None")
    if plugin is not None and plugin_hooks is not None:
        raise TypeError("plugin and plugin_hooks are mutually exclusive")

    create_action = getattr(action_factory, "create", None)
    if not callable(create_action):
        raise TypeError("action_factory must provide create")
    if action_dependencies is not None:
        if not _accepts_positional_arguments(create_action, 4):
            raise TypeError(
                "action_dependencies can only be bound to a four-argument action registry"
            )
        resolved_action_factory: ActionFactory = ActionRegistryFactoryAdapter(
            cast(DependencyAwareActionRegistry[DependenciesT], action_factory),
            action_dependencies,
        )
    elif _accepts_positional_arguments(create_action, 3):
        resolved_action_factory = cast(ActionFactory, action_factory)
    elif _accepts_positional_arguments(create_action, 4):
        raise TypeError("action_dependencies are required for the action registry")
    else:
        raise TypeError(
            "action_factory must implement ActionFactory or have bound action_dependencies"
        )

    if global_settings is None:
        resolved_global_settings = GlobalSettingsCoordinator(
            DefaultGlobalSettingsState(
                launch_arguments.plugin_uuid,
                boundary.commands,
            )
        )
    elif not isinstance(global_settings, GlobalSettingsCoordinator):
        raise TypeError("global_settings must be a GlobalSettingsCoordinator or None")
    else:
        resolved_global_settings = global_settings
    router = RuntimeEventRouter(
        resolved_action_factory,
        resolved_global_settings,
        plugin=plugin,
        plugin_hooks=plugin_hooks,
    )

    fatal_errors = _FatalErrorRelay()
    if scheduler_factory is None:
        if resolved_config.scheduler_kind is RuntimeSchedulerKind.SEQUENTIAL:
            scheduler: HandlerScheduler = SequentialHandlerScheduler(router)
        else:
            scheduler = KeyedSerialHandlerScheduler(
                router,
                worker_count=resolved_config.worker_count,
                pending_limit=resolved_config.scheduler_pending_limit,
                on_fatal_error=fatal_errors,
            )
    else:
        if not isinstance(scheduler_factory, HandlerSchedulerFactory):
            raise TypeError("scheduler_factory must implement HandlerSchedulerFactory")
        scheduler = scheduler_factory(router)
        if not isinstance(scheduler, HandlerScheduler):
            raise TypeError("scheduler_factory must return a HandlerScheduler")

    coordinator = SessionCoordinator(
        boundary.commands,
        register_event=launch_arguments.register_event,
        plugin_uuid=launch_arguments.plugin_uuid,
        readiness=session_readiness,
    )
    plugin_lifecycle = PluginSessionLifecycle(plugin) if plugin is not None else None
    event_pump = RuntimeEventPump(
        boundary.events,
        scheduler,
        poll_interval=resolved_config.event_poll_interval,
        readiness_gate=coordinator.readiness,
        on_ready=plugin_lifecycle.ready if plugin_lifecycle is not None else None,
        on_fatal_error=fatal_errors,
    )
    session_pump = SessionEventPump(
        boundary.session_events,
        coordinator,
        poll_interval=resolved_config.session_poll_interval,
        on_fatal_error=fatal_errors,
    )
    runtime = ComposedStreamDockRuntime(
        boundary=boundary,
        scheduler=scheduler,
        event_pump=event_pump,
        session_pump=session_pump,
        router=router,
        config=resolved_config,
        session_readiness=coordinator.readiness,
        on_stop=plugin_lifecycle.stop if plugin_lifecycle is not None else None,
    )
    fatal_errors.bind(runtime._on_fatal_error)
    bind_boundary_failure = getattr(boundary, "_set_fatal_error_callback", None)
    if callable(bind_boundary_failure):
        bind_boundary_failure(fatal_errors)
    return runtime


StreamDockRuntime = ComposedStreamDockRuntime


def _accepts_positional_arguments(action: Callable[..., object], count: int) -> bool:
    try:
        callable_signature: Signature = signature(action)
    except (TypeError, ValueError):
        return False
    try:
        callable_signature.bind(*(object() for _ in range(count)))
    except TypeError:
        return False
    return True

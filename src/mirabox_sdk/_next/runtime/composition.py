"""Composition root and single lifecycle owner for the next runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable
from inspect import Signature, signature
from threading import Condition, Event, Lock, Thread, current_thread
from typing import Protocol, cast, runtime_checkable

from ...protocols import StreamDockActionDependencies
from ...registration import PluginLaunchArguments
from ..boundary.ports import StreamDockBoundary
from .adapters import LegacyActionFactoryAdapter, LegacyActionRegistry
from .config import RuntimeDispatcherConfig
from .global_settings import DefaultGlobalSettingsState
from .keyed_scheduler import KeyedSerialHandlerScheduler
from .metrics import ActionContextMetrics, RuntimeRouterMetrics, StreamDockRuntimeMetrics
from .models import RuntimeLifecycleState, RuntimeSchedulerKind, transition_runtime_state
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
from .session import SessionCoordinator

logger = logging.getLogger(__name__)


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

        self._boundary = boundary
        self._scheduler = scheduler
        self._event_pump = event_pump
        self._session_pump = session_pump
        self._router = router
        self._config = resolved_config

        self._condition = Condition()
        self._state = RuntimeLifecycleState.NEW
        self._lifecycle_thread: Thread | None = None
        self._shutdown_requested = False
        self._primary_failure: Exception | None = None
        self._cleanup_failures: list[Exception] = []
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
            self._scheduler.start()
            if self._shutdown_is_requested():
                return

            self._event_pump.start()
            event_pump_started = True
            if self._shutdown_is_requested():
                return

            self._session_pump.start()
            session_pump_started = True
            with self._condition:
                if not self._shutdown_requested:
                    self._state = transition_runtime_state(
                        self._state,
                        RuntimeLifecycleState.RUNNING,
                    )
            if self._shutdown_is_requested():
                return

            self._boundary.run_forever()
        except Exception as exc:
            if not self._shutdown_is_requested() or self.failure is not None:
                self._record_primary_failure(exc)
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

            self._cleanup(
                scheduler_attempted=scheduler_attempted,
                event_pump_started=event_pump_started,
                session_pump_started=session_pump_started,
            )

            with self._condition:
                failure = self._primary_failure
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

    def close(self) -> None:
        """Idempotently close the boundary and wait outside runtime callbacks."""

        with self._condition:
            if self._state.terminal:
                return
            self._shutdown_requested = True
            close_before_run = self._state is RuntimeLifecycleState.NEW
            called_from_lifecycle = self._lifecycle_thread is current_thread()

        scheduler_thread_probe = getattr(self._scheduler, "is_dispatch_thread", None)
        called_from_scheduler = bool(
            scheduler_thread_probe() if callable(scheduler_thread_probe) else False
        )
        called_from_worker = (
            called_from_lifecycle
            or called_from_scheduler
            or self._event_pump.is_worker_thread()
            or self._session_pump.is_worker_thread()
        )
        self._ensure_boundary_closed(nonblocking=called_from_worker)
        if called_from_worker:
            return

        if close_before_run:
            self._boundary_close_complete.wait()
            with self._condition:
                if self._state is RuntimeLifecycleState.NEW:
                    self._state = transition_runtime_state(
                        self._state,
                        RuntimeLifecycleState.STOPPED,
                    )
                    self._condition.notify_all()
            self._terminal.set()
            return

        self._terminal.wait()

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

    def _shutdown_is_requested(self) -> bool:
        with self._condition:
            return self._shutdown_requested

    def _record_primary_failure(self, error: Exception) -> None:
        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception")
        with self._condition:
            if self._primary_failure is None:
                self._primary_failure = error
            self._shutdown_requested = True
            self._condition.notify_all()

    def _on_fatal_error(self, error: Exception) -> None:
        self._record_primary_failure(error)
        self._ensure_boundary_closed(nonblocking=True)

    def _ensure_boundary_closed(self, *, nonblocking: bool) -> None:
        owner = False
        with self._boundary_close_lock:
            if not self._boundary_close_started:
                self._boundary_close_started = True
                owner = True
                if nonblocking:
                    thread = Thread(
                        target=self._close_boundary_owned,
                        name="mirabox-next-runtime-close",
                        daemon=True,
                    )
                    self._boundary_close_thread = thread
                    try:
                        thread.start()
                    except Exception as exc:
                        self._record_cleanup_failure("Boundary close worker start", exc)
                        self._boundary_close_complete.set()
                    return

        if owner:
            self._close_boundary_owned()
        elif not nonblocking:
            self._boundary_close_complete.wait()

    def _close_boundary_owned(self) -> None:
        try:
            self._boundary.close()
        except Exception as exc:
            self._record_cleanup_failure("Stream Dock boundary close", exc)
        finally:
            self._boundary_close_complete.set()

    def _cleanup(
        self,
        *,
        scheduler_attempted: bool,
        event_pump_started: bool,
        session_pump_started: bool,
    ) -> None:
        self._ensure_boundary_closed(nonblocking=False)

        event_drained = True
        if event_pump_started:
            event_drained = self._safe_bool_cleanup(
                "Runtime event pump drain",
                self._event_pump.drain,
                timeout=self._config.runtime_drain_timeout,
            )

        if scheduler_attempted:
            self._safe_cleanup("Runtime scheduler stop_accepting", self._scheduler.stop_accepting)

        if event_pump_started and not event_drained:
            logger.warning("Runtime event pump did not drain before shutdown timeout")
            self._safe_cleanup("Runtime event pump request_stop", self._event_pump.request_stop)

        if session_pump_started:
            if not self._safe_bool_cleanup(
                "Runtime session pump drain",
                self._session_pump.drain,
                timeout=self._config.runtime_drain_timeout,
            ):
                logger.warning("Runtime session pump did not drain before shutdown timeout")
            self._safe_bool_cleanup(
                "Runtime session pump stop",
                self._session_pump.stop,
                timeout=self._config.worker_stop_timeout,
            )

        if scheduler_attempted:
            scheduler_timeout = (
                self._config.callback_timeout
                if self._config.callback_timeout is not None
                else self._config.runtime_drain_timeout
            )
            if not self._safe_bool_cleanup(
                "Runtime scheduler drain",
                self._scheduler.drain,
                timeout=scheduler_timeout,
            ):
                logger.warning("Runtime scheduler did not drain before shutdown timeout")
            self._safe_bool_cleanup(
                "Runtime scheduler stop",
                self._scheduler.stop,
                timeout=self._config.worker_stop_timeout,
            )

        if event_pump_started:
            self._safe_bool_cleanup(
                "Runtime event pump stop",
                self._event_pump.stop,
                timeout=self._config.worker_stop_timeout,
            )

        self._release_actions()

    def _release_actions(self) -> None:
        try:
            actions = self._router.contexts.clear()
        except Exception as exc:
            self._record_cleanup_failure("Runtime action clear", exc)
            return
        for action in actions:
            try:
                action.on_will_disappear()
            except Exception as exc:
                self._record_cleanup_failure(
                    f"Runtime action release for context {action.context}",
                    exc,
                )

    def _safe_cleanup(self, name: str, action: Callable[..., object], **kwargs: object) -> None:
        try:
            action(**kwargs)
        except Exception as exc:
            self._record_cleanup_failure(name, exc)

    def _safe_bool_cleanup(
        self,
        name: str,
        action: Callable[..., bool],
        **kwargs: object,
    ) -> bool:
        try:
            return action(**kwargs)
        except Exception as exc:
            self._record_cleanup_failure(name, exc)
            return False

    def _record_cleanup_failure(self, stage: str, error: Exception) -> None:
        with self._condition:
            self._cleanup_failures.append(error)
        logger.error(
            "%s failed; exception_type=%s",
            stage,
            type(error).__name__,
        )


def create_stream_dock_runtime(
    launch_arguments: PluginLaunchArguments,
    *,
    boundary: StreamDockBoundary,
    action_factory: ActionFactory | LegacyActionRegistry,
    action_dependencies: StreamDockActionDependencies | None = None,
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

    create_action = getattr(action_factory, "create", None)
    if not callable(create_action):
        raise TypeError("action_factory must provide create")
    if action_dependencies is not None:
        if not _accepts_positional_arguments(create_action, 4):
            raise TypeError(
                "action_dependencies can only be bound to a four-argument action registry"
            )
        resolved_action_factory: ActionFactory = LegacyActionFactoryAdapter(
            cast(LegacyActionRegistry, action_factory),
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

    global_settings_state = DefaultGlobalSettingsState(
        launch_arguments.plugin_uuid,
        boundary.commands,
    )
    router = RuntimeEventRouter(
        resolved_action_factory,
        global_settings_state,
        plugin_hooks=plugin_hooks,
    )

    if scheduler_factory is None:
        if resolved_config.scheduler_kind is RuntimeSchedulerKind.SEQUENTIAL:
            scheduler: HandlerScheduler = SequentialHandlerScheduler(router)
        else:
            scheduler = KeyedSerialHandlerScheduler(
                router,
                worker_count=resolved_config.worker_count,
                pending_limit=resolved_config.scheduler_pending_limit,
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
    )
    fatal_errors = _FatalErrorRelay()
    event_pump = RuntimeEventPump(
        boundary.events,
        scheduler,
        poll_interval=resolved_config.event_poll_interval,
        readiness_gate=coordinator.readiness,
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
    )
    fatal_errors.bind(runtime._on_fatal_error)
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

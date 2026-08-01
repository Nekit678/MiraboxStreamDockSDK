"""Synchronous routing of typed events into runtime state transitions."""

from __future__ import annotations

import logging
from threading import RLock

from ...events import DidReceiveGlobalSettingsEvent, StreamDockEvent, UnknownStreamDockEvent
from .actions import (
    ActionEventDispatcher,
    BroadcastDispatcher,
    DefaultActionContextManager,
    RuntimeEventDispatchError,
)
from .global_settings import GlobalSettingsCoordinator, GlobalSettingsState
from .metrics import (
    ActionContextMetrics,
    RuntimeRouterMetrics,
    _ActionContextMetricRecorder,
)
from .models import DispatchOutcome, DispatchResult
from .ports import ActionFactory, PluginHooks, RuntimeEventDispatcher
from .routes import (
    RUNTIME_EVENT_REGISTRY,
    RuntimeEventRegistry,
    RuntimeEventScope,
    RuntimeTransition,
)

logger = logging.getLogger(__name__)


class NullPluginHooks(PluginHooks):
    """Default plugin hook implementation with no application side effects."""

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        """Intentionally ignore one forward-compatible event."""


class RuntimeEventRouter(RuntimeEventDispatcher):
    """Route typed events and return one terminal synchronous outcome."""

    def __init__(
        self,
        action_factory: ActionFactory,
        global_settings_state: GlobalSettingsState,
        *,
        plugin_hooks: PluginHooks | None = None,
        registry: RuntimeEventRegistry = RUNTIME_EVENT_REGISTRY,
    ) -> None:
        if plugin_hooks is not None and not isinstance(plugin_hooks, PluginHooks):
            raise TypeError("plugin_hooks must implement PluginHooks")
        if not isinstance(registry, RuntimeEventRegistry):
            raise TypeError("registry must be a RuntimeEventRegistry")

        action_metrics = _ActionContextMetricRecorder()
        contexts = DefaultActionContextManager(action_factory, metrics=action_metrics)
        global_settings = GlobalSettingsCoordinator(
            global_settings_state,
            metrics=action_metrics,
        )
        broadcasts = BroadcastDispatcher(contexts, metrics=action_metrics)
        global_settings_route = registry.get_by_wire_name(DidReceiveGlobalSettingsEvent.event.value)
        if global_settings_route is None:  # pragma: no cover - registry invariant
            raise RuntimeEventDispatchError("global settings route is missing")
        actions = ActionEventDispatcher(
            contexts,
            broadcasts,
            global_settings,
            global_settings_route=global_settings_route,
            metrics=action_metrics,
        )

        self._registry = registry
        self._plugin_hooks = plugin_hooks or NullPluginHooks()
        self._action_metrics = action_metrics
        self._contexts = contexts
        self._global_settings = global_settings
        self._broadcasts = broadcasts
        self._actions = actions
        self._routing_lock = RLock()
        self._known_events_routed = 0
        self._unknown_events_delivered = 0

    @property
    def contexts(self) -> DefaultActionContextManager:
        """Return the action owner used by this synchronous router."""

        return self._contexts

    @property
    def global_settings(self) -> GlobalSettingsCoordinator:
        """Return the plugin-wide settings coordinator used by this router."""

        return self._global_settings

    def dispatch(self, event: StreamDockEvent) -> DispatchResult:
        """Synchronously apply one typed event to application state."""

        route = self._registry.route_for(event)
        if route is None:
            if not isinstance(event, UnknownStreamDockEvent):
                raise RuntimeEventDispatchError(
                    f"known event {event.event_name!r} has no runtime route"
                )
            with self._routing_lock:
                self._unknown_events_delivered += 1
            try:
                self._plugin_hooks.on_unhandled_event(event)
            except Exception as exc:
                logger.error(
                    "Failed to process unknown event %s; exception_type=%s",
                    event.event_name,
                    type(exc).__name__,
                )
                return DispatchResult(DispatchOutcome.CALLBACK_FAILED, exc)
            return DispatchResult(DispatchOutcome.HANDLED)

        with self._routing_lock:
            self._known_events_routed += 1
        if route.scope is RuntimeEventScope.ACTION:
            return self._actions.dispatch(event, route)
        if route.scope is RuntimeEventScope.BROADCAST:
            if route.transition is RuntimeTransition.UPDATE_GLOBAL_SETTINGS:
                if not isinstance(event, DidReceiveGlobalSettingsEvent):
                    raise RuntimeEventDispatchError(
                        "UPDATE_GLOBAL_SETTINGS requires DidReceiveGlobalSettingsEvent"
                    )
                source = self._global_settings.receive(event)
                return self._broadcasts.dispatch(
                    event,
                    route,
                    event_factory=lambda: self._global_settings.new_event(source),
                )
            if route.transition is not RuntimeTransition.NONE:
                raise RuntimeEventDispatchError(
                    f"unsupported broadcast transition {route.transition.value!r}"
                )
            return self._broadcasts.dispatch(event, route)
        raise RuntimeEventDispatchError(f"unsupported runtime scope {route.scope.value!r}")

    def routing_metrics(self) -> RuntimeRouterMetrics:
        """Return immutable known/unknown routing counters."""

        with self._routing_lock:
            return RuntimeRouterMetrics(
                known_events_routed=self._known_events_routed,
                unknown_events_delivered=self._unknown_events_delivered,
            )

    def action_metrics(self) -> ActionContextMetrics:
        """Return immutable action ownership and transition counters."""

        return self._action_metrics.snapshot()

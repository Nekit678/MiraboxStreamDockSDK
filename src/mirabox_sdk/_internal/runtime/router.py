"""Synchronous routing of typed events into runtime state transitions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import RLock

from ...diagnostics import SdkDiagnostic
from ...events import DidReceiveGlobalSettingsEvent, StreamDockEvent, UnknownStreamDockEvent
from ..diagnostics import report_error
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
from .plugin import LegacyPluginHooksAdapter, Plugin
from .ports import ActionFactory, PluginHooks, RuntimeEventDispatcher
from .routes import (
    RUNTIME_EVENT_REGISTRY,
    RuntimeEventRegistry,
    RuntimeEventScope,
    RuntimeTransition,
)

logger = logging.getLogger(__name__)


class NullPluginHooks(Plugin):
    """Default plugin implementation with no application side effects."""


class RuntimeEventRouter(RuntimeEventDispatcher):
    """Route typed events and return one terminal synchronous outcome."""

    def __init__(
        self,
        action_factory: ActionFactory,
        global_settings_state: GlobalSettingsState | GlobalSettingsCoordinator,
        *,
        plugin: Plugin | None = None,
        plugin_hooks: PluginHooks | None = None,
        registry: RuntimeEventRegistry = RUNTIME_EVENT_REGISTRY,
        error_observer: Callable[[SdkDiagnostic], None] | None = None,
    ) -> None:
        if plugin is not None and not isinstance(plugin, Plugin):
            raise TypeError("plugin must extend Plugin or be None")
        if plugin_hooks is not None and not isinstance(plugin_hooks, PluginHooks):
            raise TypeError("plugin_hooks must implement PluginHooks")
        if plugin is not None and plugin_hooks is not None:
            raise TypeError("plugin and plugin_hooks are mutually exclusive")
        if not isinstance(registry, RuntimeEventRegistry):
            raise TypeError("registry must be a RuntimeEventRegistry")

        action_metrics = _ActionContextMetricRecorder()
        contexts = DefaultActionContextManager(action_factory, metrics=action_metrics)
        if isinstance(global_settings_state, GlobalSettingsCoordinator):
            global_settings = global_settings_state
            global_settings.bind_metrics(action_metrics)
        else:
            global_settings = GlobalSettingsCoordinator(
                global_settings_state,
                metrics=action_metrics,
            )
        broadcasts = BroadcastDispatcher(
            contexts,
            metrics=action_metrics,
            error_observer=error_observer,
        )
        global_settings_route = registry.get_by_wire_name(DidReceiveGlobalSettingsEvent.event.value)
        if global_settings_route is None:  # pragma: no cover - registry invariant
            raise RuntimeEventDispatchError("global settings route is missing")
        action_global_settings_route = (
            global_settings_route
            if global_settings_route.scope is RuntimeEventScope.BROADCAST
            else None
        )
        actions = ActionEventDispatcher(
            contexts,
            broadcasts,
            global_settings,
            global_settings_route=action_global_settings_route,
            metrics=action_metrics,
            error_observer=error_observer,
        )

        self._registry = registry
        self._error_observer = error_observer
        if plugin is not None:
            resolved_plugin = plugin
        elif isinstance(plugin_hooks, Plugin):
            resolved_plugin = plugin_hooks
        elif plugin_hooks is not None:
            resolved_plugin = LegacyPluginHooksAdapter(plugin_hooks)
        else:
            resolved_plugin = NullPluginHooks()
        self._plugin = resolved_plugin
        self._action_metrics = action_metrics
        self._contexts = contexts
        self._global_settings = global_settings
        self._broadcasts = broadcasts
        self._actions = actions
        self._routing_lock = RLock()
        self._known_events_routed = 0
        self._unknown_events_delivered = 0
        self._plugin_callbacks_delivered = 0
        self._plugin_callback_failures = 0

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
                self._plugin.on_unhandled_event(event)
            except Exception as exc:
                report_error(
                    logger,
                    exc,
                    event_name=event.event_name,
                    callback="on_unhandled_event",
                    error_observer=self._error_observer,
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
                plugin_result = self._dispatch_plugin(
                    self._global_settings.new_event(source),
                    route.plugin_callback,
                )
                action_result = self._broadcasts.dispatch(
                    event,
                    route,
                    event_factory=lambda: self._global_settings.new_event(source),
                )
                return self._aggregate_results(plugin_result, action_result)
            if route.transition is not RuntimeTransition.NONE:
                raise RuntimeEventDispatchError(
                    f"unsupported broadcast transition {route.transition.value!r}"
                )
            plugin_result = self._dispatch_plugin(event, route.plugin_callback)
            action_result = self._broadcasts.dispatch(event, route)
            return self._aggregate_results(plugin_result, action_result)
        if route.scope is RuntimeEventScope.PLUGIN:
            if route.transition is RuntimeTransition.UPDATE_GLOBAL_SETTINGS:
                if not isinstance(event, DidReceiveGlobalSettingsEvent):
                    raise RuntimeEventDispatchError(
                        "UPDATE_GLOBAL_SETTINGS requires DidReceiveGlobalSettingsEvent"
                    )
                source = self._global_settings.receive(event)
                return self._dispatch_plugin(
                    self._global_settings.new_event(source),
                    route.callback,
                )
            if route.transition is not RuntimeTransition.NONE:
                raise RuntimeEventDispatchError(
                    f"unsupported plugin transition {route.transition.value!r}"
                )
            return self._dispatch_plugin(event, route.callback)
        raise RuntimeEventDispatchError(f"unsupported runtime scope {route.scope.value!r}")

    def _dispatch_plugin(
        self,
        event: StreamDockEvent,
        callback: str | None,
    ) -> DispatchResult:
        """Invoke an optional known-event plugin callback without blocking actions."""

        if callback is None:
            return DispatchResult(DispatchOutcome.HANDLED)
        with self._routing_lock:
            self._plugin_callbacks_delivered += 1
        try:
            getattr(self._plugin, callback)(event)
        except Exception as exc:
            with self._routing_lock:
                self._plugin_callback_failures += 1
            report_error(
                logger,
                exc,
                event_name=event.event_name,
                callback=callback,
                error_observer=self._error_observer,
            )
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, exc)
        return DispatchResult(DispatchOutcome.HANDLED)

    @staticmethod
    def _aggregate_results(*results: DispatchResult) -> DispatchResult:
        """Return the first failure in deterministic target-delivery order."""

        for result in results:
            if result.outcome is DispatchOutcome.CALLBACK_FAILED:
                return result
        return DispatchResult(DispatchOutcome.HANDLED)

    def routing_metrics(self) -> RuntimeRouterMetrics:
        """Return immutable known/unknown routing counters."""

        with self._routing_lock:
            return RuntimeRouterMetrics(
                known_events_routed=self._known_events_routed,
                unknown_events_delivered=self._unknown_events_delivered,
                plugin_callbacks_delivered=self._plugin_callbacks_delivered,
                plugin_callback_failures=self._plugin_callback_failures,
            )

    def action_metrics(self) -> ActionContextMetrics:
        """Return immutable action ownership and transition counters."""

        return self._action_metrics.snapshot()

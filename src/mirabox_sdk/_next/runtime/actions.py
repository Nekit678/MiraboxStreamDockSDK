"""Synchronous action ownership and state-transition dispatch."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import RLock

from ...errors import JsonCodecDecodeError
from ...events import (
    ActionEvent,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    StreamDockEvent,
    TitleParametersDidChangeEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from ...json_types import clone_json_object
from .global_settings import GlobalSettingsCoordinator
from .metrics import ActionContextMetrics, _ActionContextMetricRecorder
from .models import DispatchOutcome, DispatchResult
from .ports import ActionContextManager, ActionFactory, RuntimeActionCallbacks
from .routes import (
    RUNTIME_EVENT_REGISTRY,
    RuntimeEventRoute,
    RuntimeEventScope,
    RuntimeTransition,
)

logger = logging.getLogger(__name__)


class RuntimeActionIdentityError(RuntimeError):
    """Report an action factory result that disagrees with its appearance event."""


class RuntimeEventDispatchError(RuntimeError):
    """Report a route/DTO invariant violation during synchronous dispatch."""


class DefaultActionContextManager(ActionContextManager):
    """Thread-safe owner of application actions keyed by opaque context."""

    def __init__(
        self,
        factory: ActionFactory,
        *,
        metrics: _ActionContextMetricRecorder | None = None,
    ) -> None:
        if not isinstance(factory, ActionFactory):
            raise TypeError("factory must implement ActionFactory")
        self._factory = factory
        self._metrics = metrics or _ActionContextMetricRecorder()
        self._lock = RLock()
        self._actions: dict[str, RuntimeActionCallbacks] = {}

    def create(self, event: WillAppearEvent) -> RuntimeActionCallbacks | None:
        """Create and retain one action, ignoring duplicate contexts."""

        if not isinstance(event, WillAppearEvent):
            raise TypeError("event must be a WillAppearEvent")
        with self._lock:
            if event.context in self._actions:
                self._metrics.increment("duplicate_appearances")
                return None
            try:
                action = self._factory.create(
                    event.action,
                    event.context,
                    clone_json_object(event.settings),
                )
            except JsonCodecDecodeError as exc:
                error = JsonCodecDecodeError(
                    exc.reason,
                    event_name=event.event_name,
                    path=("payload", "settings", *exc.path),
                )
                raise error from exc
            if action is None:
                self._metrics.increment("unknown_action_uuids")
                logger.error(
                    "Unknown action UUID %s for context %s",
                    event.action,
                    event.context,
                )
                return None
            if action.action != event.action or action.context != event.context:
                raise RuntimeActionIdentityError(
                    "action factory returned mismatched identity for "
                    f"UUID {event.action!r}, context {event.context!r}"
                )
            self._actions[event.context] = action
            self._metrics.increment("action_instances_created")
            return action

    def get(self, context: str) -> RuntimeActionCallbacks | None:
        """Return the current action for ``context``, if any."""

        if not isinstance(context, str):
            raise TypeError("context must be a string")
        with self._lock:
            return self._actions.get(context)

    def remove(
        self,
        context: str,
        *,
        expected: RuntimeActionCallbacks | None = None,
    ) -> RuntimeActionCallbacks | None:
        """Remove one action, optionally guarded by object identity."""

        if not isinstance(context, str):
            raise TypeError("context must be a string")
        with self._lock:
            action = self._actions.get(context)
            if action is None or (expected is not None and action is not expected):
                return None
            del self._actions[context]
            self._metrics.increment("actions_removed")
            return action

    def snapshot(self) -> tuple[RuntimeActionCallbacks, ...]:
        """Return an immutable insertion-ordered point-in-time snapshot."""

        with self._lock:
            return tuple(self._actions.values())

    def clear(self) -> tuple[RuntimeActionCallbacks, ...]:
        """Atomically remove and return all retained actions."""

        with self._lock:
            actions = tuple(self._actions.values())
            self._actions.clear()
            self._metrics.increment("actions_removed", len(actions))
            return actions

    def metrics(self) -> ActionContextMetrics:
        """Return the shared immutable action/runtime metric snapshot."""

        return self._metrics.snapshot()


class BroadcastDispatcher:
    """Deliver one barrier event to a stable snapshot of active actions."""

    def __init__(
        self,
        contexts: ActionContextManager,
        *,
        metrics: _ActionContextMetricRecorder | None = None,
    ) -> None:
        if not isinstance(contexts, ActionContextManager):
            raise TypeError("contexts must implement ActionContextManager")
        self._contexts = contexts
        self._metrics = metrics or _ActionContextMetricRecorder()

    def dispatch(
        self,
        event: StreamDockEvent,
        route: RuntimeEventRoute,
        *,
        event_factory: Callable[[], StreamDockEvent] | None = None,
    ) -> DispatchResult:
        """Dispatch to a snapshot and isolate each action callback failure."""

        self._validate_route(event, route)
        actions = self._contexts.snapshot()
        self._metrics.increment("broadcasts")
        self._metrics.increment("broadcast_targets", len(actions))
        first_error: Exception | None = None
        for action in actions:
            target_event = event_factory() if event_factory is not None else event
            result = self.dispatch_one(action, target_event, route)
            if result.error is not None and first_error is None:
                first_error = result.error
        if first_error is not None:
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, first_error)
        return DispatchResult(DispatchOutcome.HANDLED)

    def dispatch_one(
        self,
        action: RuntimeActionCallbacks,
        event: StreamDockEvent,
        route: RuntimeEventRoute,
    ) -> DispatchResult:
        """Invoke one broadcast callback without allowing its failure to escape."""

        self._validate_route(event, route)
        try:
            _invoke(action, event, route)
        except Exception as exc:
            self._metrics.increment("broadcast_failures")
            logger.error(
                "Failed to process broadcast event %s for action %s context %s; exception_type=%s",
                event.event_name,
                action.action,
                action.context,
                type(exc).__name__,
            )
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, exc)
        return DispatchResult(DispatchOutcome.HANDLED)

    @staticmethod
    def _validate_route(event: StreamDockEvent, route: RuntimeEventRoute) -> None:
        if route.scope is not RuntimeEventScope.BROADCAST:
            raise RuntimeEventDispatchError(f"route {route.wire_name!r} is not broadcast-scoped")
        if not isinstance(event, route.event_class):
            raise RuntimeEventDispatchError(
                f"route {route.wire_name!r} cannot dispatch {type(event).__name__}"
            )

    def metrics(self) -> ActionContextMetrics:
        """Return the shared immutable action/runtime metric snapshot."""

        return self._metrics.snapshot()


class ActionEventDispatcher:
    """Apply action-scoped transitions before invoking selected callbacks."""

    def __init__(
        self,
        contexts: ActionContextManager,
        broadcasts: BroadcastDispatcher,
        global_settings: GlobalSettingsCoordinator,
        *,
        global_settings_route: RuntimeEventRoute | None = None,
        metrics: _ActionContextMetricRecorder | None = None,
    ) -> None:
        if not isinstance(contexts, ActionContextManager):
            raise TypeError("contexts must implement ActionContextManager")
        if not isinstance(broadcasts, BroadcastDispatcher):
            raise TypeError("broadcasts must be a BroadcastDispatcher")
        if not isinstance(global_settings, GlobalSettingsCoordinator):
            raise TypeError("global_settings must be a GlobalSettingsCoordinator")
        self._contexts = contexts
        self._broadcasts = broadcasts
        self._global_settings = global_settings
        self._global_settings_route = (
            global_settings_route or RUNTIME_EVENT_REGISTRY[DidReceiveGlobalSettingsEvent]
        )
        self._metrics = metrics or _ActionContextMetricRecorder()

    def dispatch(self, event: StreamDockEvent, route: RuntimeEventRoute) -> DispatchResult:
        """Apply one validated action route synchronously."""

        if route.scope is not RuntimeEventScope.ACTION:
            raise RuntimeEventDispatchError(f"route {route.wire_name!r} is not action-scoped")
        if not isinstance(event, route.event_class) or not isinstance(event, ActionEvent):
            raise RuntimeEventDispatchError(
                f"route {route.wire_name!r} cannot dispatch {type(event).__name__}"
            )

        if route.transition is RuntimeTransition.CREATE_ACTION:
            return self._create(event, route)
        if route.transition is RuntimeTransition.REMOVE_ACTION:
            return self._remove(event, route)
        if route.transition is RuntimeTransition.UPDATE_ACTION_SETTINGS:
            return self._update_settings(event, route)
        if route.transition is RuntimeTransition.UPDATE_ACTION_TITLE:
            return self._update_title(event, route)
        if route.transition is not RuntimeTransition.NONE:
            raise RuntimeEventDispatchError(
                f"unsupported action transition {route.transition.value!r}"
            )
        return self._dispatch_current(event, route)

    def _create(self, event: ActionEvent, route: RuntimeEventRoute) -> DispatchResult:
        if not isinstance(event, WillAppearEvent):
            raise RuntimeEventDispatchError("CREATE_ACTION requires WillAppearEvent")
        try:
            action = self._contexts.create(event)
        except RuntimeActionIdentityError:
            raise
        except Exception as exc:
            logger.error(
                "Failed to create action %s for event %s context %s; exception_type=%s",
                event.action,
                event.event_name,
                event.context,
                type(exc).__name__,
            )
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, exc)
        if action is None:
            return DispatchResult(DispatchOutcome.IGNORED)

        result = self._invoke_action(action, event, route)
        if result.error is not None:
            if self._contexts.remove(event.context, expected=action) is not None:
                self._metrics.increment("appearance_rollbacks")
            try:
                action.on_will_disappear()
            except Exception as exc:
                logger.error(
                    "Failed to roll back action %s context %s; exception_type=%s",
                    action.action,
                    action.context,
                    type(exc).__name__,
                )
            return result

        replay = self._global_settings.new_replay_event()
        if replay is None:
            return result
        return self._broadcasts.dispatch_one(action, replay, self._global_settings_route)

    def _remove(self, event: ActionEvent, route: RuntimeEventRoute) -> DispatchResult:
        if not isinstance(event, WillDisappearEvent):
            raise RuntimeEventDispatchError("REMOVE_ACTION requires WillDisappearEvent")
        action = self._contexts.remove(event.context)
        if action is None:
            return self._missing_context(event)
        return self._invoke_action(action, event, route)

    def _update_settings(
        self,
        event: ActionEvent,
        route: RuntimeEventRoute,
    ) -> DispatchResult:
        if not isinstance(event, DidReceiveSettingsEvent):
            raise RuntimeEventDispatchError(
                "UPDATE_ACTION_SETTINGS requires DidReceiveSettingsEvent"
            )
        action = self._contexts.get(event.context)
        if action is None:
            return self._missing_context(event)
        try:
            action.update_settings_from_wire(event.settings)
        except JsonCodecDecodeError as exc:
            error = JsonCodecDecodeError(
                exc.reason,
                event_name=event.event_name,
                path=("payload", "settings", *exc.path),
            )
            error.__cause__ = exc
            self._metrics.increment("settings_update_failures")
            logger.error(
                "Failed to update settings for event %s action %s context %s; exception_type=%s",
                event.event_name,
                event.action,
                event.context,
                type(error).__name__,
            )
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, error)
        except Exception as exc:
            self._metrics.increment("settings_update_failures")
            logger.error(
                "Failed to update settings for event %s action %s context %s; exception_type=%s",
                event.event_name,
                event.action,
                event.context,
                type(exc).__name__,
            )
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, exc)
        self._metrics.increment("settings_updates")
        return self._invoke_action(action, event, route)

    def _update_title(
        self,
        event: ActionEvent,
        route: RuntimeEventRoute,
    ) -> DispatchResult:
        if not isinstance(event, TitleParametersDidChangeEvent):
            raise RuntimeEventDispatchError(
                "UPDATE_ACTION_TITLE requires TitleParametersDidChangeEvent"
            )
        action = self._contexts.get(event.context)
        if action is None:
            return self._missing_context(event)
        try:
            action.title = event.title
            action.title_parameters = event.title_parameters
        except Exception as exc:
            logger.error(
                "Failed to update title for event %s action %s context %s; exception_type=%s",
                event.event_name,
                event.action,
                event.context,
                type(exc).__name__,
            )
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, exc)
        self._metrics.increment("title_updates")
        return self._invoke_action(action, event, route)

    def _dispatch_current(
        self,
        event: ActionEvent,
        route: RuntimeEventRoute,
    ) -> DispatchResult:
        action = self._contexts.get(event.context)
        if action is None:
            return self._missing_context(event)
        return self._invoke_action(action, event, route)

    def _missing_context(self, event: ActionEvent) -> DispatchResult:
        self._metrics.increment("missing_contexts")
        logger.warning(
            "Ignoring event %s for missing action %s context %s",
            event.event_name,
            event.action,
            event.context,
        )
        return DispatchResult(DispatchOutcome.IGNORED)

    @staticmethod
    def _invoke_action(
        action: RuntimeActionCallbacks,
        event: StreamDockEvent,
        route: RuntimeEventRoute,
    ) -> DispatchResult:
        try:
            _invoke(action, event, route)
        except Exception as exc:
            logger.error(
                "Failed to process action event %s for action %s context %s; exception_type=%s",
                event.event_name,
                action.action,
                action.context,
                type(exc).__name__,
            )
            return DispatchResult(DispatchOutcome.CALLBACK_FAILED, exc)
        return DispatchResult(DispatchOutcome.HANDLED)

    def metrics(self) -> ActionContextMetrics:
        """Return the shared immutable action/runtime metric snapshot."""

        return self._metrics.snapshot()


def _invoke(
    action: RuntimeActionCallbacks,
    event: StreamDockEvent,
    route: RuntimeEventRoute,
) -> None:
    callback = getattr(action, route.callback)
    callback(event)

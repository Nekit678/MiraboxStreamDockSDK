"""State stores used by the Stream Dock event runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from threading import RLock
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from .action import Action
from .action_registry import ActionRegistry
from .codecs import JSON_OBJECT_CODEC, JsonCodec
from .commands import SetGlobalSettingsCommand
from .errors import JsonCodecDecodeError
from .events import (
    ActionEvent,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    EventDescriptor,
    EventScope,
    StreamDockEvent,
    TitleParametersDidChangeEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from .json_types import (
    JsonObject,
    ValidatedJsonObject,
    _copy_on_write_json_object,
    _prepare_copy_on_write_json_object,
    clone_json_object,
)
from .protocols import StreamDockActionDependencies, StreamDockSender

DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)
GlobalSettingsT = TypeVar("GlobalSettingsT")

logger = logging.getLogger(__name__)


class ActionStore(Generic[DependenciesT]):
    """Own active action instances and their context lookup.

    Event dispatch is intentionally serialized by the connection, but snapshot
    reads and lifecycle removal may occur from another thread during shutdown.
    The store therefore protects its mapping and never exposes the mutable
    backing dictionary.
    """

    def __init__(
        self,
        registry: ActionRegistry[DependenciesT],
        dependencies: DependenciesT,
    ) -> None:
        """Create an empty store backed by one action registry.

        Args:
            registry: Per-plugin action UUID registry.
            dependencies: Shared dependency container for new actions.
        """

        self._registry = registry
        self._dependencies = dependencies
        self._lock = RLock()
        self._actions: dict[str, Action[Any, DependenciesT]] = {}

    @property
    def actions(self) -> Mapping[str, Action[Any, DependenciesT]]:
        """Return a read-only point-in-time snapshot keyed by context."""

        with self._lock:
            return MappingProxyType(self._actions.copy())

    def replace(self, actions: Mapping[str, Action[Any, DependenciesT]]) -> None:
        """Replace all active actions with a shallow mapping snapshot.

        Args:
            actions: Complete context-to-action mapping to retain.
        """

        with self._lock:
            self._actions = dict(actions)

    def get(self, context: str) -> Action[Any, DependenciesT] | None:
        """Return the action for ``context``, if it is active.

        Args:
            context: Opaque action-instance context.
        """

        with self._lock:
            return self._actions.get(context)

    def create(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
    ) -> tuple[Action[Any, DependenciesT] | None, bool]:
        """Create and retain an action unless its context already exists.

        Args:
            action_uuid: Manifest UUID used to resolve the action class.
            context: Opaque action-instance context.
            settings: Raw appearance settings passed through the action codec.

        Returns:
            ``(action, True)`` for a newly retained action, ``(action, False)``
            for an existing context, or ``(None, False)`` for an unknown UUID.

        Raises:
            JsonCodecDecodeError: If the registered action rejects ``settings``.
        """

        with self._lock:
            existing = self._actions.get(context)
            if existing is not None:
                return existing, False
            action = self._registry.create(
                action_uuid,
                context,
                settings,
                self._dependencies,
            )
            if action is None:
                return None, False
            self._actions[context] = action
            return action, True

    def create_and_dispatch(
        self,
        event: WillAppearEvent,
        descriptor: EventDescriptor,
    ) -> Action[Any, DependenciesT] | None:
        """Create one context and invoke its appearance callback.

        Args:
            event: Validated appearance event.
            descriptor: Registry descriptor selecting the action callback.

        Returns:
            The newly active action, or ``None`` for an existing context or
            unknown UUID.
        """

        try:
            action, created = self.create(
                event.action,
                event.context,
                event.settings,
            )
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                event_name=event.event_name,
                path=("payload", "settings", *exc.path),
            ) from exc
        if action is None:
            logger.error("Unknown action UUID: %s", event.action)
            return None
        if not created:
            return None
        try:
            self._invoke(action, event, descriptor)
        except Exception:
            self.remove(event.context, expected=action)
            try:
                action.on_will_disappear()
            except Exception:
                logger.exception("Failed to roll back action context %s", event.context)
            raise
        return action

    def remove(
        self,
        context: str,
        *,
        expected: Action[Any, DependenciesT] | None = None,
    ) -> Action[Any, DependenciesT] | None:
        """Remove and return one action, optionally only if identity matches.

        Args:
            context: Opaque action-instance context.
            expected: Optional identity guard used during failed-creation
                rollback.
        """

        with self._lock:
            action = self._actions.get(context)
            if action is None or (expected is not None and action is not expected):
                return None
            del self._actions[context]
            return action

    def snapshot(self) -> tuple[Action[Any, DependenciesT], ...]:
        """Return active actions in insertion order."""

        with self._lock:
            return tuple(self._actions.values())

    def clear(self) -> tuple[Action[Any, DependenciesT], ...]:
        """Atomically remove and return every active action."""

        with self._lock:
            actions = tuple(self._actions.values())
            self._actions.clear()
            return actions

    def dispatch(self, event: StreamDockEvent, descriptor: EventDescriptor) -> None:
        """Route a registry-described action or broadcast event.

        Args:
            event: Validated event to deliver.
            descriptor: Registry routing and callback metadata.
        """

        if descriptor.scope is EventScope.BROADCAST:
            self.broadcast(event, descriptor)
            return
        if not isinstance(event, ActionEvent):  # pragma: no cover - registry invariant
            raise AssertionError(f"Action-scoped event {event.event_name!r} lacks action routing")
        action = self.get(event.context)
        if action is not None:
            self._invoke(action, event, descriptor)

    def remove_and_dispatch(
        self,
        event: WillDisappearEvent,
        descriptor: EventDescriptor,
    ) -> None:
        """Remove one context before invoking its disappearance callback.

        Args:
            event: Validated disappearance event.
            descriptor: Registry descriptor selecting the action callback.
        """

        action = self.remove(event.context)
        if action is not None:
            self._invoke(action, event, descriptor)

    def update_settings_and_dispatch(
        self,
        event: DidReceiveSettingsEvent,
        descriptor: EventDescriptor,
    ) -> None:
        """Update one action's settings before invoking its callback.

        Args:
            event: Validated settings event.
            descriptor: Registry descriptor selecting the action callback.
        """

        action = self.get(event.context)
        if action is None:
            return
        try:
            action.update_settings_from_wire(event.settings)
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                event_name=event.event_name,
                path=("payload", "settings", *exc.path),
            ) from exc
        self._invoke(action, event, descriptor)

    def update_title_and_dispatch(
        self,
        event: TitleParametersDidChangeEvent,
        descriptor: EventDescriptor,
    ) -> None:
        """Update one action's title state before invoking its callback.

        Args:
            event: Validated title-parameters event.
            descriptor: Registry descriptor selecting the action callback.
        """

        action = self.get(event.context)
        if action is None:
            return
        action.title = event.title
        action.title_parameters = event.title_parameters
        self._invoke(action, event, descriptor)

    def broadcast(self, event: StreamDockEvent, descriptor: EventDescriptor) -> None:
        """Invoke one broadcast callback for every current action.

        Args:
            event: Validated immutable event shared by the callbacks.
            descriptor: Registry descriptor selecting the action callback.
        """

        for action in self.snapshot():
            self.dispatch_safely(action, event, descriptor)

    def broadcast_factory(
        self,
        event_factory: Callable[[], StreamDockEvent],
        descriptor: EventDescriptor,
    ) -> None:
        """Broadcast a separately isolated event to each current action.

        Args:
            event_factory: Callable returning one event per action.
            descriptor: Registry descriptor selecting the action callback.
        """

        for action in self.snapshot():
            self.dispatch_safely(action, event_factory(), descriptor)

    def dispatch_safely(
        self,
        action: Action[Any, DependenciesT],
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        """Invoke one callback without blocking other actions on failure.

        Args:
            action: Target action instance.
            event: Validated event to deliver.
            descriptor: Registry descriptor selecting the action callback.
        """

        try:
            self._invoke(action, event, descriptor)
        except Exception:
            logger.exception(
                "Failed to process broadcast Stream Dock event %s for action %s context %s",
                event.event_name,
                action.action,
                action.context,
            )

    @staticmethod
    def _invoke(
        action: Action[Any, DependenciesT],
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        callback = getattr(action, descriptor.callback)
        callback(event)


class GlobalSettingsStore:
    """Own plugin-wide settings, persistence, and isolated COW replays.

    :meth:`set`, :meth:`set_typed`, :meth:`update`, and :meth:`receive` are
    serialized and may be called from different application threads. The
    mutable object returned by :attr:`settings` is a copy-on-write convenience
    view, not a concurrently mutable container; callers must confine each view
    to one thread at a time.
    """

    def __init__(self, context: str, stream_dock: StreamDockSender) -> None:
        """Create an empty, not-yet-loaded store for one plugin UUID.

        Args:
            context: Plugin UUID used by global-settings commands.
            stream_dock: Thread-safe outbound command sender.
        """

        self._context = context
        self._stream_dock = stream_dock
        self._lock = RLock()
        self._snapshot: JsonObject = {}
        self._source = _prepare_copy_on_write_json_object(self._snapshot)
        self._settings: JsonObject = _copy_on_write_json_object(self._source)
        self._snapshot_dirty = False
        self._loaded = False

    @property
    def settings(self) -> JsonObject:
        """Return the current mutable copy-on-write settings view."""

        with self._lock:
            return self._settings

    @property
    def loaded(self) -> bool:
        """Return whether settings have been received or persisted locally."""

        with self._lock:
            return self._loaded

    def replace_local(self, settings: JsonObject) -> None:
        """Replace the local view without changing its loaded/replay status.

        Args:
            settings: Raw JSON object to validate, clone, and retain locally.
        """

        snapshot = clone_json_object(settings)
        with self._lock:
            loaded = self._loaded
            self._replace_locked(snapshot)
            self._loaded = loaded

    def receive(self, settings: JsonObject) -> ValidatedJsonObject:
        """Own settings received from Stream Dock and return their replay source.

        Args:
            settings: Validated wire settings to isolate from the event.

        Returns:
            Immutable backing snapshot for isolated broadcasts.
        """

        snapshot = clone_json_object(settings)
        with self._lock:
            self._replace_locked(snapshot)
            return self._source

    def update(self, update: Callable[[JsonObject], None]) -> None:
        """Apply, persist, and commit one rollback-safe settings transaction.

        Args:
            update: Callback that mutates an isolated COW draft.

        Raises:
            JsonCodecEncodeError: If the completed draft is not valid JSON.
            Exception: Any callback or outbound send failure.
        """

        with self._lock:
            draft = _copy_on_write_json_object(self._current_source_locked())
            update(draft)
            command = SetGlobalSettingsCommand.from_settings(
                self._context,
                draft,
                JSON_OBJECT_CODEC,
            )
            self._send_and_commit_locked(command)

    def set(self, settings: JsonObject) -> None:
        """Validate, persist, and commit raw JSON settings.

        Args:
            settings: Plugin-wide JSON settings to persist.
        """

        with self._lock:
            command = SetGlobalSettingsCommand.from_settings(
                self._context,
                settings,
                JSON_OBJECT_CODEC,
            )
            self._send_and_commit_locked(command)

    def set_typed(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Encode, persist, and commit typed plugin-wide settings.

        Args:
            settings: Plugin-owned settings value.
            codec: Codec producing its JSON representation.
        """

        with self._lock:
            command = SetGlobalSettingsCommand.from_settings(self._context, settings, codec)
            self._send_and_commit_locked(command)

    def new_event(
        self,
        source: ValidatedJsonObject | None = None,
    ) -> DidReceiveGlobalSettingsEvent:
        """Return an isolated event backed by the selected immutable snapshot.

        Args:
            source: Optional historical broadcast source. ``None`` uses the
                latest local state.
        """

        with self._lock:
            if source is None:
                source = self._current_source_locked()
            return DidReceiveGlobalSettingsEvent(settings=_copy_on_write_json_object(source))

    def _send_and_commit_locked(self, command: SetGlobalSettingsCommand) -> None:
        self._stream_dock.send(command)
        self._replace_locked(command._owned_settings_source())

    def _current_source_locked(self) -> ValidatedJsonObject:
        if self._snapshot_dirty:
            self._snapshot = clone_json_object(self._settings)
            self._source = _prepare_copy_on_write_json_object(self._snapshot)
            self._snapshot_dirty = False
        return self._source

    def _replace_locked(
        self,
        snapshot: JsonObject | ValidatedJsonObject,
    ) -> None:
        settings: JsonObject

        def mark_snapshot_dirty_after_mutation() -> None:
            with self._lock:
                if self._settings is settings:
                    self._snapshot_dirty = True

        if isinstance(snapshot, ValidatedJsonObject):
            source = snapshot
            owned_snapshot = source._value
        else:
            source = _prepare_copy_on_write_json_object(snapshot)
            owned_snapshot = snapshot
        settings = _copy_on_write_json_object(
            source,
            on_mutation=mark_snapshot_dirty_after_mutation,
        )
        self._snapshot = owned_snapshot
        self._source = source
        self._settings = settings
        self._snapshot_dirty = False
        self._loaded = True

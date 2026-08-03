"""Plugin-wide settings state and isolated replay coordination."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from threading import RLock
from typing import Protocol, TypeVar, runtime_checkable

from ...codecs import JsonCodec
from ...commands import SetGlobalSettingsCommand
from ...events import DidReceiveGlobalSettingsEvent
from ...json_types import JsonObject, ValidatedJsonObject, clone_json_object
from ..messaging.ports import OutboundCommandSink
from .metrics import ActionContextMetrics, _ActionContextMetricRecorder

GlobalSettingsT = TypeVar("GlobalSettingsT")


@runtime_checkable
class GlobalSettingsState(Protocol):
    """State backend required by :class:`GlobalSettingsCoordinator`.

    The supported legacy :class:`mirabox_sdk.GlobalSettingsStore` implements
    this protocol structurally. Keeping the dependency narrow prevents the new
    runtime package from importing legacy event-routing metadata.
    """

    @property
    @abstractmethod
    def settings(self) -> JsonObject: ...

    @property
    @abstractmethod
    def loaded(self) -> bool: ...

    @abstractmethod
    def receive(self, settings: JsonObject) -> ValidatedJsonObject: ...

    @abstractmethod
    def new_event(
        self,
        source: ValidatedJsonObject | None = None,
    ) -> DidReceiveGlobalSettingsEvent: ...

    @abstractmethod
    def update(self, update: Callable[[JsonObject], None]) -> None: ...

    @abstractmethod
    def set(self, settings: JsonObject) -> None: ...

    @abstractmethod
    def set_typed(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None: ...


class GlobalSettingsCoordinator:
    """Own global-settings transitions and produce isolated callback events."""

    def __init__(
        self,
        state: GlobalSettingsState,
        *,
        metrics: _ActionContextMetricRecorder | None = None,
    ) -> None:
        if not isinstance(state, GlobalSettingsState):
            raise TypeError("state must implement GlobalSettingsState")
        self._state = state
        self._metrics = metrics or _ActionContextMetricRecorder()

    @property
    def settings(self) -> JsonObject:
        """Return the backend's current application-facing settings view."""

        return self._state.settings

    @property
    def loaded(self) -> bool:
        """Return whether a received or locally persisted snapshot exists."""

        return self._state.loaded

    def receive(self, event: DidReceiveGlobalSettingsEvent) -> ValidatedJsonObject:
        """Replace state before callbacks and return the immutable replay source."""

        if not isinstance(event, DidReceiveGlobalSettingsEvent):
            raise TypeError("event must be a DidReceiveGlobalSettingsEvent")
        source = self._state.receive(event.settings)
        self._metrics.increment("global_settings_updates")
        return source

    def new_event(
        self,
        source: ValidatedJsonObject | None = None,
    ) -> DidReceiveGlobalSettingsEvent:
        """Create one callback-owned event isolated from all other views."""

        return self._state.new_event(source)

    def new_replay_event(self) -> DidReceiveGlobalSettingsEvent | None:
        """Return the latest isolated event for a late action, if state is loaded."""

        if not self.loaded:
            return None
        event = self.new_event()
        self._metrics.increment("global_settings_replays")
        return event

    def update(self, update: Callable[[JsonObject], None]) -> None:
        """Persist and commit one rollback-safe raw settings transaction."""

        self._state.update(update)
        self._metrics.increment("global_settings_updates")

    def set(self, settings: JsonObject) -> None:
        """Persist raw settings and commit them only after send succeeds."""

        self._state.set(settings)
        self._metrics.increment("global_settings_updates")

    def set_typed(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Persist typed settings and commit their encoded representation."""

        self._state.set_typed(settings, codec)
        self._metrics.increment("global_settings_updates")

    def metrics(self) -> ActionContextMetrics:
        """Return the shared immutable action/runtime metric snapshot."""

        return self._metrics.snapshot()


class DefaultGlobalSettingsState(GlobalSettingsState):
    """Runtime-owned global settings state over the typed command sink."""

    def __init__(self, context: str, sender: OutboundCommandSink) -> None:
        if not isinstance(context, str) or not context.strip():
            raise ValueError("context must be a non-empty string")
        if not isinstance(sender, OutboundCommandSink):
            raise TypeError("sender must implement OutboundCommandSink")
        self._context = context
        self._sender = sender
        self._lock = RLock()
        self._settings: JsonObject = {}
        self._loaded = False

    @property
    def settings(self) -> JsonObject:
        with self._lock:
            return self._settings

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def receive(self, settings: JsonObject) -> ValidatedJsonObject:
        source = ValidatedJsonObject(settings)
        with self._lock:
            self._replace_locked(source)
        return source

    def new_event(
        self,
        source: ValidatedJsonObject | None = None,
    ) -> DidReceiveGlobalSettingsEvent:
        with self._lock:
            resolved_source = source or ValidatedJsonObject(self._settings)
            return DidReceiveGlobalSettingsEvent(settings=resolved_source.isolated_copy())

    def update(self, update: Callable[[JsonObject], None]) -> None:
        if not callable(update):
            raise TypeError("update must be callable")
        with self._lock:
            draft = clone_json_object(self._settings)
            update(draft)
            self._send_and_replace_locked(SetGlobalSettingsCommand(self._context, draft))

    def set(self, settings: JsonObject) -> None:
        with self._lock:
            self._send_and_replace_locked(SetGlobalSettingsCommand(self._context, settings))

    def set_typed(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        with self._lock:
            command = SetGlobalSettingsCommand.from_settings(self._context, settings, codec)
            self._send_and_replace_locked(command)

    def _send_and_replace_locked(self, command: SetGlobalSettingsCommand) -> None:
        self._sender.send(command)
        self._replace_locked(ValidatedJsonObject(command.settings))

    def _replace_locked(self, source: ValidatedJsonObject) -> None:
        self._settings = source.isolated_copy()
        self._loaded = True

"""Plugin-wide settings state and isolated replay coordination."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Condition, RLock
from typing import Protocol, TypeVar, runtime_checkable

from ...codecs import JsonCodec
from ...commands import SetGlobalSettingsCommand
from ...completion import CommandFuture
from ...events import DidReceiveGlobalSettingsEvent
from ...global_settings import GlobalSettings, GlobalSettingsBusyError
from ...json_types import JsonObject, ValidatedJsonObject
from ..messaging.ports import OutboundCommandSink
from .metrics import ActionContextMetrics, _ActionContextMetricRecorder

GlobalSettingsT = TypeVar("GlobalSettingsT")


@runtime_checkable
class GlobalSettingsState(Protocol):
    """State backend required by :class:`GlobalSettingsCoordinator`.

    Keeping this dependency narrow isolates routing from the concrete
    runtime-owned state implementation.
    """

    @property
    @abstractmethod
    def settings(self) -> JsonObject:
        """Return a fresh plain deep copy owned by the caller.

        Neither the backend nor other readers may share mutable containers
        with this result. The coordinator forwards it without another copy.
        """

        ...

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

    @abstractmethod
    def update_async(self, update: Callable[[JsonObject], None]) -> CommandFuture: ...

    @abstractmethod
    def set_async(self, settings: JsonObject) -> CommandFuture: ...

    @abstractmethod
    def set_typed_async(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> CommandFuture: ...


class GlobalSettingsCoordinator(GlobalSettings):
    """Own one public settings facade and its internal callback transitions."""

    def __init__(
        self,
        state: GlobalSettingsState,
        *,
        metrics: _ActionContextMetricRecorder | None = None,
    ) -> None:
        if not isinstance(state, GlobalSettingsState):
            raise TypeError("state must implement GlobalSettingsState")
        self._state = state
        self._metrics = metrics

    def bind_metrics(self, metrics: _ActionContextMetricRecorder) -> None:
        """Attach the router's shared metric recorder during composition."""

        if not isinstance(metrics, _ActionContextMetricRecorder):
            raise TypeError("metrics must be an _ActionContextMetricRecorder")
        if self._metrics is not None and self._metrics is not metrics:
            raise RuntimeError("global settings metrics are already bound")
        self._metrics = metrics

    @property
    def loaded(self) -> bool:
        """Return whether a received or locally persisted snapshot exists."""

        return self._state.loaded

    def snapshot(self) -> JsonObject:
        """Return an isolated snapshot of the current plugin-wide settings."""

        return self._state.settings

    def receive(self, event: DidReceiveGlobalSettingsEvent) -> ValidatedJsonObject:
        """Replace state before callbacks and return the immutable replay source."""

        if not isinstance(event, DidReceiveGlobalSettingsEvent):
            raise TypeError("event must be a DidReceiveGlobalSettingsEvent")
        source = self._state.receive(event.settings)
        self._increment_metric("global_settings_updates")
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
        self._increment_metric("global_settings_replays")
        return event

    def update(self, update: Callable[[JsonObject], None]) -> None:
        """Persist and commit one rollback-safe raw settings transaction."""

        self._state.update(update)
        self._increment_metric("global_settings_updates")

    def set(self, settings: JsonObject) -> None:
        """Persist raw settings and commit them only after send succeeds."""

        self._state.set(settings)
        self._increment_metric("global_settings_updates")

    def set_typed(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Persist typed settings and commit their encoded representation."""

        self._state.set_typed(settings, codec)
        self._increment_metric("global_settings_updates")

    def metrics(self) -> ActionContextMetrics:
        """Return the shared immutable action/runtime metric snapshot."""

        if self._metrics is None:
            return ActionContextMetrics()
        return self._metrics.snapshot()

    def update_async(self, update: Callable[[JsonObject], None]) -> CommandFuture:
        return self._track_async_update(self._state.update_async(update))

    def set_async(self, settings: JsonObject) -> CommandFuture:
        return self._track_async_update(self._state.set_async(settings))

    def set_typed_async(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> CommandFuture:
        return self._track_async_update(self._state.set_typed_async(settings, codec))

    def _track_async_update(self, persistence: CommandFuture) -> CommandFuture:
        completion = CommandFuture()

        def finished(result: CommandFuture) -> None:
            error = result.exception(timeout=0)
            if error is None:
                self._increment_metric("global_settings_updates")
            completion._finish(error=error)

        persistence.add_done_callback(finished)
        return completion

    def _increment_metric(self, field_name: str) -> None:
        if self._metrics is not None:
            self._metrics.increment(field_name)


class DefaultGlobalSettingsState(GlobalSettingsState):
    """Runtime-owned global settings state over the typed command sink."""

    def __init__(self, context: str, sender: OutboundCommandSink) -> None:
        if not isinstance(context, str) or not context.strip():
            raise ValueError("context must be a non-empty string")
        if not isinstance(sender, OutboundCommandSink):
            raise TypeError("sender must implement OutboundCommandSink")
        self._context = context
        self._sender = sender
        # Writes and incoming replacements remain serial. Readers only take the
        # state lock and can see the last committed snapshot during transport I/O.
        self._write_lock = RLock()
        self._write_depth = 0
        self._lock = Condition()
        self._pending: CommandFuture | None = None
        self._settings = ValidatedJsonObject({})
        self._loaded = False

    @property
    def settings(self) -> JsonObject:
        """Return an isolated copy without exposing backend-owned containers."""

        with self._lock:
            return self._settings.isolated_copy()

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def receive(self, settings: JsonObject) -> ValidatedJsonObject:
        source = ValidatedJsonObject(settings)
        with self._write_transaction():
            with self._lock:
                self._replace_locked(source)
        return source

    def new_event(
        self,
        source: ValidatedJsonObject | None = None,
    ) -> DidReceiveGlobalSettingsEvent:
        with self._lock:
            resolved_source = source if source is not None else self._settings
            return DidReceiveGlobalSettingsEvent(settings=resolved_source.isolated_copy())

    def update(self, update: Callable[[JsonObject], None]) -> None:
        if not callable(update):
            raise TypeError("update must be callable")
        with self._write_transaction():
            draft = self.settings
            update(draft)
            self._send_and_replace(SetGlobalSettingsCommand(self._context, draft))

    def set(self, settings: JsonObject) -> None:
        with self._write_transaction():
            self._send_and_replace(SetGlobalSettingsCommand(self._context, settings))

    def set_typed(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        with self._write_transaction():
            command = SetGlobalSettingsCommand.from_settings(self._context, settings, codec)
            self._send_and_replace(command)

    def _wait_for_pending(self) -> None:
        with self._lock:
            # A reentrant send hook must not wait on its own async submission.
            if self._write_depth and self._pending is not None:
                raise GlobalSettingsBusyError(
                    "A global settings transaction is already in progress"
                )
            self._lock.wait_for(lambda: self._pending is None)

    @contextmanager
    def _write_transaction(self, *, blocking: bool = True) -> Iterator[None]:
        if not self._write_lock.acquire(blocking=blocking):
            raise GlobalSettingsBusyError("A global settings transaction is already in progress")
        try:
            if blocking:
                self._wait_for_pending()
            else:
                with self._lock:
                    self._require_idle_locked()
            self._write_depth += 1
            try:
                yield
            finally:
                self._write_depth -= 1
        finally:
            self._write_lock.release()

    def _send_and_replace(self, command: SetGlobalSettingsCommand) -> None:
        self._sender.send(command)
        with self._lock:
            self._replace_locked(ValidatedJsonObject(command.settings))

    def update_async(self, update: Callable[[JsonObject], None]) -> CommandFuture:
        if not callable(update):
            raise TypeError("update must be callable")

        def prepare() -> SetGlobalSettingsCommand:
            draft = self.settings
            update(draft)
            return SetGlobalSettingsCommand(self._context, draft)

        return self._persist_async(prepare)

    def set_async(self, settings: JsonObject) -> CommandFuture:
        return self._persist_async(lambda: SetGlobalSettingsCommand(self._context, settings))

    def set_typed_async(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> CommandFuture:
        return self._persist_async(
            lambda: SetGlobalSettingsCommand.from_settings(self._context, settings, codec)
        )

    def _persist_async(self, prepare: Callable[[], SetGlobalSettingsCommand]) -> CommandFuture:
        with self._write_transaction(blocking=False):
            command = prepare()
            source = ValidatedJsonObject(command.settings)
            completion = CommandFuture()
            with self._lock:
                self._pending = completion
            try:
                sent = self._sender.send_async(command)
            except BaseException:
                with self._lock:
                    self._pending = None
                    self._lock.notify_all()
                raise
            sent.add_done_callback(lambda result: self._complete_async(result, source, completion))
            return completion

    def _require_idle_locked(self) -> None:
        if self._pending is not None or self._write_depth:
            raise GlobalSettingsBusyError("A global settings transaction is already in progress")

    def _complete_async(
        self,
        sent: CommandFuture,
        source: ValidatedJsonObject,
        completion: CommandFuture,
    ) -> None:
        error = sent.exception(timeout=0)
        with self._lock:
            if error is None:
                self._replace_locked(source)
            self._pending = None
            self._lock.notify_all()
        # Completion observers may read settings or submit another transaction.
        # Invoke them after releasing the state lock.
        completion._finish(error=error)

    def _replace_locked(self, source: ValidatedJsonObject) -> None:
        self._settings = source
        self._loaded = True

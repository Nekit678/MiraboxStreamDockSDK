"""Public port for one application's plugin-wide settings state."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Protocol, TypeVar, runtime_checkable

from .codecs import JsonCodec
from .completion import CommandFuture
from .json_types import JsonObject
from .json_types import JsonValue as JsonValue  # Resolve recursive JSON type hints.

GlobalSettingsT = TypeVar("GlobalSettingsT")


class GlobalSettingsBusyError(RuntimeError):
    """Reject an async write while another settings transaction is active."""


@runtime_checkable
class GlobalSettings(Protocol):
    """Runtime-owned plugin-wide settings with isolated snapshots.

    Every consumer in one application must receive the same instance. Incoming
    ``didReceiveGlobalSettings`` events update that instance before callbacks;
    local writes update it only after their outbound command succeeds.
    """

    @property
    @abstractmethod
    def loaded(self) -> bool:
        """Return whether a received or successfully persisted value exists."""

        ...

    @abstractmethod
    def snapshot(self) -> JsonObject:
        """Return an isolated copy of the current raw settings object."""

        ...

    @abstractmethod
    def update(self, update: Callable[[JsonObject], None]) -> None:
        """Persist one rollback-safe mutation of an isolated settings draft."""

        ...

    @abstractmethod
    def set(self, settings: JsonObject) -> None:
        """Persist raw settings and commit them after command success."""

        ...

    @abstractmethod
    def set_typed(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Encode, persist, and commit plugin-owned typed settings."""

        ...

    @abstractmethod
    def update_async(self, update: Callable[[JsonObject], None]) -> CommandFuture:
        """Mutate and validate on this thread, then persist without waiting.

        Only one transaction may be in progress. Raises
        :class:`GlobalSettingsBusyError` before invoking ``update`` if busy.
        The returned future completes after commit or rollback. Observe it with
        ``add_done_callback``; a wait timeout does not cancel persistence.
        """

        ...

    @abstractmethod
    def set_async(self, settings: JsonObject) -> CommandFuture:
        """Validate and submit a replacement, committing only after success.

        Raises :class:`GlobalSettingsBusyError` if a transaction is in progress.
        Validation and command acceptance errors propagate before returning.
        """

        ...

    @abstractmethod
    def set_typed_async(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> CommandFuture:
        """Encode on this thread and persist with the same async guarantees."""

        ...


__all__ = ["GlobalSettings", "GlobalSettingsBusyError"]

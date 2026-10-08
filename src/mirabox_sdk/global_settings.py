"""Public port for one application's plugin-wide settings state."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Protocol, TypeVar, runtime_checkable

from .codecs import JsonCodec
from .json_types import JsonObject
from .json_types import JsonValue as JsonValue  # Resolve recursive JSON type hints.

GlobalSettingsT = TypeVar("GlobalSettingsT")


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


__all__ = ["GlobalSettings"]

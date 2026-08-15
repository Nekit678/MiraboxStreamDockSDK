"""Stable application-facing ports for the runtime dispatcher."""

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

from .._next.runtime.ports import ActionFactory, PluginHooks, RuntimeLifecycle
from ..global_settings import GlobalSettings
from ..protocols import StreamDockSender


@runtime_checkable
class ApplicationService(Protocol):
    """Synchronous resource owned by one Stream Dock application.

    Services start in declaration order before the runtime connects and stop in
    reverse order after the runtime finishes. A service whose ``start()``
    raises is responsible for rolling back its own partial initialization.
    """

    @abstractmethod
    def start(self) -> None:
        """Allocate resources required by action callbacks."""

        ...

    @abstractmethod
    def stop(self) -> None:
        """Release resources; implementations should be idempotent."""

        ...


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    """Canonical dependencies shared by one Stream Dock application.

    The context is created before action-dependency and service factories run.
    Its :attr:`global_settings` object is the same runtime-owned facade exposed
    by :class:`StreamDockApplication`.
    """

    stream_dock: StreamDockSender
    global_settings: GlobalSettings


ApplicationServiceFactory: TypeAlias = Callable[[ApplicationContext], ApplicationService]


__all__ = [
    "ActionFactory",
    "ApplicationContext",
    "ApplicationService",
    "ApplicationServiceFactory",
    "GlobalSettings",
    "PluginHooks",
    "RuntimeLifecycle",
    "StreamDockSender",
]

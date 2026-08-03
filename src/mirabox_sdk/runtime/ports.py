"""Stable application-facing ports for the runtime dispatcher."""

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from .._next.runtime.ports import ActionFactory, PluginHooks, RuntimeLifecycle
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


__all__ = [
    "ActionFactory",
    "ApplicationService",
    "PluginHooks",
    "RuntimeLifecycle",
    "StreamDockSender",
]

"""Stable application-facing ports for the runtime dispatcher."""

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

from .._next.runtime.ports import ActionFactory, PluginHooks, RuntimeLifecycle
from ..global_settings import GlobalSettings
from ..protocols import StreamDockSender


@runtime_checkable
class SessionReadiness(Protocol):
    """Read-only signal for the one-time Stream Dock session initialization.

    A session becomes ready after the transport is connected, the registration
    command and initial global-settings request have completed, and the
    outbound writer can service commands. ``wait()`` must not be called from
    :meth:`ApplicationService.start`, because that method runs before the
    runtime starts the session. Services can wait from work they start there.
    """

    @property
    @abstractmethod
    def ready(self) -> bool:
        """Return whether mandatory session initialization succeeded."""

        ...

    @property
    @abstractmethod
    def terminal(self) -> bool:
        """Return whether the session can no longer become ready."""

        ...

    @property
    @abstractmethod
    def failure(self) -> Exception | None:
        """Return the fatal initialization failure, when one occurred."""

        ...

    @abstractmethod
    def wait(self, timeout: float | None = None) -> bool:
        """Wait for readiness or terminal closure and return readiness state."""

        ...


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
    by :class:`StreamDockApplication`. ``session_readiness`` is one shared
    read-only signal for work that requires a connected, initialized session.
    """

    stream_dock: StreamDockSender
    global_settings: GlobalSettings
    session_readiness: SessionReadiness


ApplicationServiceFactory: TypeAlias = Callable[[ApplicationContext], ApplicationService]


__all__ = [
    "ActionFactory",
    "ApplicationContext",
    "ApplicationService",
    "ApplicationServiceFactory",
    "GlobalSettings",
    "PluginHooks",
    "RuntimeLifecycle",
    "SessionReadiness",
    "StreamDockSender",
]

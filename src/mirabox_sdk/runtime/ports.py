"""Stable application-facing ports for the runtime dispatcher."""

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias, TypeVar, runtime_checkable

from .._internal.messaging.inbound import InboundOverflowPolicy
from .._internal.runtime.plugin import Plugin
from .._internal.runtime.ports import PluginHooks, RuntimeLifecycle
from ..action import Action
from ..codecs import JsonCodec
from ..global_settings import GlobalSettings
from ..json_types import JsonObject
from ..json_types import JsonValue as JsonValue  # Resolve recursive JSON type hints.
from ..protocols import StreamDockActionDependencies, StreamDockSender
from .metrics import StreamDockRuntimeMetrics
from .shutdown import StopSignal

GlobalSettingsT = TypeVar("GlobalSettingsT")
DependenciesT = TypeVar(
    "DependenciesT",
    bound=StreamDockActionDependencies,
    contravariant=True,
)


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
class ApplicationRuntime(Protocol):
    """Complete runtime contract consumed by :class:`StreamDockApplication`.

    ``RuntimeLifecycle`` intentionally remains a narrower contract for
    components that only need to run, close, or inspect metrics. It is not
    sufficient to construct an application because the application also
    exposes the canonical global-settings facade and its write operations.
    """

    @property
    @abstractmethod
    def global_settings(self) -> GlobalSettings:
        """Return the canonical runtime-owned plugin-wide settings facade."""

        ...

    @abstractmethod
    def run_forever(self) -> None:
        """Run until remote disconnect, close, or a fatal failure."""

        ...

    @abstractmethod
    def close(self) -> None:
        """Idempotently request graceful runtime shutdown."""

        ...

    @abstractmethod
    def metrics(self) -> StreamDockRuntimeMetrics:
        """Return an immutable aggregate runtime snapshot."""

        ...

    @abstractmethod
    def update_global_settings(self, update: Callable[[JsonObject], None]) -> None:
        """Persist one rollback-safe global-settings transaction."""

        ...

    @abstractmethod
    def set_global_settings(self, settings: JsonObject) -> None:
        """Persist raw plugin-wide settings."""

        ...

    @abstractmethod
    def set_typed_global_settings(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Encode and persist typed plugin-wide settings."""

        ...


@runtime_checkable
class DependencyAwareActionRegistry(Protocol[DependenciesT]):
    """Registry that builds actions with one typed shared dependency object."""

    @abstractmethod
    def create(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
        dependencies: DependenciesT,
    ) -> Action[Any, Any] | None:
        """Return an action for one visible context, or ``None`` if unknown."""

        ...


@runtime_checkable
class ApplicationService(Protocol):
    """Synchronous resource owned by one Stream Dock application.

    Services start in declaration order before the runtime connects and stop in
    reverse order after runtime callbacks and cleanup finish. Cleanup can be
    deferred past the shutdown deadline. A service whose ``start()``
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

    The context is created before plugin, action-dependency and service
    factories run.
    Its :attr:`global_settings` object is the same runtime-owned facade exposed
    by :class:`StreamDockApplication`. ``session_readiness`` is one shared
    read-only signal for work that requires a connected, initialized session.
    ``stop_signal`` requests cooperative cancellation before resources close.
    """

    stream_dock: StreamDockSender
    global_settings: GlobalSettings
    session_readiness: SessionReadiness
    stop_signal: StopSignal = field(default_factory=StopSignal)


ApplicationServiceFactory: TypeAlias = Callable[[ApplicationContext], ApplicationService]


__all__ = [
    "ApplicationRuntime",
    "ApplicationContext",
    "ApplicationService",
    "ApplicationServiceFactory",
    "DependencyAwareActionRegistry",
    "GlobalSettings",
    "InboundOverflowPolicy",
    "Plugin",
    "PluginHooks",
    "RuntimeLifecycle",
    "SessionReadiness",
    "StreamDockSender",
]

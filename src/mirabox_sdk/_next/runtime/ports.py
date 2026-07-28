"""Application-facing and internal ports for the runtime dispatcher."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ...commands import StreamDockCommand
from ...events import StreamDockEvent, UnknownStreamDockEvent, WillAppearEvent
from ...json_types import JsonObject
from ..messaging.models import CommandFuture
from .metrics import HandlerSchedulerMetrics, StreamDockRuntimeMetrics
from .models import DispatchResult


@runtime_checkable
class RuntimeAction(Protocol):
    """Minimum identity exposed by an application-owned action instance."""

    @property
    @abstractmethod
    def action(self) -> str:
        """Return the manifest action UUID."""

        ...

    @property
    @abstractmethod
    def context(self) -> str:
        """Return the opaque Stream Dock context."""

        ...


@runtime_checkable
class ActionFactory(Protocol):
    """Create one application action for a visible Stream Dock context."""

    @abstractmethod
    def create(
        self,
        action_uuid: str,
        context: str,
        initial_settings: JsonObject,
    ) -> RuntimeAction | None:
        """Return a new action, or ``None`` for an unknown action UUID."""

        ...


@runtime_checkable
class PluginHooks(Protocol):
    """Plugin-scope callbacks that are not action routes."""

    @abstractmethod
    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        """Observe one forward-compatible event exactly once."""

        ...


@runtime_checkable
class StreamDockSender(Protocol):
    """Narrow typed command port supplied to runtime collaborators."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None:
        """Submit a command and wait for its terminal result."""

        ...

    @abstractmethod
    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Submit a command and return its boundary completion handle."""

        ...


@runtime_checkable
class RuntimeLifecycle(Protocol):
    """Single-run lifecycle exposed by the runtime facade."""

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


@runtime_checkable
class DispatchCompletion(Protocol):
    """Read-only terminal result for scheduler-owned dispatch work."""

    @abstractmethod
    def done(self) -> bool:
        """Return whether dispatch ownership reached a terminal outcome."""

        ...

    @abstractmethod
    def result(self, timeout: float | None = None) -> DispatchResult:
        """Wait for and return the terminal dispatch result."""

        ...

    @abstractmethod
    def add_done_callback(
        self,
        callback: Callable[[DispatchCompletion], None],
    ) -> None:
        """Invoke ``callback`` once after terminal completion."""

        ...


@runtime_checkable
class HandlerScheduler(Protocol):
    """Bounded, replaceable scheduler for typed runtime events."""

    @abstractmethod
    def start(self) -> None:
        """Start scheduler-owned workers, if any."""

        ...

    @abstractmethod
    def submit(self, event: StreamDockEvent) -> DispatchCompletion:
        """Accept one event and return its terminal completion."""

        ...

    @abstractmethod
    def stop_accepting(self) -> None:
        """Reject new events while retaining ownership of accepted work."""

        ...

    @abstractmethod
    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait for accepted work to reach terminal outcomes."""

        ...

    @abstractmethod
    def stop(self, *, timeout: float | None = None) -> bool:
        """Stop scheduler-owned workers within the optional timeout."""

        ...

    @abstractmethod
    def metrics(self) -> HandlerSchedulerMetrics:
        """Return an immutable scheduler snapshot."""

        ...


@runtime_checkable
class ActionContextManager(Protocol):
    """Own application actions addressed by opaque Stream Dock contexts."""

    @abstractmethod
    def create(self, event: WillAppearEvent) -> RuntimeAction | None:
        """Create and retain an action for an appearance event."""

        ...

    @abstractmethod
    def get(self, context: str) -> RuntimeAction | None:
        """Return the current action for ``context``, if any."""

        ...

    @abstractmethod
    def remove(self, context: str) -> RuntimeAction | None:
        """Remove and return the current action for ``context``, if any."""

        ...

    @abstractmethod
    def snapshot(self) -> tuple[RuntimeAction, ...]:
        """Return an immutable action snapshot."""

        ...

    @abstractmethod
    def clear(self) -> tuple[RuntimeAction, ...]:
        """Atomically remove and return all retained actions."""

        ...

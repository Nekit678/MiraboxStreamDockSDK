"""Public connection protocols for the typed MiraBox SDK layer."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol

from .commands import StreamDockCommand
from .events import StreamDockEvent


class StreamDockSender(Protocol):
    """Minimal outbound command channel required by action helpers."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None:
        """Serialize and send one command to Stream Dock.

        Args:
            command: Typed command to transmit.
        """

        ...


class StreamDockListener(Protocol):
    """Callback boundary receiving connection and protocol events."""

    @abstractmethod
    def on_stream_dock_connected(self) -> None:
        """Handle an opened WebSocket before normal events are delivered."""

        ...

    @abstractmethod
    def on_stream_dock_event(self, event: StreamDockEvent) -> None:
        """Handle one parsed known or forward-compatible unknown event.

        Args:
            event: Typed event produced by the connection parser.
        """

        ...


class StreamDockConnection(StreamDockSender, Protocol):
    """Lifecycle and messaging boundary for a Stream Dock connection.

    A plugin runtime installs one listener, then calls :meth:`run_forever`.
    Implementations are responsible for delivering parsed incoming events and
    accepting typed outgoing commands.
    """

    @abstractmethod
    def set_listener(self, listener: StreamDockListener) -> None:
        """Set the listener that receives connection callbacks."""

        ...

    @abstractmethod
    def run_forever(self) -> None:
        """Process WebSocket traffic until the connection closes."""

        ...

    @abstractmethod
    def close(self) -> None:
        """Request connection shutdown and release transport resources."""

        ...


class StreamDockActionDependencies(Protocol):
    """Minimum dependency container required by :class:`Action`.

    Applications commonly implement this protocol with a frozen dataclass and
    add any repositories, clients, or services required by their actions.
    """

    @property
    @abstractmethod
    def stream_dock(self) -> StreamDockSender:
        """Return the outbound command channel used by action helpers."""

        ...


class LifecycleService(Protocol):
    """Plugin-owned service managed with the Stream Dock runtime.

    Services start in declaration order before the connection loop and stop in
    reverse order during shutdown. Only successfully started services are
    stopped.
    """

    @abstractmethod
    def start(self) -> None:
        """Allocate resources or start background work for the service."""

        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop background work and release resources; preferably idempotently."""

        ...


class PluginApplication(Protocol):
    """Executable application lifecycle consumed by :func:`run_plugin_cli`."""

    @abstractmethod
    def run(self) -> None:
        """Start the application and block until normal completion."""

        ...

    @abstractmethod
    def stop(self) -> None:
        """Release application resources after completion or failure."""

        ...

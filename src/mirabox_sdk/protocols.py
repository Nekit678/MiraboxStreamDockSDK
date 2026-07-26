"""Public connection protocols for the typed MiraBox SDK layer."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol

from .commands import StreamDockCommand
from .events import StreamDockEvent
from .outbound import CommandFuture


class StreamDockSender(Protocol):
    """Minimal thread-safe outbound command channel required by helpers."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None:
        """Submit one command to the connection's outbound writer.

        Calls from application threads may overlap. The relative FIFO order of
        overlapping calls is whichever order the command bus accepts them.

        Args:
            command: Typed command to transmit.
        """

        ...

    @abstractmethod
    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Submit one command without waiting for serialization or transport.

        Queue-capacity and shutdown rejections are raised before this method
        returns. Writer-side failures are available through the returned
        completion handle.

        Args:
            command: Typed command to transmit.

        Returns:
            Completion handle for the accepted command.
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
    accepting typed outgoing commands. ``set_listener()`` and ``run_forever()``
    belong to the lifecycle thread; ``send()``, ``send_async()``, and
    ``close()`` may be called concurrently from application or callback
    threads.
    """

    @abstractmethod
    def set_listener(self, listener: StreamDockListener) -> None:
        """Set the listener before starting the connection loop."""

        ...

    @abstractmethod
    def run_forever(self) -> None:
        """Process WebSocket traffic once, blocking the lifecycle thread."""

        ...

    @abstractmethod
    def close(self) -> None:
        """Idempotently request shutdown from an application or callback thread."""

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

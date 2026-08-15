"""Public connection protocols for the typed MiraBox SDK layer."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol

from .commands import StreamDockCommand
from .completion import CommandFuture


class StreamDockSender(Protocol):
    """Minimal thread-safe outbound command channel required by helpers."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None:
        """Submit one command to the connection's outbound writer.

        Calls from application threads may overlap. The relative FIFO order of
        overlapping calls is whichever order the command bus accepts them.
        Before the application starts its outbound writer, this raises
        :class:`OutboundCommandBusNotReadyError` instead of waiting for a
        consumer that does not exist.

        Args:
            command: Typed command to transmit.
        """

        ...

    @abstractmethod
    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Submit one command without waiting for serialization or transport.

        Queue-capacity, lifecycle, and shutdown rejections are raised before
        this method returns. Writer-side failures are available through the
        returned completion handle. Submission before the outbound writer has
        started raises :class:`OutboundCommandBusNotReadyError`.

        Args:
            command: Typed command to transmit.

        Returns:
            Completion handle for the accepted command.
        """

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

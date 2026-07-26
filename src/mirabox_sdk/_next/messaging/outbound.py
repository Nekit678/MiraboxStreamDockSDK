"""Typed outbound command ports and completion handles."""

from __future__ import annotations

from abc import abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...commands import StreamDockCommand


class CommandFuture:
    """Read-only completion handle for one accepted outbound command."""

    __slots__ = ("_future",)

    def __init__(self) -> None:
        self._future: Future[None] = Future()

    def done(self) -> bool:
        """Return whether command processing reached a terminal state."""

        return self._future.done()

    def result(self, timeout: float | None = None) -> None:
        """Wait for completion and re-raise the recorded command failure."""

        return self._future.result(timeout)

    def exception(self, timeout: float | None = None) -> Exception | None:
        """Wait for completion and return the recorded failure, if any."""

        return self._future.exception(timeout)

    def _finish(self, *, error: Exception | None = None) -> None:
        """Complete the command once for internal boundary components."""

        if error is None:
            self._future.set_result(None)
        else:
            self._future.set_exception(error)


@dataclass(slots=True)
class CommandSubmission:
    """One accepted typed command and its completion handle."""

    command: StreamDockCommand
    completion: CommandFuture


@runtime_checkable
class OutboundCommandSource(Protocol):
    """Source of accepted command submissions for the command writer."""

    @abstractmethod
    def receive(self) -> CommandSubmission:
        """Return the next accepted command submission in FIFO order."""

        ...


@runtime_checkable
class OutboundCommandSink(Protocol):
    """Typed command port exposed to the next SDK layer."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None:
        """Submit a command and wait for its terminal result."""

        ...

    @abstractmethod
    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Submit a command and return its completion handle."""

        ...

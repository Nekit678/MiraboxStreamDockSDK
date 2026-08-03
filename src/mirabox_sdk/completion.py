"""Canonical outbound command completion and submission errors."""

from __future__ import annotations

from concurrent.futures import Future


class OutboundCommandBusError(RuntimeError):
    """Base error raised while submitting an outbound command."""


class OutboundQueueFullError(OutboundCommandBusError):
    """Raised when the bounded outbound queue cannot accept a command."""


class OutboundCommandBusClosedError(OutboundCommandBusError):
    """Raised when a command is submitted after outbound shutdown begins."""


class CommandFuture:
    """Read-only completion handle for one accepted outbound command."""

    __slots__ = ("_future",)

    def __init__(self) -> None:
        self._future: Future[None] = Future()

    def _share(self) -> CommandFuture:
        """Return a distinct handle backed by this completion state."""

        shared = type(self).__new__(type(self))
        shared._future = self._future
        return shared

    def done(self) -> bool:
        """Return whether command processing reached a terminal state."""

        return self._future.done()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait up to ``timeout`` seconds and report terminal completion."""

        try:
            self._future.exception(timeout)
        except TimeoutError:
            return False
        return True

    def result(self, timeout: float | None = None) -> None:
        """Wait for completion and re-raise the recorded command failure."""

        try:
            return self._future.result(timeout)
        except TimeoutError as exc:
            raise TimeoutError("Outbound command did not complete before the timeout") from exc

    def exception(self, timeout: float | None = None) -> Exception | None:
        """Wait for completion and return the recorded failure, if any."""

        try:
            return self._future.exception(timeout)
        except TimeoutError as exc:
            raise TimeoutError("Outbound command did not complete before the timeout") from exc

    def _finish(self, *, error: Exception | None = None) -> None:
        """Complete the command once for runtime-owned boundary components."""

        if error is None:
            self._future.set_result(None)
        else:
            self._future.set_exception(error)


__all__ = [
    "CommandFuture",
    "OutboundCommandBusClosedError",
    "OutboundCommandBusError",
    "OutboundQueueFullError",
]

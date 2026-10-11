"""Canonical outbound command completion and submission errors."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError
from math import isfinite
from threading import Lock
from time import monotonic
from typing import cast


class OutboundCommandBusError(RuntimeError):
    """Base error raised while submitting an outbound command."""


class OutboundQueueFullError(OutboundCommandBusError):
    """Raised when the bounded outbound queue cannot accept a command."""


class OutboundCommandBusClosedError(OutboundCommandBusError):
    """Raised when a command is submitted after outbound shutdown begins."""


class OutboundCommandBusNotReadyError(OutboundCommandBusError):
    """Raised when a command is submitted before the outbound writer starts."""


class OutboundCommandDeadlineExceededError(TimeoutError, OutboundCommandBusError):
    """The accepted command exhausted its deadline, including transport I/O."""


class OutboundCommandCancelledError(OutboundCommandBusError):
    """The accepted command was cancelled before transport send began."""


def _validate_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    return float(deadline)


class _CommandControl:
    def __init__(self, deadline: float | None) -> None:
        self.lock = Lock()
        self.deadline = deadline
        self.terminal = False
        self.sending = False
        self.abort: Callable[[], None] | None = None
        self.cancel: Callable[[], bool] | None = None


class CommandFuture:
    """Completion and pre-send cancellation for an accepted outbound command."""

    __slots__ = ("_future", "_control")

    def __init__(self, *, deadline: float | None = None) -> None:
        self._future: Future[None] = Future()
        self._control = _CommandControl(_validate_deadline(deadline))

    def _share(self) -> CommandFuture:
        """Return a distinct handle backed by this completion state."""

        shared = type(self).__new__(type(self))
        shared._future = self._future
        shared._control = self._control
        return shared

    def cancel(self) -> bool:
        """Cancel before transport send; return False once sending or complete.

        Coalesced handles cancel their shared physical command. Persistence
        handles delegate cancellation to their underlying command.
        """

        with self._control.lock:
            if self._control.terminal or self._control.sending:
                return False
            delegate = self._control.cancel
            if delegate is None:
                self._control.terminal = True
        if delegate is not None:
            return delegate()
        self._future.set_exception(
            OutboundCommandCancelledError("Outbound command cancelled before transport send")
        )
        return True

    def cancelled(self) -> bool:
        """Return whether explicit pre-send cancellation completed the command."""

        return self.done() and isinstance(self.exception(timeout=0), OutboundCommandCancelledError)

    def _delegate_cancel(self, command: CommandFuture) -> None:
        self._control.cancel = command.cancel

    def _start_send(self, abort: Callable[[], None]) -> bool:
        """Atomically exclude cancellation once transport owns the send."""

        with self._control.lock:
            if self._control.terminal:
                return False
            deadline = self._control.deadline
            if deadline is None or monotonic() < deadline:
                self._control.sending = True
                self._control.abort = abort
                return True
        self._expire()
        return False

    def _expire(self) -> None:
        try:
            self._finish(
                error=OutboundCommandDeadlineExceededError("Outbound command deadline exceeded")
            )
        except InvalidStateError:
            pass

    def done(self) -> bool:
        """Return whether command processing reached a terminal state."""

        return self._future.done()

    def add_done_callback(self, callback: Callable[[CommandFuture], None]) -> None:
        """Observe completion without blocking a callback worker.

        The callback receives this handle and runs on the completing thread, or
        immediately on the registering thread if already complete. Keep it short;
        use ``exception(timeout=0)`` to inspect failures without waiting.
        """

        if not callable(callback):
            raise TypeError("callback must be callable")
        self._future.add_done_callback(lambda _future: callback(self))

    def wait(self, timeout: float | None = None) -> bool:
        """Wait up to ``timeout`` seconds and report terminal completion."""

        try:
            self._future.exception(timeout)
        except TimeoutError:
            return False
        return True

    def result(self, timeout: float | None = None) -> None:
        """Wait for completion and re-raise the recorded command failure."""

        # Wait without raising recorded failures, which may themselves be timeouts.
        self.exception(timeout)
        return self._future.result(timeout=0)

    def exception(self, timeout: float | None = None) -> Exception | None:
        """Wait for completion and return the recorded failure, if any."""

        try:
            # Only _finish() writes failures, and it accepts Exception instances.
            return cast(Exception | None, self._future.exception(timeout))
        except TimeoutError as exc:
            raise TimeoutError("Outbound command did not complete before the timeout") from exc

    def _finish(self, *, error: Exception | None = None) -> None:
        """Complete the command once for runtime-owned boundary components."""

        with self._control.lock:
            if self._control.terminal:
                raise InvalidStateError("CommandFuture is already finished")
            deadline = self._control.deadline
            if deadline is not None and monotonic() >= deadline:
                error = OutboundCommandDeadlineExceededError("Outbound command deadline exceeded")
            self._control.terminal = True
            abort = (
                self._control.abort
                if isinstance(error, OutboundCommandDeadlineExceededError)
                else None
            )
            self._control.abort = None
            self._control.cancel = None
        # Interrupt the socket before publishing failure or invoking observers.
        if abort is not None:
            abort()
        if error is None:
            self._future.set_result(None)
        else:
            self._future.set_exception(error)


__all__ = [
    "CommandFuture",
    "OutboundCommandBusClosedError",
    "OutboundCommandBusError",
    "OutboundCommandBusNotReadyError",
    "OutboundCommandCancelledError",
    "OutboundCommandDeadlineExceededError",
    "OutboundQueueFullError",
]

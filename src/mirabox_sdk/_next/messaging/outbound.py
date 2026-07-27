"""Bounded semantic queue for typed outbound Stream Dock commands."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
from threading import Condition
from time import monotonic

from ...commands import (
    SetGlobalSettingsCommand,
    SetImageCommand,
    SetSettingsCommand,
    SetStateCommand,
    SetTitleCommand,
    StreamDockCommand,
)
from .metrics import OutboundCommandQueueMetrics
from .models import CommandFuture, CommandSubmission
from .ports import OutboundCommandQueueControl, OutboundCommandSink, OutboundCommandSource


class OutboundCommandQueueError(RuntimeError):
    """Base error for typed outbound queue submission failures."""


class OutboundQueueFullError(OutboundCommandQueueError):
    """Report synchronous rejection by a full command queue."""


class OutboundCommandQueueClosedError(OutboundCommandQueueError):
    """Report submission after typed outbound shutdown began."""


@dataclass(slots=True)
class _QueuedCommand:
    command: StreamDockCommand
    completions: list[CommandFuture]


class _CoalescedCommandFuture(CommandFuture):
    """Complete every future represented by one coalesced wire command."""

    __slots__ = ("_completions",)

    def __init__(self, completions: tuple[CommandFuture, ...]) -> None:
        super().__init__()
        self._completions = completions

    def _finish(self, *, error: Exception | None = None) -> None:
        super()._finish(error=error)
        for completion in self._completions:
            completion._finish(error=error)


class OutboundCommandQueue(
    OutboundCommandSource,
    OutboundCommandSink,
    OutboundCommandQueueControl,
):
    """Accept commands without I/O and expose one FIFO writer source."""

    def __init__(self, queue_limit: int, *, coalesce_commands: bool = False) -> None:
        _validate_queue_limit(queue_limit)
        if type(coalesce_commands) is not bool:
            raise ValueError("coalesce_commands must be a boolean")

        self._queue_limit = queue_limit
        self._coalesce_commands = coalesce_commands
        self._condition = Condition()
        self._queue: deque[_QueuedCommand] = deque()
        self._accepting = True

        self._peak_depth = 0
        self._submitted = 0
        self._enqueued = 0
        self._coalesced = 0
        self._dequeued = 0
        self._rejected_full = 0
        self._rejected_after_shutdown = 0
        self._discarded_during_shutdown = 0

    def send(self, command: StreamDockCommand) -> None:
        """Submit a command and wait for writer-side terminal completion."""

        self.send_async(command).result()

    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Accept a typed command or synchronously report queue rejection."""

        if not isinstance(command, StreamDockCommand):
            raise TypeError("command must be StreamDockCommand")

        completion = CommandFuture()
        with self._condition:
            self._submitted += 1
            if not self._accepting:
                self._rejected_after_shutdown += 1
                raise OutboundCommandQueueClosedError(
                    "Outbound command queue is no longer accepting commands"
                )
            if self._coalesce(command, completion):
                self._coalesced += 1
                self._condition.notify_all()
                return completion
            if len(self._queue) >= self._queue_limit:
                self._rejected_full += 1
                raise OutboundQueueFullError(
                    f"Outbound command queue is full (limit={self._queue_limit})"
                )

            self._queue.append(_QueuedCommand(command, [completion]))
            self._enqueued += 1
            self._peak_depth = max(self._peak_depth, len(self._queue))
            self._condition.notify_all()
            return completion

    def receive(self, *, timeout: float | None = None) -> CommandSubmission:
        """Return the next physical command submission in FIFO order."""

        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout

        with self._condition:
            while not self._queue:
                if not self._accepting:
                    raise OutboundCommandQueueClosedError("Outbound command queue is closed")
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Timed out waiting for outbound command queue")
                self._condition.wait(remaining)

            queued = self._queue.popleft()
            self._dequeued += 1
            self._condition.notify_all()

        if len(queued.completions) == 1:
            completion = queued.completions[0]
        else:
            completion = _CoalescedCommandFuture(tuple(queued.completions))
        return CommandSubmission(queued.command, completion)

    def stop_accepting(self) -> None:
        """Reject new commands while allowing accepted commands to drain."""

        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until every queued command has been received by a writer."""

        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while self._queue:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop submissions and fail commands left queued after timeout."""

        timeout = _validate_timeout(timeout)
        self.stop_accepting()
        if self.drain(timeout=timeout):
            return True

        discarded: tuple[CommandFuture, ...]
        with self._condition:
            error = OutboundCommandQueueClosedError(
                "Outbound command was discarded during shutdown"
            )
            self._discarded_during_shutdown += len(self._queue)
            completions = []
            while self._queue:
                queued = self._queue.popleft()
                completions.extend(queued.completions)
            discarded = tuple(completions)
            self._condition.notify_all()

        for completion in discarded:
            completion._finish(error=error)
        return False

    def metrics(self) -> OutboundCommandQueueMetrics:
        """Return an atomic immutable metrics snapshot."""

        with self._condition:
            return OutboundCommandQueueMetrics(
                queue_limit=self._queue_limit,
                current_depth=len(self._queue),
                peak_depth=self._peak_depth,
                submitted=self._submitted,
                enqueued=self._enqueued,
                coalesced=self._coalesced,
                dequeued=self._dequeued,
                rejected_full=self._rejected_full,
                rejected_after_shutdown=self._rejected_after_shutdown,
                discarded_during_shutdown=self._discarded_during_shutdown,
            )

    def _coalesce(self, command: StreamDockCommand, completion: CommandFuture) -> bool:
        if not self._coalesce_commands or not self._queue:
            return False

        queued = self._queue[-1]
        key = self._coalescing_key(command)
        if key is None or key != self._coalescing_key(queued.command):
            return False
        queued.command = command
        queued.completions.append(completion)
        return True

    @staticmethod
    def _coalescing_key(command: StreamDockCommand) -> tuple[object, ...] | None:
        if type(command) is SetStateCommand:
            return (SetStateCommand, command.context)
        if type(command) is SetTitleCommand:
            return (SetTitleCommand, command.context, command.target, command.state)
        if type(command) is SetImageCommand:
            return (SetImageCommand, command.context, command.target, command.state)
        if type(command) is SetSettingsCommand:
            return (SetSettingsCommand, command.context)
        if type(command) is SetGlobalSettingsCommand:
            return (SetGlobalSettingsCommand, command.context)
        return None


def _validate_queue_limit(queue_limit: int) -> None:
    if type(queue_limit) is not int or queue_limit <= 0:
        raise ValueError("queue_limit must be a positive integer")


def _validate_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("timeout must be a non-negative number or None")
    return float(timeout)

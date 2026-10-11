"""Bounded semantic queue for typed outbound Stream Dock commands."""

from __future__ import annotations

from collections import deque
from concurrent.futures import InvalidStateError
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from threading import TIMEOUT_MAX, Condition, Lock, Thread
from time import monotonic

from ...commands import (
    SetGlobalSettingsCommand,
    SetImageCommand,
    SetSettingsCommand,
    SetStateCommand,
    SetTitleCommand,
    StreamDockCommand,
)
from ...completion import (
    OutboundCommandBusClosedError,
    OutboundCommandBusError,
    OutboundCommandBusNotReadyError,
    OutboundQueueFullError,
    _check_blocking_allowed,
    _validate_deadline,
)
from ..transport.buffer_limits import DEFAULT_QUEUE_BYTE_LIMIT, retained_size, validate_byte_limit
from .metrics import OutboundCommandQueueMetrics
from .models import CommandFuture, CommandSubmission
from .ports import OutboundCommandQueueControl, OutboundCommandSink, OutboundCommandSource

OutboundCommandQueueError = OutboundCommandBusError
OutboundCommandQueueClosedError = OutboundCommandBusClosedError


class _CommandSinkLifecycleState(Enum):
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    CLOSED = "closed"


class WriterReadyOutboundCommandSink(OutboundCommandSink):
    """Expose a command sink only while its writer can consume submissions."""

    def __init__(self, sink: OutboundCommandSink) -> None:
        if not isinstance(sink, OutboundCommandSink):
            raise TypeError("sink must implement OutboundCommandSink")
        self._sink = sink
        self._lock = Lock()
        self._state = _CommandSinkLifecycleState.CREATED

    def send(self, command: StreamDockCommand, *, deadline: float | None = None) -> None:
        """Submit a command and wait for its terminal result when writer-ready."""

        _check_blocking_allowed()
        self.send_async(command, deadline=deadline).result()

    def send_async(
        self, command: StreamDockCommand, *, deadline: float | None = None
    ) -> CommandFuture:
        """Submit a command only after the boundary starts its command writer."""

        if not isinstance(command, StreamDockCommand):
            raise TypeError("command must be StreamDockCommand")
        with self._lock:
            state = self._state
        if state in (
            _CommandSinkLifecycleState.CREATED,
            _CommandSinkLifecycleState.STARTING,
        ):
            raise OutboundCommandBusNotReadyError(
                "Outbound command writer is not ready; "
                "run the application before submitting commands"
            )
        if state in (
            _CommandSinkLifecycleState.STOPPING,
            _CommandSinkLifecycleState.CLOSED,
        ):
            return self._submit(command, deadline)
        return self._submit(command, deadline)

    def _submit(self, command: StreamDockCommand, deadline: float | None) -> CommandFuture:
        if deadline is None:
            return self._sink.send_async(command)
        return self._sink.send_async(command, deadline=deadline)

    def begin_starting(self) -> None:
        """Mark the boundary startup phase before workers are started."""

        with self._lock:
            if self._state is _CommandSinkLifecycleState.CREATED:
                self._state = _CommandSinkLifecycleState.STARTING

    def mark_ready(self) -> None:
        """Allow submissions after the command writer has successfully started."""

        with self._lock:
            if self._state is _CommandSinkLifecycleState.STARTING:
                self._state = _CommandSinkLifecycleState.READY

    def begin_stopping(self) -> None:
        """Reject new submissions while boundary shutdown is in progress."""

        with self._lock:
            if self._state is not _CommandSinkLifecycleState.CLOSED:
                self._state = _CommandSinkLifecycleState.STOPPING

    def mark_closed(self) -> None:
        """Record terminal closure after every queue has been shut down."""

        with self._lock:
            self._state = _CommandSinkLifecycleState.CLOSED


@dataclass(slots=True, eq=False)
class _QueuedCommand:
    command: StreamDockCommand
    completion: CommandFuture
    size: int
    deadline: float | None


class OutboundCommandQueue(
    OutboundCommandSource,
    OutboundCommandSink,
    OutboundCommandQueueControl,
):
    """Accept commands without I/O and expose one FIFO writer source."""

    def __init__(
        self,
        queue_limit: int,
        *,
        coalesce_commands: bool = False,
        byte_limit: int = DEFAULT_QUEUE_BYTE_LIMIT,
    ) -> None:
        _validate_queue_limit(queue_limit)
        validate_byte_limit("byte_limit", byte_limit)
        if type(coalesce_commands) is not bool:
            raise ValueError("coalesce_commands must be a boolean")

        self._queue_limit = queue_limit
        self._coalesce_commands = coalesce_commands
        self._byte_limit = byte_limit
        self._condition = Condition()
        self._queue: deque[_QueuedCommand] = deque()
        self._queued: dict[CommandFuture, _QueuedCommand] = {}
        self._deadlines: dict[CommandFuture, float] = {}
        self._deadline_thread: Thread | None = None
        self._accepting = True

        self._peak_depth = 0
        self._submitted = 0
        self._enqueued = 0
        self._coalesced = 0
        self._dequeued = 0
        self._rejected_full = 0
        self._rejected_after_shutdown = 0
        self._discarded_during_shutdown = 0
        self._current_bytes = 0
        self._peak_bytes = 0
        self._rejected_oversized = 0

    def send(self, command: StreamDockCommand, *, deadline: float | None = None) -> None:
        """Submit a command and wait for writer-side terminal completion."""

        _check_blocking_allowed()
        self.send_async(command, deadline=deadline).result()

    def send_async(
        self, command: StreamDockCommand, *, deadline: float | None = None
    ) -> CommandFuture:
        """Accept a typed command or synchronously report queue rejection."""

        if not isinstance(command, StreamDockCommand):
            raise TypeError("command must be StreamDockCommand")
        deadline = _validate_deadline(deadline)

        with self._condition:
            self._submitted += 1
            if not self._accepting:
                self._rejected_after_shutdown += 1
                raise OutboundCommandQueueClosedError(
                    "Outbound command queue is no longer accepting commands"
                )
            size = retained_size(command, self._byte_limit)
            if size > self._byte_limit:
                self._rejected_oversized += 1
                raise OutboundQueueFullError(
                    f"Outbound command exceeds byte limit (byte_limit={self._byte_limit})"
                )
            completion = self._coalesce(command, size) if deadline is None else None
            if completion is not None:
                self._coalesced += 1
                self._condition.notify_all()
                return completion
            if (
                len(self._queue) >= self._queue_limit
                or self._current_bytes + size > self._byte_limit
            ):
                self._rejected_full += 1
                raise OutboundQueueFullError(
                    f"Outbound command queue is full "
                    f"(limit={self._queue_limit}, byte_limit={self._byte_limit})"
                )

            completion = CommandFuture(deadline=deadline)
            queued = _QueuedCommand(command, completion, size, deadline)
            self._queue.append(queued)
            self._queued[completion] = queued
            completion.add_done_callback(self._command_finished)
            if deadline is not None:
                self._deadlines[completion] = deadline
                if self._deadline_thread is None:
                    self._deadline_thread = Thread(
                        target=self._watch_deadlines,
                        name="mirabox-command-deadlines",
                        daemon=True,
                    )
                    try:
                        self._deadline_thread.start()
                    except BaseException:
                        self._deadline_thread = None
                        self._deadlines.pop(completion, None)
                        self._queued.pop(completion, None)
                        self._queue.remove(queued)
                        raise
            self._current_bytes += size
            self._peak_bytes = max(self._peak_bytes, self._current_bytes)
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
            self._queued.pop(queued.completion, None)
            self._current_bytes -= queued.size
            self._dequeued += 1
            self._condition.notify_all()

            return CommandSubmission(queued.command, queued.completion)

    def _command_finished(self, completion: CommandFuture) -> None:
        with self._condition:
            queued = self._queued.pop(completion, None)
            if queued is not None:
                self._queue.remove(queued)
                self._current_bytes -= queued.size
            self._deadlines.pop(completion, None)
            self._condition.notify_all()

    def _watch_deadlines(self) -> None:
        while True:
            with self._condition:
                if not self._deadlines:
                    self._deadline_thread = None
                    return
                completion, deadline = min(self._deadlines.items(), key=lambda item: item[1])
                remaining = deadline - monotonic()
                if remaining > 0:
                    self._condition.wait(min(remaining, TIMEOUT_MAX))
                    continue
                self._deadlines.pop(completion, None)
            # Completion callbacks may submit commands; never run under the queue lock.
            completion._expire()

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
                self._queued.pop(queued.completion, None)
                completions.append(queued.completion)
            discarded = tuple(completions)
            self._current_bytes = 0
            self._condition.notify_all()

        for completion in discarded:
            try:
                completion._finish(error=error)
            except InvalidStateError:
                pass
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
                byte_limit=self._byte_limit,
                current_bytes=self._current_bytes,
                peak_bytes=self._peak_bytes,
                rejected_oversized=self._rejected_oversized,
            )

    def _coalesce(self, command: StreamDockCommand, size: int) -> CommandFuture | None:
        if not self._coalesce_commands or not self._queue:
            return None

        queued = self._queue[-1]
        if queued.deadline is not None:
            return None
        key = self._coalescing_key(command)
        if key is None or key != self._coalescing_key(queued.command):
            return None
        replacement_bytes = self._current_bytes - queued.size + size
        if replacement_bytes > self._byte_limit:
            return None
        with queued.completion._control.lock:
            if queued.completion._control.terminal:
                return None
            queued.command = command
            queued.size = size
            self._current_bytes = replacement_bytes
            self._peak_bytes = max(self._peak_bytes, self._current_bytes)
            return queued.completion._share()

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

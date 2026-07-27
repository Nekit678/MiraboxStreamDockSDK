"""Bounded single-writer dispatch for outbound Stream Dock commands."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Event, Thread, current_thread

from .commands import (
    SetGlobalSettingsCommand,
    SetImageCommand,
    SetSettingsCommand,
    SetStateCommand,
    SetTitleCommand,
    StreamDockCommand,
)

logger = logging.getLogger(__name__)


class OutboundCommandBusError(RuntimeError):
    """Base error raised while submitting a command to the outbound bus."""


class OutboundQueueFullError(OutboundCommandBusError):
    """Raised when the bounded outbound queue cannot accept another command."""


class OutboundCommandBusClosedError(OutboundCommandBusError):
    """Raised when a command is submitted after outbound shutdown begins."""


class _CommandCompletionState:
    """Completion state shared by handles for one coalesced wire command."""

    __slots__ = ("completed", "error")

    def __init__(self) -> None:
        self.completed = Event()
        self.error: Exception | None = None


class CommandFuture:
    """Read-only completion handle returned for an accepted outbound command.

    Queue-capacity and shutdown rejections are raised by ``send_async()``
    before a future is returned. Serialization and transport failures happen
    on the writer thread and are re-raised by :meth:`result`.
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state = _CommandCompletionState()

    def _share(self) -> CommandFuture:
        """Return a distinct handle backed by this completion state."""

        shared = type(self).__new__(type(self))
        shared._state = self._state
        return shared

    def done(self) -> bool:
        """Return whether serialization and transport processing has finished."""

        return self._state.completed.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait up to ``timeout`` seconds and return whether the command finished."""

        return self._state.completed.wait(timeout)

    def result(self, timeout: float | None = None) -> None:
        """Wait for completion and re-raise any writer-side failure.

        Raises:
            TimeoutError: If the command is still pending after ``timeout``.
            Exception: The serialization, transport, or shutdown error recorded
                by the outbound writer.
        """

        if not self.wait(timeout):
            raise TimeoutError("Outbound command did not complete before the timeout")
        if self._state.error is not None:
            raise self._state.error

    def exception(self, timeout: float | None = None) -> Exception | None:
        """Wait for completion and return the recorded failure, if any."""

        if not self.wait(timeout):
            raise TimeoutError("Outbound command did not complete before the timeout")
        return self._state.error

    def _finish(self, *, error: Exception | None = None) -> None:
        if self._state.completed.is_set():
            return
        self._state.error = error
        self._state.completed.set()


@dataclass(frozen=True, slots=True)
class OutboundQueueMetrics:
    """Thread-safe point-in-time counters for one outbound command queue."""

    queue_limit: int
    current_depth: int
    peak_depth: int
    submitted: int
    enqueued: int
    coalesced: int
    serialized: int
    sent: int
    rejected_full: int
    rejected_after_shutdown: int
    discarded_after_shutdown: int
    serialization_failures: int
    transport_failures: int

    @property
    def rejected(self) -> int:
        """Return the number of submissions rejected before queueing."""

        return self.rejected_full + self.rejected_after_shutdown

    @property
    def failures(self) -> int:
        """Return the number of serialization and transport failures."""

        return self.serialization_failures + self.transport_failures


@dataclass(slots=True)
class _QueuedCommand:
    command: StreamDockCommand
    completion: CommandFuture


class _OutboundCommandBus:
    """Serialize and write commands in order from one dedicated thread."""

    def __init__(
        self,
        *,
        queue_limit: int,
        coalesce_commands: bool,
        serialize: Callable[[StreamDockCommand], str],
        write: Callable[[str], object],
    ) -> None:
        self._queue_limit = queue_limit
        self._coalesce_commands = coalesce_commands
        self._serialize = serialize
        self._write = write
        self._condition = Condition()
        self._queue: deque[_QueuedCommand] = deque()
        self._in_flight: _QueuedCommand | None = None
        self._thread: Thread | None = None
        self._started = False
        self._accepting = True
        self._shutdown_requested = False

        self._peak_depth = 0
        self._submitted = 0
        self._enqueued = 0
        self._coalesced = 0
        self._serialized = 0
        self._sent = 0
        self._rejected_full = 0
        self._rejected_after_shutdown = 0
        self._discarded_after_shutdown = 0
        self._serialization_failures = 0
        self._transport_failures = 0

    def start(self) -> None:
        """Start the writer lazily and leave an existing writer unchanged."""

        with self._condition:
            self._start_locked()

    def submit(self, command: StreamDockCommand) -> None:
        """Queue a command and wait until the writer has processed it.

        Both serialization and transport errors are returned to the submitting
        thread without allowing that thread to encode or write WebSocket
        frames.
        """

        self.submit_async(command).result()

    def submit_async(self, command: StreamDockCommand) -> CommandFuture:
        """Queue a command and return without waiting for writer-side I/O.

        Queue-capacity and shutdown errors are raised synchronously because no
        command was accepted. Once accepted, serialization, transport, and
        shutdown errors are recorded on the returned future.
        """

        with self._condition:
            self._submitted += 1
            if not self._accepting:
                self._rejected_after_shutdown += 1
                raise OutboundCommandBusClosedError(
                    "Outbound command bus is no longer accepting commands"
                )
            if current_thread() is self._thread:
                raise OutboundCommandBusError(
                    "The outbound writer cannot submit a command to itself"
                )

            self._start_locked()
            submission = self._coalesce(command)
            if submission is not None:
                self._coalesced += 1
                self._condition.notify()
            else:
                if len(self._queue) >= self._queue_limit:
                    self._rejected_full += 1
                    raise OutboundQueueFullError(
                        f"Outbound command queue is full (limit={self._queue_limit})"
                    )
                submission = CommandFuture()
                self._queue.append(_QueuedCommand(command, submission))
                self._enqueued += 1
                self._peak_depth = max(self._peak_depth, len(self._queue))
                self._condition.notify()

        return submission

    def stop_accepting(self) -> None:
        """Reject new commands while allowing already queued work to drain."""

        with self._condition:
            self._accepting = False

    def shutdown(self, *, timeout: float | None) -> bool:
        """Stop after draining queued commands, optionally bounded by ``timeout``."""

        with self._condition:
            self._accepting = False
            self._shutdown_requested = True
            thread = self._thread
            if thread is None:
                self._discard_queued_commands()
                return True
            self._condition.notify_all()

        if thread is current_thread():
            with self._condition:
                self._discard_queued_commands()
            return True

        thread.join(timeout)
        if not thread.is_alive():
            return True

        with self._condition:
            error = OutboundCommandBusClosedError(
                "Outbound command was not processed before shutdown timed out"
            )
            self._discard_queued_commands(error=error)
            if self._in_flight is not None:
                self._finish_submission(self._in_flight, error=error)
            self._condition.notify_all()
        return False

    def metrics(self) -> OutboundQueueMetrics:
        """Return an atomic snapshot of queue counters and depth."""

        with self._condition:
            return OutboundQueueMetrics(
                queue_limit=self._queue_limit,
                current_depth=len(self._queue),
                peak_depth=self._peak_depth,
                submitted=self._submitted,
                enqueued=self._enqueued,
                coalesced=self._coalesced,
                serialized=self._serialized,
                sent=self._sent,
                rejected_full=self._rejected_full,
                rejected_after_shutdown=self._rejected_after_shutdown,
                discarded_after_shutdown=self._discarded_after_shutdown,
                serialization_failures=self._serialization_failures,
                transport_failures=self._transport_failures,
            )

    def _start_locked(self) -> None:
        if self._started:
            return
        if self._shutdown_requested:
            raise OutboundCommandBusClosedError("Outbound command bus has already been stopped")
        thread = Thread(
            target=self._run,
            name="mirabox-outbound-commands",
            daemon=True,
        )
        self._thread = thread
        self._started = True
        try:
            thread.start()
        except Exception:
            self._thread = None
            self._started = False
            raise

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._shutdown_requested:
                    self._condition.wait()
                if not self._queue:
                    return
                queued = self._queue.popleft()
                self._in_flight = queued

            try:
                raw_message = self._serialize(queued.command)
            except Exception as exc:
                with self._condition:
                    self._serialization_failures += 1
                    self._finish_submission(queued, error=exc)
                    self._in_flight = None
                logger.exception(
                    "Failed to serialize outbound Stream Dock command %s",
                    type(queued.command).__name__,
                )
                continue

            with self._condition:
                self._serialized += 1

            try:
                self._write(raw_message)
            except Exception as exc:
                with self._condition:
                    self._transport_failures += 1
                    self._finish_submission(queued, error=exc)
                logger.exception(
                    "Failed to send outbound Stream Dock command %s",
                    type(queued.command).__name__,
                )
            else:
                with self._condition:
                    self._sent += 1
                    self._finish_submission(queued)
            finally:
                with self._condition:
                    self._in_flight = None

    def _coalesce(self, command: StreamDockCommand) -> CommandFuture | None:
        if not self._coalesce_commands or not self._queue:
            return None

        queued = self._queue[-1]
        key = self._coalescing_key(command)
        if key is None or key != self._coalescing_key(queued.command):
            return None

        queued.command = command
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

    @staticmethod
    def _finish_submission(
        queued: _QueuedCommand,
        *,
        error: Exception | None = None,
    ) -> None:
        queued.completion._finish(error=error)

    def _discard_queued_commands(
        self,
        *,
        error: Exception | None = None,
    ) -> None:
        if error is None:
            error = OutboundCommandBusClosedError("Outbound command was discarded during shutdown")
        self._discarded_after_shutdown += len(self._queue)
        while self._queue:
            self._finish_submission(self._queue.popleft(), error=error)

"""Shared shutdown budget and observable lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic


class StopSignal:
    """Read-only cooperative cancellation signal shared with application work."""

    def __init__(self) -> None:
        self._event = Event()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for shutdown to begin; return whether it was requested."""
        return self._event.wait(timeout)


@dataclass(frozen=True, slots=True)
class ShutdownFailure:
    """One cleanup failure, retaining its stage and original exception."""

    stage: str
    error: BaseException


@dataclass(frozen=True, slots=True)
class ShutdownOutcome:
    """Immutable snapshot; request a new snapshot to observe deferred cleanup."""

    complete: bool
    workers_stopped: bool
    unfinished_callbacks: int
    pending_cleanup: tuple[str, ...]
    cleanup_failures: tuple[ShutdownFailure, ...]
    timed_out_stages: tuple[str, ...]
    callback_timeouts: int
    discarded_events: int
    discarded_commands: int
    primary_failure: BaseException | None

    @property
    def successful(self) -> bool:
        return (
            self.complete
            and not self.cleanup_failures
            and not self.timed_out_stages
            and self.primary_failure is None
        )


class RuntimeWorkerError(RuntimeError):
    """Fatal worker exit with the original BaseException retained as its cause."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"{stage} exited unexpectedly ({type(cause).__name__})")
        self.__cause__ = cause


class ShutdownState:
    """Internal shared state; every waiting stage consumes the same deadline."""

    def __init__(self, timeout: float | None = 5.0) -> None:
        self.signal = StopSignal()
        self._timeout = timeout
        self._lock = Lock()
        self._deadline: float | None = None
        self._workers: dict[str, bool] = {}
        self._pending: set[str] = set()
        self._failures: list[ShutdownFailure] = []
        self._timeouts: list[str] = []
        self._primary_failure: BaseException | None = None

    @property
    def primary_failure(self) -> BaseException | None:
        with self._lock:
            return self._primary_failure

    @primary_failure.setter
    def primary_failure(self, error: BaseException) -> None:
        with self._lock:
            if self._primary_failure is None:
                self._primary_failure = error
                for failure in self._failures:
                    error.add_note(
                        f"Shutdown cleanup failed: {failure.stage} ({type(failure.error).__name__})"
                    )

    def begin(self) -> None:
        with self._lock:
            if not self.signal.requested:
                if self._timeout is not None:
                    self._deadline = monotonic() + self._timeout
                self.signal._event.set()

    def remaining(self, limit: float | None = None) -> float | None:
        with self._lock:
            remaining = None if self._deadline is None else max(0.0, self._deadline - monotonic())
        if limit is None:
            return remaining
        return limit if remaining is None else min(limit, remaining)

    def worker(self, stage: str, stopped: bool) -> None:
        with self._lock:
            self._workers[stage] = stopped

    def pending(self, stage: str, active: bool) -> None:
        with self._lock:
            if active:
                self._pending.add(stage)
            else:
                self._pending.discard(stage)

    def failed(self, stage: str, error: BaseException) -> None:
        with self._lock:
            self._failures.append(ShutdownFailure(stage, error))
            if self._primary_failure is not None:
                self._primary_failure.add_note(
                    f"Shutdown cleanup failed: {stage} ({type(error).__name__})"
                )

    def timed_out(self, stage: str) -> None:
        with self._lock:
            if stage not in self._timeouts:
                self._timeouts.append(stage)

    def snapshot(
        self,
        *,
        unfinished_callbacks: int = 0,
        callback_timeouts: int = 0,
        discarded_events: int = 0,
        discarded_commands: int = 0,
    ) -> ShutdownOutcome | None:
        with self._lock:
            if not self.signal.requested:
                return None
            workers_stopped = all(self._workers.values())
            return ShutdownOutcome(
                complete=workers_stopped and not self._pending and unfinished_callbacks == 0,
                workers_stopped=workers_stopped,
                unfinished_callbacks=unfinished_callbacks,
                pending_cleanup=tuple(sorted(self._pending)),
                cleanup_failures=tuple(self._failures),
                timed_out_stages=tuple(self._timeouts),
                callback_timeouts=callback_timeouts,
                discarded_events=discarded_events,
                discarded_commands=discarded_commands,
                primary_failure=self._primary_failure,
            )

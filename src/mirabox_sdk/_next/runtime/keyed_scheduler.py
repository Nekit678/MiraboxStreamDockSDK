"""Bounded keyed-serial scheduling for typed runtime events."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from threading import Condition, Thread, current_thread
from time import monotonic

from ...events import ActionEvent, StreamDockEvent
from .metrics import HandlerSchedulerMetrics
from .models import DispatchOutcome, DispatchResult
from .ports import DispatchCompletion, HandlerScheduler, RuntimeEventDispatcher
from .scheduler import (
    HandlerSchedulerLifecycleError,
    _DispatchCompletion,
    _is_global_barrier,
    _validate_timeout,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ScheduledWork:
    token: int
    event: StreamDockEvent
    completion: _DispatchCompletion
    context: str | None
    is_barrier: bool


class KeyedSerialHandlerScheduler(HandlerScheduler):
    """Run different action contexts concurrently behind global barriers.

    The scheduler owns a fixed worker pool and a bounded pending deque. A full
    deque blocks the submitting source consumer, preserving boundary
    backpressure instead of transferring events to an unbounded executor.
    """

    def __init__(
        self,
        dispatcher: RuntimeEventDispatcher,
        *,
        worker_count: int,
        pending_limit: int,
    ) -> None:
        if not isinstance(dispatcher, RuntimeEventDispatcher):
            raise TypeError("dispatcher must implement RuntimeEventDispatcher")
        _require_positive_integer("worker_count", worker_count)
        _require_positive_integer("pending_limit", pending_limit)

        self._dispatcher = dispatcher
        self._worker_count = worker_count
        self._pending_limit = pending_limit
        self._condition = Condition()
        self._pending: deque[_ScheduledWork] = deque()
        self._terminalizing = 0
        self._workers: tuple[Thread, ...] = ()
        self._live_workers = 0
        self._active_contexts: set[str] = set()
        self._active_by_thread: dict[Thread, _ScheduledWork] = {}
        self._barrier_active = False
        self._started = False
        self._accepting = True
        self._stopped = False

        self._accepted = 0
        self._completed = 0
        self._peak_pending = 0
        self._peak_active_callbacks = 0
        self._barriers_processed = 0
        self._callback_failures = 0
        self._callback_timeouts = 0
        self._discarded_during_shutdown = 0
        self._admission_backpressure = 0
        self._next_token = 0
        self._timed_out_tokens: set[int] = set()

    def start(self) -> None:
        """Start the fixed worker pool once."""

        with self._condition:
            if self._started and not self._stopped:
                return
            if not self._accepting or self._stopped:
                raise HandlerSchedulerLifecycleError("scheduler has already been stopped")
            self._started = True
            workers = tuple(
                Thread(
                    target=self._run,
                    name=f"mirabox-next-runtime-keyed-{index + 1}",
                    daemon=True,
                )
                for index in range(self._worker_count)
            )
            self._workers = workers

        started_count = 0
        try:
            for worker in workers:
                with self._condition:
                    self._live_workers += 1
                try:
                    worker.start()
                except Exception:
                    with self._condition:
                        self._live_workers -= 1
                    raise
                started_count += 1
        except Exception:
            with self._condition:
                self._workers = workers[:started_count]
                self._accepting = False
                if self._live_workers == 0:
                    self._stopped = True
                self._condition.notify_all()
            for worker in workers[:started_count]:
                worker.join()
            raise

    def submit(self, event: StreamDockEvent) -> DispatchCompletion:
        """Admit one event, blocking only while the pending deque is full."""

        if not isinstance(event, StreamDockEvent):
            raise TypeError("event must be a StreamDockEvent")

        completion = _DispatchCompletion()
        with self._condition:
            if not self._accepting:
                self._discarded_during_shutdown += 1
                completion._finish(result=DispatchResult(DispatchOutcome.DISCARDED_DURING_SHUTDOWN))
                return completion
            if not self._started:
                raise HandlerSchedulerLifecycleError("scheduler has not been started")

            backpressured = False
            while len(self._pending) >= self._pending_limit:
                if not backpressured:
                    self._admission_backpressure += 1
                    backpressured = True
                self._condition.wait()
                if not self._accepting:
                    self._discarded_during_shutdown += 1
                    completion._finish(
                        result=DispatchResult(DispatchOutcome.DISCARDED_DURING_SHUTDOWN)
                    )
                    return completion

            is_barrier = _is_global_barrier(event)
            context = event.context if isinstance(event, ActionEvent) else None
            if not is_barrier and context is None:
                # A malformed or future non-action route is scheduled
                # conservatively until the dispatcher reports the invariant.
                is_barrier = True

            self._next_token += 1
            self._pending.append(
                _ScheduledWork(
                    token=self._next_token,
                    event=event,
                    completion=completion,
                    context=None if is_barrier else context,
                    is_barrier=is_barrier,
                )
            )
            self._accepted += 1
            self._peak_pending = max(self._peak_pending, len(self._pending))
            self._condition.notify_all()
        return completion

    def stop_accepting(self) -> None:
        """Reject later submissions while allowing accepted work to drain."""

        with self._condition:
            self._accepting = False
            if not self._started or self._live_workers == 0:
                self._stopped = True
            self._condition.notify_all()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait for accepted work and record every newly timed-out callback."""

        timeout = _validate_timeout(timeout)
        timed_out: tuple[_ScheduledWork, ...] = ()
        pending_count = 0
        active_count = 0
        with self._condition:
            drained = self._condition.wait_for(self._is_drained, timeout=timeout)
            if drained:
                return True

            timed_out = tuple(
                work
                for work in self._active_by_thread.values()
                if work.token not in self._timed_out_tokens
            )
            for work in timed_out:
                self._timed_out_tokens.add(work.token)
            self._callback_timeouts += len(timed_out)
            pending_count = len(self._pending)
            active_count = len(self._active_by_thread)

        for work in timed_out:
            logger.warning(
                "Runtime callback did not finish before shutdown timeout; event_name=%s context=%s",
                work.event.event_name,
                work.context,
            )
        if pending_count:
            logger.warning(
                "Runtime scheduler retained pending work at shutdown timeout; "
                "pending=%s active_callbacks=%s pending_limit=%s",
                pending_count,
                active_count,
                self._pending_limit,
            )
        return False

    def stop(self, *, timeout: float | None = None) -> bool:
        """Stop workers, discarding only work stranded after a timeout."""

        timeout = _validate_timeout(timeout)
        called_from_callback = self.is_dispatch_thread()
        self.stop_accepting()
        if called_from_callback:
            return True

        deadline = None if timeout is None else monotonic() + timeout
        drained = self.drain(timeout=timeout)
        if not drained:
            self._discard_pending()

        with self._condition:
            workers = self._workers
        for worker in workers:
            remaining = None if deadline is None else max(0.0, deadline - monotonic())
            worker.join(remaining)
        return drained and not any(worker.is_alive() for worker in workers)

    def is_dispatch_thread(self) -> bool:
        """Return whether the caller belongs to this scheduler's worker pool."""

        with self._condition:
            return current_thread() in self._workers

    def metrics(self) -> HandlerSchedulerMetrics:
        """Return an immutable point-in-time scheduler snapshot."""

        with self._condition:
            return HandlerSchedulerMetrics(
                accepted=self._accepted,
                completed=self._completed,
                current_pending=len(self._pending),
                peak_pending=self._peak_pending,
                current_active_callbacks=len(self._active_by_thread),
                peak_active_callbacks=self._peak_active_callbacks,
                active_contexts=len(self._active_contexts),
                barriers_processed=self._barriers_processed,
                callback_failures=self._callback_failures,
                callback_timeouts=self._callback_timeouts,
                discarded_during_shutdown=self._discarded_during_shutdown,
                admission_backpressure=self._admission_backpressure,
            )

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    work = self._take_next_work()
                    while work is None:
                        if not self._accepting and not self._pending:
                            return
                        self._condition.wait()
                        work = self._take_next_work()

                result: DispatchResult | None = None
                error: Exception | None = None
                try:
                    result = self._dispatcher.dispatch(work.event)
                    if not isinstance(result, DispatchResult):
                        raise TypeError("dispatcher must return DispatchResult")
                except Exception as exc:
                    error = exc

                try:
                    if error is not None:
                        work.completion._finish(error=error)
                    else:
                        assert result is not None
                        work.completion._finish(result=result)
                finally:
                    with self._condition:
                        self._completed += 1
                        if work.is_barrier:
                            self._barriers_processed += 1
                            self._barrier_active = False
                        else:
                            assert work.context is not None
                            self._active_contexts.remove(work.context)
                        if result is not None and result.outcome is DispatchOutcome.CALLBACK_FAILED:
                            self._callback_failures += 1
                        self._active_by_thread.pop(current_thread(), None)
                        self._condition.notify_all()
        finally:
            with self._condition:
                self._live_workers -= 1
                if self._live_workers == 0:
                    self._stopped = True
                self._condition.notify_all()

    def _take_next_work(self) -> _ScheduledWork | None:
        if self._barrier_active:
            return None

        for index, work in enumerate(self._pending):
            if work.is_barrier:
                if index != 0 or self._active_by_thread:
                    return None
                self._barrier_active = True
            else:
                assert work.context is not None
                if work.context in self._active_contexts:
                    continue
                self._active_contexts.add(work.context)

            del self._pending[index]
            self._active_by_thread[current_thread()] = work
            self._peak_active_callbacks = max(
                self._peak_active_callbacks,
                len(self._active_by_thread),
            )
            self._condition.notify_all()
            return work
        return None

    def _is_drained(self) -> bool:
        return not self._pending and not self._active_by_thread and self._terminalizing == 0

    def _discard_pending(self) -> None:
        with self._condition:
            discarded = tuple(self._pending)
            self._pending.clear()
            self._terminalizing += len(discarded)
            self._discarded_during_shutdown += len(discarded)
            self._condition.notify_all()
        for work in discarded:
            try:
                work.completion._finish(
                    result=DispatchResult(DispatchOutcome.DISCARDED_DURING_SHUTDOWN)
                )
            finally:
                with self._condition:
                    self._terminalizing -= 1
                    self._completed += 1
                    self._condition.notify_all()


def _require_positive_integer(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")

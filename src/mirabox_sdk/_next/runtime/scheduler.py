"""Reference sequential scheduling for typed runtime events."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from math import isfinite
from threading import Condition, Lock, Thread, current_thread

from ...events import ActionEvent, StreamDockEvent, UnknownStreamDockEvent
from .metrics import HandlerSchedulerMetrics
from .models import DispatchOutcome, DispatchResult
from .ports import DispatchCompletion, HandlerScheduler, RuntimeEventDispatcher
from .routes import RUNTIME_EVENT_REGISTRY, DispatchOrdering


class HandlerSchedulerLifecycleError(RuntimeError):
    """Report use of a scheduler outside its single-run lifecycle."""


class _DispatchCompletion(DispatchCompletion):
    """Internal completion backed by one standard-library future."""

    __slots__ = ("_future",)

    def __init__(self) -> None:
        self._future: Future[DispatchResult] = Future()

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: float | None = None) -> DispatchResult:
        return self._future.result(timeout)

    def add_done_callback(
        self,
        callback: Callable[[DispatchCompletion], None],
    ) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._future.add_done_callback(lambda _future: callback(self))

    def _finish(
        self,
        *,
        result: DispatchResult | None = None,
        error: Exception | None = None,
    ) -> None:
        if (result is None) == (error is None):
            raise ValueError("exactly one of result or error must be supplied")
        if error is not None:
            self._future.set_exception(error)
        else:
            assert result is not None
            self._future.set_result(result)


class SequentialHandlerScheduler(HandlerScheduler):
    """Execute one callback at a time without retaining an event queue.

    Admission is serialized with the callback itself. A concurrent submitter
    therefore applies direct backpressure instead of placing its event in a
    scheduler-owned buffer.
    """

    def __init__(
        self,
        dispatcher: RuntimeEventDispatcher,
    ) -> None:
        if not isinstance(dispatcher, RuntimeEventDispatcher):
            raise TypeError("dispatcher must implement RuntimeEventDispatcher")

        self._dispatcher = dispatcher
        self._condition = Condition()
        self._dispatch_lock = Lock()
        self._started = False
        self._accepting = True
        self._stopped = False
        self._active_thread: Thread | None = None

        self._accepted = 0
        self._completed = 0
        self._current_active_callbacks = 0
        self._peak_active_callbacks = 0
        self._active_contexts = 0
        self._barriers_processed = 0
        self._callback_failures = 0
        self._discarded_during_shutdown = 0
        self._admission_backpressure = 0

    def start(self) -> None:
        """Enter the running state; no worker thread is created."""

        with self._condition:
            if self._started and not self._stopped:
                return
            if not self._accepting or self._stopped:
                raise HandlerSchedulerLifecycleError("scheduler has already been stopped")
            self._started = True

    def submit(self, event: StreamDockEvent) -> DispatchCompletion:
        """Synchronously dispatch one event and return its terminal completion."""

        if not isinstance(event, StreamDockEvent):
            raise TypeError("event must be a StreamDockEvent")

        if not self._dispatch_lock.acquire(blocking=False):
            with self._condition:
                self._admission_backpressure += 1
            self._dispatch_lock.acquire()

        try:
            completion = _DispatchCompletion()
            with self._condition:
                if not self._accepting:
                    self._discarded_during_shutdown += 1
                    completion._finish(
                        result=DispatchResult(DispatchOutcome.DISCARDED_DURING_SHUTDOWN)
                    )
                    return completion
                if not self._started:
                    raise HandlerSchedulerLifecycleError("scheduler has not been started")

                is_barrier = _is_global_barrier(event)
                self._accepted += 1
                self._current_active_callbacks = 1
                self._peak_active_callbacks = max(self._peak_active_callbacks, 1)
                self._active_contexts = int(not is_barrier and isinstance(event, ActionEvent))
                self._active_thread = current_thread()

            result: DispatchResult | None = None
            error: Exception | None = None
            try:
                result = self._dispatcher.dispatch(event)
                if not isinstance(result, DispatchResult):
                    raise TypeError("dispatcher must return DispatchResult")
            except Exception as exc:
                error = exc

            if error is not None:
                completion._finish(error=error)
            else:
                assert result is not None
                completion._finish(result=result)

            with self._condition:
                self._completed += 1
                if is_barrier:
                    self._barriers_processed += 1
                if result is not None and result.outcome is DispatchOutcome.CALLBACK_FAILED:
                    self._callback_failures += 1
                self._current_active_callbacks = 0
                self._active_contexts = 0
                self._active_thread = None
                if not self._accepting:
                    self._stopped = True
                self._condition.notify_all()
            return completion
        finally:
            self._dispatch_lock.release()

    def stop_accepting(self) -> None:
        """Reject later submissions with an explicit discarded completion."""

        with self._condition:
            self._accepting = False
            if self._current_active_callbacks == 0:
                self._stopped = True
            self._condition.notify_all()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait for the directly executing callback, if any, to finish."""

        timeout = _validate_timeout(timeout)
        with self._condition:
            return self._condition.wait_for(
                lambda: self._current_active_callbacks == 0,
                timeout=timeout,
            )

    def stop(self, *, timeout: float | None = None) -> bool:
        """Stop admission and wait unless called by the active callback."""

        timeout = _validate_timeout(timeout)
        with self._condition:
            called_from_callback = self._active_thread is current_thread()
        self.stop_accepting()
        if called_from_callback:
            return True
        return self.drain(timeout=timeout)

    def is_dispatch_thread(self) -> bool:
        """Return whether the caller is currently executing the callback."""

        with self._condition:
            return self._active_thread is current_thread()

    def metrics(self) -> HandlerSchedulerMetrics:
        """Return an immutable point-in-time scheduler snapshot."""

        with self._condition:
            return HandlerSchedulerMetrics(
                accepted=self._accepted,
                completed=self._completed,
                current_pending=0,
                peak_pending=0,
                current_active_callbacks=self._current_active_callbacks,
                peak_active_callbacks=self._peak_active_callbacks,
                active_contexts=self._active_contexts,
                barriers_processed=self._barriers_processed,
                callback_failures=self._callback_failures,
                callback_timeouts=0,
                discarded_during_shutdown=self._discarded_during_shutdown,
                admission_backpressure=self._admission_backpressure,
            )


def _is_global_barrier(event: StreamDockEvent) -> bool:
    if isinstance(event, UnknownStreamDockEvent):
        return True
    try:
        route = RUNTIME_EVENT_REGISTRY.route_for(event)
    except Exception:
        return False
    return route is not None and route.ordering is DispatchOrdering.GLOBAL_BARRIER


def _validate_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("timeout must be a non-negative finite number or None")
    return float(timeout)

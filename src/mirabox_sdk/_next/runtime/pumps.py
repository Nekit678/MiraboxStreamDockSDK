"""Runtime-owned consumers for typed boundary sources."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from threading import Condition, Thread, current_thread
from time import monotonic

from ...events import StreamDockEvent
from ..messaging.ports import InboundEventSource, InboundEventSourceClosedError
from .metrics import RuntimeEventPumpMetrics
from .models import DispatchOutcome, DispatchResult
from .ports import DispatchCompletion, HandlerScheduler, RuntimeEventPumpWorker

logger = logging.getLogger(__name__)


class RuntimeEventPumpLifecycleError(RuntimeError):
    """Report use of an event pump outside its single-run lifecycle."""


@dataclass(slots=True)
class _OwnedEvent:
    finished: bool = False


class RuntimeEventPump(RuntimeEventPumpWorker):
    """Consume typed events and own acknowledgement through terminal dispatch."""

    def __init__(
        self,
        source: InboundEventSource,
        scheduler: HandlerScheduler,
        *,
        poll_interval: float = 0.05,
        on_fatal_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if not isinstance(source, InboundEventSource):
            raise TypeError("source must implement InboundEventSource")
        if not isinstance(scheduler, HandlerScheduler):
            raise TypeError("scheduler must implement HandlerScheduler")
        poll_interval = _validate_poll_interval(poll_interval)
        if on_fatal_error is not None and not callable(on_fatal_error):
            raise TypeError("on_fatal_error must be callable or None")

        self._source = source
        self._scheduler = scheduler
        self._poll_interval = poll_interval
        self._on_fatal_error = on_fatal_error
        self._condition = Condition()
        self._thread: Thread | None = None
        self._started = False
        self._stop_requested = False
        self._stopped = False
        self._failure: Exception | None = None

        self._events_received = 0
        self._events_acknowledged = 0
        self._acknowledgement_failures = 0
        self._submitted_to_scheduler = 0
        self._discarded_during_shutdown = 0
        self._source_poll_timeouts = 0
        self._source_closed = 0
        self._current_owned = 0
        self._peak_owned = 0

    @property
    def failure(self) -> Exception | None:
        """Return the first fatal source, scheduler, or acknowledgement failure."""

        with self._condition:
            return self._failure

    def start(self) -> None:
        """Start the single source-consumer thread once."""

        with self._condition:
            if self._started and not self._stopped:
                return
            if self._stopped or self._stop_requested:
                raise RuntimeEventPumpLifecycleError("event pump has already been stopped")
            thread = Thread(
                target=self._run,
                name="mirabox-next-runtime-events",
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

    def request_stop(self) -> None:
        """Request a non-blocking stop after currently owned work completes."""

        with self._condition:
            self._stop_requested = True
            if self._thread is None:
                self._stopped = True
            self._condition.notify_all()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until the consumer exited and all received events are terminal."""

        timeout = _validate_timeout(timeout)
        with self._condition:
            return self._condition.wait_for(
                lambda: self._stopped and self._current_owned == 0,
                timeout=timeout,
            )

    def stop(self, *, timeout: float | None = None) -> bool:
        """Request stop and join unless invoked by an application callback."""

        timeout = _validate_timeout(timeout)
        self.request_stop()
        with self._condition:
            thread = self._thread
            if thread is None or thread is current_thread():
                return True

        deadline = None if timeout is None else monotonic() + timeout
        thread.join(timeout)
        if thread.is_alive():
            return False
        remaining = None if deadline is None else max(0.0, deadline - monotonic())
        return self.drain(timeout=remaining)

    def is_worker_thread(self) -> bool:
        """Return whether the caller is the pump-owned callback thread."""

        with self._condition:
            return self._thread is current_thread()

    def metrics(self) -> RuntimeEventPumpMetrics:
        """Return an immutable point-in-time ownership snapshot."""

        with self._condition:
            return RuntimeEventPumpMetrics(
                events_received=self._events_received,
                events_acknowledged=self._events_acknowledged,
                acknowledgement_failures=self._acknowledgement_failures,
                submitted_to_scheduler=self._submitted_to_scheduler,
                discarded_during_shutdown=self._discarded_during_shutdown,
                source_poll_timeouts=self._source_poll_timeouts,
                source_closed=self._source_closed,
                current_owned=self._current_owned,
                peak_owned=self._peak_owned,
            )

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    if self._stop_requested:
                        return

                try:
                    event = self._source.receive(timeout=self._poll_interval)
                except TimeoutError:
                    with self._condition:
                        self._source_poll_timeouts += 1
                    continue
                except InboundEventSourceClosedError:
                    with self._condition:
                        self._source_closed += 1
                    return
                except Exception as exc:
                    logger.error(
                        "Runtime inbound event source failed; exception_type=%s",
                        type(exc).__name__,
                    )
                    self._record_fatal(exc)
                    return

                owned = self._take_ownership()
                if not isinstance(event, StreamDockEvent):
                    self._finish_owned(
                        owned,
                        error=TypeError("source returned a non-StreamDockEvent value"),
                    )
                    return
                with self._condition:
                    stopping = self._stop_requested
                if stopping:
                    self._finish_owned(
                        owned,
                        result=DispatchResult(DispatchOutcome.DISCARDED_DURING_SHUTDOWN),
                    )
                    return

                with self._condition:
                    self._submitted_to_scheduler += 1
                try:
                    completion = self._scheduler.submit(event)
                    completion.add_done_callback(
                        lambda finished, owned=owned: self._on_dispatch_done(owned, finished)
                    )
                except Exception as exc:
                    self._finish_owned(owned, error=exc)
        finally:
            with self._condition:
                self._stopped = True
                self._condition.notify_all()

    def _take_ownership(self) -> _OwnedEvent:
        with self._condition:
            self._events_received += 1
            self._current_owned += 1
            self._peak_owned = max(self._peak_owned, self._current_owned)
        return _OwnedEvent()

    def _on_dispatch_done(
        self,
        owned: _OwnedEvent,
        completion: DispatchCompletion,
    ) -> None:
        try:
            result = completion.result()
            if not isinstance(result, DispatchResult):
                raise TypeError("dispatch completion must return DispatchResult")
        except Exception as exc:
            self._finish_owned(owned, error=exc)
        else:
            self._finish_owned(owned, result=result)

    def _finish_owned(
        self,
        owned: _OwnedEvent,
        *,
        result: DispatchResult | None = None,
        error: Exception | None = None,
    ) -> None:
        with self._condition:
            if owned.finished:
                return
            owned.finished = True
            if result is not None and result.outcome is DispatchOutcome.DISCARDED_DURING_SHUTDOWN:
                self._discarded_during_shutdown += 1

        fatal = error
        try:
            self._source.task_done()
        except Exception as exc:
            with self._condition:
                self._acknowledgement_failures += 1
            logger.error(
                "Runtime event acknowledgement failed; exception_type=%s",
                type(exc).__name__,
            )
            if fatal is None:
                fatal = exc
        else:
            with self._condition:
                self._events_acknowledged += 1
        finally:
            with self._condition:
                self._current_owned -= 1
                self._condition.notify_all()

        if fatal is not None:
            self._record_fatal(fatal)

    def _record_fatal(self, error: Exception) -> None:
        callback: Callable[[Exception], None] | None = None
        with self._condition:
            if self._failure is not None:
                return
            self._failure = error
            self._stop_requested = True
            callback = self._on_fatal_error
            self._condition.notify_all()
        if callback is not None:
            try:
                callback(error)
            except Exception as exc:
                logger.error(
                    "Runtime fatal-error observer failed; exception_type=%s",
                    type(exc).__name__,
                )


def _validate_poll_interval(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError("poll_interval must be a positive finite number")
    return float(value)


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

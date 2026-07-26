"""Bounded keyed-serial dispatch for inbound Stream Dock events."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Condition, Thread, current_thread
from time import monotonic

from .events import (
    ActionEvent,
    DialRotateEvent,
    StreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)

logger = logging.getLogger(__name__)


class InboundOverflowPolicy(StrEnum):
    """Policy applied to discardable events when the inbound queue is full."""

    DROP_NEWEST = "drop_newest"
    DROP_OLDEST = "drop_oldest"


class _InboundEventClass(StrEnum):
    """Delivery guarantee used by the bounded inbound queue."""

    LOSSLESS = "lossless"
    COALESCABLE = "coalescable"


@dataclass(frozen=True, slots=True)
class InboundQueueMetrics:
    """Thread-safe point-in-time counters for one inbound event queue."""

    queue_limit: int
    current_depth: int
    peak_depth: int
    received: int
    enqueued: int
    coalesced: int
    dispatched: int
    dropped_newest: int
    dropped_oldest: int
    dropped_after_shutdown: int
    dropped_without_listener: int
    callback_failures: int
    backpressured: int = 0
    callback_timeouts: int = 0

    @property
    def dropped(self) -> int:
        """Return the total number of events discarded by the connection."""

        return (
            self.dropped_newest
            + self.dropped_oldest
            + self.dropped_after_shutdown
            + self.dropped_without_listener
        )


@dataclass(slots=True)
class _QueuedEvent:
    event: StreamDockEvent


@dataclass(frozen=True, slots=True)
class _ActiveCallback:
    token: int
    event_name: str
    context: str | None


class _InboundEventDispatcher:
    """Own a bounded queue and dispatch callbacks serially per action context."""

    def __init__(
        self,
        *,
        queue_limit: int,
        worker_count: int,
        overflow_policy: InboundOverflowPolicy,
        coalesce_dial_rotations: bool,
        dispatch: Callable[[StreamDockEvent], bool],
    ) -> None:
        self._queue_limit = queue_limit
        self._worker_count = worker_count
        self._overflow_policy = overflow_policy
        self._coalesce_dial_rotations = coalesce_dial_rotations
        self._dispatch = dispatch
        self._condition = Condition()
        self._queue: deque[_QueuedEvent] = deque()
        self._last_queued_by_context: dict[str, _QueuedEvent] = {}
        self._threads: tuple[Thread, ...] = ()
        self._active_contexts: set[str] = set()
        self._active_callbacks = 0
        self._active_callbacks_by_thread: dict[Thread, _ActiveCallback] = {}
        self._next_callback_token = 0
        self._timed_out_callback_tokens: set[int] = set()
        self._last_timed_out_callbacks: tuple[_ActiveCallback, ...] = ()
        self._barrier_active = False
        self._started = False
        self._accepting = True
        self._shutdown_requested = False

        self._peak_depth = 0
        self._received = 0
        self._enqueued = 0
        self._coalesced = 0
        self._backpressured = 0
        self._dispatched = 0
        self._dropped_newest = 0
        self._dropped_oldest = 0
        self._dropped_after_shutdown = 0
        self._dropped_without_listener = 0
        self._callback_failures = 0
        self._callback_timeouts = 0

    def start(self) -> None:
        """Start the event-dispatch worker pool exactly once."""

        with self._condition:
            if self._started:
                raise RuntimeError("Inbound event dispatcher has already been started")
            if self._shutdown_requested:
                raise RuntimeError("Inbound event dispatcher has already been stopped")
            self._started = True
            threads = tuple(
                Thread(
                    target=self._run,
                    name=f"mirabox-inbound-events-{index + 1}",
                    daemon=True,
                )
                for index in range(self._worker_count)
            )
            self._threads = threads
            for thread in threads:
                thread.start()

    def submit(self, event: StreamDockEvent) -> bool:
        """Enqueue one event, blocking only to preserve lossless overflow."""

        with self._condition:
            self._received += 1
            if not self._accepting:
                self._dropped_after_shutdown += 1
                return False

            if self._coalesce(event):
                self._coalesced += 1
                return True

            event_class = self._classify_event(event)
            backpressured = False
            while len(self._queue) == self._queue_limit:
                if event_class is _InboundEventClass.LOSSLESS:
                    if self._drop_queued_discardable_event():
                        break
                    if not backpressured:
                        self._backpressured += 1
                        backpressured = True
                    self._condition.wait()
                    if not self._accepting:
                        self._dropped_after_shutdown += 1
                        return False
                    continue

                if self._overflow_policy is InboundOverflowPolicy.DROP_NEWEST:
                    self._dropped_newest += 1
                    self._break_context_coalescing(event)
                    return False

                if self._drop_queued_discardable_event():
                    break

                # A discardable event must not displace queued lossless state.
                self._dropped_newest += 1
                self._break_context_coalescing(event)
                return False

            queued = _QueuedEvent(event)
            self._queue.append(queued)
            context = self._dispatch_context(event)
            if context is None:
                # Lifecycle, broadcast, and unknown events are ordering
                # barriers for every context and context-local coalescing.
                self._last_queued_by_context.clear()
            else:
                self._last_queued_by_context[context] = queued
            self._enqueued += 1
            self._peak_depth = max(self._peak_depth, len(self._queue))
            self._condition.notify()
            return True

    def stop_accepting(self) -> None:
        """Reject events arriving after connection shutdown begins."""

        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def is_dispatch_thread(self) -> bool:
        """Return whether the caller is one of this dispatcher's workers."""

        with self._condition:
            return current_thread() in self._threads

    def shutdown(self, *, timeout: float | None) -> bool:
        """Stop after draining queued events, optionally bounded by ``timeout``."""

        with self._condition:
            self._accepting = False
            self._shutdown_requested = True
            self._last_timed_out_callbacks = ()
            threads = self._threads
            if not threads:
                self._discard_queued_events()
                return True
            self._condition.notify_all()

        if current_thread() in threads:
            with self._condition:
                self._discard_queued_events()
                deadline = None if timeout is None else monotonic() + timeout
                while self._active_callbacks > 1:
                    remaining = None if deadline is None else deadline - monotonic()
                    if remaining is not None and remaining <= 0:
                        self._record_callback_timeouts(exclude_thread=current_thread())
                        return False
                    self._condition.wait(remaining)
                return True

        deadline = None if timeout is None else monotonic() + timeout
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - monotonic())
            thread.join(remaining)
        if not any(thread.is_alive() for thread in threads):
            return True

        with self._condition:
            self._record_callback_timeouts()
            self._discard_queued_events()
            self._condition.notify_all()
        return False

    def shutdown_timeout_callbacks(self) -> tuple[tuple[str, str | None], ...]:
        """Return event names and contexts captured by the latest timeout."""

        with self._condition:
            return tuple(
                (callback.event_name, callback.context)
                for callback in self._last_timed_out_callbacks
            )

    def metrics(self) -> InboundQueueMetrics:
        """Return an atomic snapshot of queue counters and depth."""

        with self._condition:
            return InboundQueueMetrics(
                queue_limit=self._queue_limit,
                current_depth=len(self._queue),
                peak_depth=self._peak_depth,
                received=self._received,
                enqueued=self._enqueued,
                coalesced=self._coalesced,
                backpressured=self._backpressured,
                dispatched=self._dispatched,
                dropped_newest=self._dropped_newest,
                dropped_oldest=self._dropped_oldest,
                dropped_after_shutdown=self._dropped_after_shutdown,
                dropped_without_listener=self._dropped_without_listener,
                callback_failures=self._callback_failures,
                callback_timeouts=self._callback_timeouts,
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                queued = self._take_next_queued_event()
                while queued is None:
                    if not self._queue and self._shutdown_requested:
                        return
                    self._condition.wait()
                    queued = self._take_next_queued_event()

            try:
                delivered = self._dispatch(queued.event)
            except Exception:
                with self._condition:
                    self._callback_failures += 1
                logger.exception(
                    "Failed to dispatch inbound Stream Dock event %s",
                    queued.event.event_name,
                )
            else:
                with self._condition:
                    if delivered:
                        self._dispatched += 1
                    else:
                        self._dropped_without_listener += 1
            finally:
                with self._condition:
                    self._finish_dispatch(queued.event)

    def _take_next_queued_event(self) -> _QueuedEvent | None:
        if self._barrier_active:
            return None

        for index, queued in enumerate(self._queue):
            context = self._dispatch_context(queued.event)
            if context is None:
                if index != 0 or self._active_callbacks:
                    return None
                self._barrier_active = True
            elif context in self._active_contexts:
                continue
            else:
                self._active_contexts.add(context)

            del self._queue[index]
            self._forget_queued_event(queued)
            self._active_callbacks += 1
            self._next_callback_token += 1
            self._active_callbacks_by_thread[current_thread()] = _ActiveCallback(
                token=self._next_callback_token,
                event_name=queued.event.event_name,
                context=self._event_context(queued.event),
            )
            self._condition.notify_all()
            return queued
        return None

    def _finish_dispatch(self, event: StreamDockEvent) -> None:
        context = self._dispatch_context(event)
        if context is None:
            self._barrier_active = False
        else:
            self._active_contexts.remove(context)
        self._active_callbacks -= 1
        self._active_callbacks_by_thread.pop(current_thread(), None)
        self._condition.notify_all()

    def _record_callback_timeouts(self, *, exclude_thread: Thread | None = None) -> None:
        callbacks = tuple(
            callback
            for thread, callback in self._active_callbacks_by_thread.items()
            if thread is not exclude_thread
        )
        self._last_timed_out_callbacks = callbacks
        for callback in callbacks:
            if callback.token in self._timed_out_callback_tokens:
                continue
            self._timed_out_callback_tokens.add(callback.token)
            self._callback_timeouts += 1

    def _coalesce(self, event: StreamDockEvent) -> bool:
        if not self._coalesce_dial_rotations or not isinstance(event, DialRotateEvent):
            return False

        queued = self._last_queued_by_context.get(event.context)
        if queued is None or not isinstance(queued.event, DialRotateEvent):
            return False

        previous = queued.event
        if (
            previous.action != event.action
            or previous.device != event.device
            or previous.coordinates != event.coordinates
            or previous.pressed != event.pressed
            or previous.controller != event.controller
        ):
            return False

        queued.event = replace(event, ticks=previous.ticks + event.ticks)
        return True

    def _break_context_coalescing(self, event: StreamDockEvent) -> None:
        context = self._dispatch_context(event)
        if context is None:
            self._last_queued_by_context.clear()
        else:
            self._last_queued_by_context.pop(context, None)

    def _drop_queued_discardable_event(self) -> bool:
        indexes = range(len(self._queue))
        if self._overflow_policy is InboundOverflowPolicy.DROP_NEWEST:
            indexes = reversed(indexes)

        for index in indexes:
            queued = self._queue[index]
            if self._classify_event(queued.event) is _InboundEventClass.LOSSLESS:
                continue

            del self._queue[index]
            self._forget_queued_event(queued)
            if self._overflow_policy is InboundOverflowPolicy.DROP_NEWEST:
                self._dropped_newest += 1
            else:
                self._dropped_oldest += 1
            return True
        return False

    def _forget_queued_event(self, queued: _QueuedEvent) -> None:
        context = self._dispatch_context(queued.event)
        if context is not None and self._last_queued_by_context.get(context) is queued:
            del self._last_queued_by_context[context]

    def _discard_queued_events(self) -> None:
        self._dropped_after_shutdown += len(self._queue)
        self._queue.clear()
        self._last_queued_by_context.clear()

    @staticmethod
    def _dispatch_context(event: StreamDockEvent) -> str | None:
        if isinstance(event, (WillAppearEvent, WillDisappearEvent)):
            return None
        return event.context if isinstance(event, ActionEvent) else None

    @staticmethod
    def _event_context(event: StreamDockEvent) -> str | None:
        return event.context if isinstance(event, ActionEvent) else None

    @staticmethod
    def _classify_event(event: StreamDockEvent) -> _InboundEventClass:
        if isinstance(event, DialRotateEvent):
            return _InboundEventClass.COALESCABLE
        return _InboundEventClass.LOSSLESS

"""Bounded asynchronous dispatch for inbound Stream Dock events."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Condition, Thread, current_thread

from .events import ActionEvent, DialRotateEvent, StreamDockEvent

logger = logging.getLogger(__name__)


class InboundOverflowPolicy(StrEnum):
    """Policy applied when the inbound event queue reaches its limit."""

    DROP_NEWEST = "drop_newest"
    DROP_OLDEST = "drop_oldest"


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


class _InboundEventDispatcher:
    """Own a bounded queue and invoke one callback from a dedicated thread."""

    def __init__(
        self,
        *,
        queue_limit: int,
        overflow_policy: InboundOverflowPolicy,
        coalesce_dial_rotations: bool,
        dispatch: Callable[[StreamDockEvent], bool],
    ) -> None:
        self._queue_limit = queue_limit
        self._overflow_policy = overflow_policy
        self._coalesce_dial_rotations = coalesce_dial_rotations
        self._dispatch = dispatch
        self._condition = Condition()
        self._queue: deque[_QueuedEvent] = deque()
        self._last_queued_by_context: dict[str, _QueuedEvent] = {}
        self._thread: Thread | None = None
        self._started = False
        self._accepting = True
        self._shutdown_requested = False

        self._peak_depth = 0
        self._received = 0
        self._enqueued = 0
        self._coalesced = 0
        self._dispatched = 0
        self._dropped_newest = 0
        self._dropped_oldest = 0
        self._dropped_after_shutdown = 0
        self._dropped_without_listener = 0
        self._callback_failures = 0

    def start(self) -> None:
        """Start the single event-dispatch thread exactly once."""

        with self._condition:
            if self._started:
                raise RuntimeError("Inbound event dispatcher has already been started")
            if self._shutdown_requested:
                raise RuntimeError("Inbound event dispatcher has already been stopped")
            self._started = True
            thread = Thread(
                target=self._run,
                name="mirabox-inbound-events",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def submit(self, event: StreamDockEvent) -> bool:
        """Enqueue one event without blocking the WebSocket reader."""

        with self._condition:
            self._received += 1
            if not self._accepting:
                self._dropped_after_shutdown += 1
                return False

            if self._coalesce(event):
                self._coalesced += 1
                return True

            if len(self._queue) == self._queue_limit:
                if self._overflow_policy is InboundOverflowPolicy.DROP_NEWEST:
                    self._dropped_newest += 1
                    self._break_context_coalescing(event)
                    return False

                dropped = self._queue.popleft()
                self._forget_queued_event(dropped)
                self._dropped_oldest += 1

            queued = _QueuedEvent(event)
            self._queue.append(queued)
            context = self._event_context(event)
            if context is None:
                # Broadcast and unknown events may affect every action. Treat
                # them as ordering barriers for context-local coalescing.
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

    def shutdown(self, *, timeout: float | None) -> bool:
        """Stop after draining queued events, optionally bounded by ``timeout``."""

        with self._condition:
            self._accepting = False
            self._shutdown_requested = True
            thread = self._thread
            if thread is None:
                self._discard_queued_events()
                return True
            self._condition.notify_all()

        if thread is current_thread():
            with self._condition:
                self._discard_queued_events()
            return True

        thread.join(timeout)
        if not thread.is_alive():
            return True

        with self._condition:
            self._discard_queued_events()
            self._condition.notify_all()
        return False

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
                dispatched=self._dispatched,
                dropped_newest=self._dropped_newest,
                dropped_oldest=self._dropped_oldest,
                dropped_after_shutdown=self._dropped_after_shutdown,
                dropped_without_listener=self._dropped_without_listener,
                callback_failures=self._callback_failures,
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._shutdown_requested:
                    self._condition.wait()
                if not self._queue:
                    return
                queued = self._queue.popleft()
                self._forget_queued_event(queued)

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
        context = self._event_context(event)
        if context is None:
            self._last_queued_by_context.clear()
        else:
            self._last_queued_by_context.pop(context, None)

    def _forget_queued_event(self, queued: _QueuedEvent) -> None:
        context = self._event_context(queued.event)
        if context is not None and self._last_queued_by_context.get(context) is queued:
            del self._last_queued_by_context[context]

    def _discard_queued_events(self) -> None:
        self._dropped_after_shutdown += len(self._queue)
        self._queue.clear()
        self._last_queued_by_context.clear()

    @staticmethod
    def _event_context(event: StreamDockEvent) -> str | None:
        return event.context if isinstance(event, ActionEvent) else None

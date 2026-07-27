"""Bounded semantic queue for decoded Stream Dock events."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite
from threading import Condition
from time import monotonic

from ...events import (
    ActionEvent,
    DialRotateEvent,
    StreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from .metrics import InboundEventQueueMetrics
from .ports import InboundEventQueueControl, InboundEventSink, InboundEventSource


class InboundOverflowPolicy(StrEnum):
    """Policy applied only to explicitly discardable typed events."""

    DROP_NEWEST = "drop_newest"
    DROP_OLDEST = "drop_oldest"


@dataclass(slots=True)
class _QueuedEvent:
    event: StreamDockEvent


class InboundEventQueue(InboundEventSource, InboundEventSink, InboundEventQueueControl):
    """Preserve lossless events and coalesce compatible dial rotations."""

    def __init__(
        self,
        queue_limit: int,
        *,
        overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
        coalesce_dial_rotations: bool = False,
    ) -> None:
        _validate_queue_limit(queue_limit)
        if not isinstance(overflow_policy, InboundOverflowPolicy):
            raise ValueError("overflow_policy must be an InboundOverflowPolicy")
        if type(coalesce_dial_rotations) is not bool:
            raise ValueError("coalesce_dial_rotations must be a boolean")

        self._queue_limit = queue_limit
        self._overflow_policy = overflow_policy
        self._coalesce_dial_rotations = coalesce_dial_rotations
        self._condition = Condition()
        self._queue: deque[_QueuedEvent] = deque()
        self._last_queued_by_context: dict[str, _QueuedEvent] = {}
        self._accepting = True
        self._in_flight = 0

        self._peak_depth = 0
        self._submitted = 0
        self._enqueued = 0
        self._coalesced = 0
        self._dequeued = 0
        self._acknowledged = 0
        self._backpressured = 0
        self._dropped_newest = 0
        self._dropped_oldest = 0
        self._rejected_full = 0
        self._rejected_after_shutdown = 0
        self._discarded_during_shutdown = 0

    def submit(
        self,
        event: StreamDockEvent,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Submit one typed event according to its semantic delivery policy."""

        if not isinstance(event, StreamDockEvent):
            raise TypeError("event must be StreamDockEvent")
        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout

        with self._condition:
            self._submitted += 1
            if not self._accepting:
                self._rejected_after_shutdown += 1
                return False

            if self._coalesce(event):
                self._coalesced += 1
                return True

            discardable = isinstance(event, DialRotateEvent)
            backpressured = False
            while len(self._queue) >= self._queue_limit:
                if discardable:
                    if self._overflow_policy is InboundOverflowPolicy.DROP_NEWEST:
                        self._dropped_newest += 1
                        self._break_context_coalescing(event)
                        return False
                    if self._drop_queued_rotation():
                        break
                    self._dropped_newest += 1
                    self._break_context_coalescing(event)
                    return False

                if self._drop_queued_rotation():
                    break

                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    self._rejected_full += 1
                    self._break_context_coalescing(event)
                    return False
                if not backpressured:
                    self._backpressured += 1
                    backpressured = True
                self._condition.wait(remaining)
                if not self._accepting:
                    self._rejected_after_shutdown += 1
                    self._break_context_coalescing(event)
                    return False

            queued = _QueuedEvent(event)
            self._queue.append(queued)
            context = self._coalescing_context(event)
            if context is None:
                self._last_queued_by_context.clear()
            else:
                self._last_queued_by_context[context] = queued
            self._enqueued += 1
            self._peak_depth = max(self._peak_depth, len(self._queue))
            self._condition.notify_all()
            return True

    def receive(self, *, timeout: float | None = None) -> StreamDockEvent:
        """Return the next event; the consumer must later call ``task_done``."""

        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout

        with self._condition:
            while not self._queue:
                if not self._accepting:
                    raise InboundEventQueueClosedError("Inbound event queue is closed")
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Timed out waiting for inbound event queue")
                self._condition.wait(remaining)

            queued = self._queue.popleft()
            self._forget_queued_event(queued)
            self._dequeued += 1
            self._in_flight += 1
            self._condition.notify_all()
            return queued.event

    def task_done(self) -> None:
        """Acknowledge completed handling of one event returned by ``receive``."""

        with self._condition:
            if self._in_flight == 0:
                raise ValueError("task_done() called too many times")
            self._in_flight -= 1
            self._acknowledged += 1
            self._condition.notify_all()

    def stop_accepting(self) -> None:
        """Reject new events while allowing accepted events to drain."""

        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until all queued and in-flight events have been acknowledged."""

        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while self._queue or self._in_flight:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop submissions and drain, recording every timeout discard."""

        timeout = _validate_timeout(timeout)
        self.stop_accepting()
        if self.drain(timeout=timeout):
            return True

        with self._condition:
            self._discarded_during_shutdown += len(self._queue)
            self._queue.clear()
            self._last_queued_by_context.clear()
            self._condition.notify_all()
        return False

    def metrics(self) -> InboundEventQueueMetrics:
        """Return an atomic immutable metrics snapshot."""

        with self._condition:
            return InboundEventQueueMetrics(
                queue_limit=self._queue_limit,
                current_depth=len(self._queue),
                peak_depth=self._peak_depth,
                submitted=self._submitted,
                enqueued=self._enqueued,
                coalesced=self._coalesced,
                dequeued=self._dequeued,
                in_flight=self._in_flight,
                acknowledged=self._acknowledged,
                backpressured=self._backpressured,
                dropped_newest=self._dropped_newest,
                dropped_oldest=self._dropped_oldest,
                rejected_full=self._rejected_full,
                rejected_after_shutdown=self._rejected_after_shutdown,
                discarded_during_shutdown=self._discarded_during_shutdown,
            )

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

    def _drop_queued_rotation(self) -> bool:
        indexes = range(len(self._queue))
        if self._overflow_policy is InboundOverflowPolicy.DROP_NEWEST:
            indexes = reversed(indexes)

        for index in indexes:
            queued = self._queue[index]
            if not isinstance(queued.event, DialRotateEvent):
                continue
            del self._queue[index]
            self._forget_queued_event(queued)
            if self._overflow_policy is InboundOverflowPolicy.DROP_NEWEST:
                self._dropped_newest += 1
            else:
                self._dropped_oldest += 1
            return True
        return False

    def _break_context_coalescing(self, event: StreamDockEvent) -> None:
        context = self._coalescing_context(event)
        if context is None:
            self._last_queued_by_context.clear()
        else:
            self._last_queued_by_context.pop(context, None)

    def _forget_queued_event(self, queued: _QueuedEvent) -> None:
        context = self._coalescing_context(queued.event)
        if context is not None and self._last_queued_by_context.get(context) is queued:
            del self._last_queued_by_context[context]

    @staticmethod
    def _coalescing_context(event: StreamDockEvent) -> str | None:
        if isinstance(event, (WillAppearEvent, WillDisappearEvent)):
            return None
        return event.context if isinstance(event, ActionEvent) else None


class InboundEventQueueClosedError(RuntimeError):
    """Report that the typed inbound queue reached terminal shutdown."""


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

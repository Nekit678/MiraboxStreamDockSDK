"""Bounded API-independent queues owned by the transport layer."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from math import isfinite
from threading import Condition
from time import monotonic
from typing import Generic, TypeVar

from .frames import OutboundFrame, TextFrame
from .metrics import TransportQueueMetrics
from .ports import (
    RawInboundSink,
    RawInboundSource,
    RawOutboundSink,
    RawOutboundSource,
    SessionEventSink,
    SessionEventSource,
    TransportQueueControl,
)
from .session import SessionEvent

ItemT = TypeVar("ItemT")


class TransportQueueError(RuntimeError):
    """Base error for transport queue lifecycle failures."""


class TransportQueueClosedError(TransportQueueError):
    """Report that a transport queue no longer accepts or produces items."""


class TransportQueueFullError(TransportQueueError):
    """Report that bounded backpressure expired before capacity became free."""


class _BoundedTransportQueue(Generic[ItemT]):
    """Condition-backed FIFO with bounded capacity and explicit shutdown."""

    def __init__(
        self,
        *,
        queue_limit: int,
        queue_name: str,
        item_type: type[ItemT],
        reject: Callable[[ItemT, Exception], None] | None = None,
    ) -> None:
        _validate_queue_limit(queue_limit)
        self._queue_limit = queue_limit
        self._queue_name = queue_name
        self._item_type = item_type
        self._reject = reject
        self._condition = Condition()
        self._queue: deque[ItemT] = deque()
        self._accepting = True

        self._peak_depth = 0
        self._submitted = 0
        self._enqueued = 0
        self._dequeued = 0
        self._backpressured = 0
        self._rejected_full = 0
        self._rejected_after_shutdown = 0
        self._discarded_during_shutdown = 0

    def submit(self, item: ItemT, *, timeout: float | None = None) -> bool:
        """Submit an item, waiting for bounded capacity when necessary."""

        if not isinstance(item, self._item_type):
            raise TypeError(f"item must be {self._item_type.__name__}")
        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout

        with self._condition:
            self._submitted += 1
            if not self._accepting:
                self._rejected_after_shutdown += 1
                self._reject_item(
                    item,
                    TransportQueueClosedError(f"{self._queue_name} is no longer accepting items"),
                )
                return False

            backpressured = False
            while len(self._queue) >= self._queue_limit:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    self._rejected_full += 1
                    self._reject_item(
                        item,
                        TransportQueueFullError(
                            f"{self._queue_name} is full (limit={self._queue_limit})"
                        ),
                    )
                    return False
                if not backpressured:
                    self._backpressured += 1
                    backpressured = True
                self._condition.wait(remaining)
                if not self._accepting:
                    self._rejected_after_shutdown += 1
                    self._reject_item(
                        item,
                        TransportQueueClosedError(
                            f"{self._queue_name} is no longer accepting items"
                        ),
                    )
                    return False

            self._queue.append(item)
            self._enqueued += 1
            self._peak_depth = max(self._peak_depth, len(self._queue))
            self._condition.notify_all()
            return True

    def receive(self, *, timeout: float | None = None) -> ItemT:
        """Return the next item, waiting until data or terminal shutdown."""

        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout

        with self._condition:
            while not self._queue:
                if not self._accepting:
                    raise TransportQueueClosedError(f"{self._queue_name} is closed")
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for {self._queue_name}")
                self._condition.wait(remaining)

            item = self._queue.popleft()
            self._dequeued += 1
            self._condition.notify_all()
            return item

    def stop_accepting(self) -> None:
        """Reject new items while allowing accepted items to drain."""

        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until the queue is empty without changing its lifecycle."""

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
        """Stop submissions and drain, discarding observably after timeout."""

        timeout = _validate_timeout(timeout)
        self.stop_accepting()
        if self.drain(timeout=timeout):
            return True

        discarded: tuple[ItemT, ...]
        with self._condition:
            error = TransportQueueClosedError(
                f"{self._queue_name} item was discarded during shutdown"
            )
            self._discarded_during_shutdown += len(self._queue)
            discarded = tuple(self._queue)
            self._queue.clear()
            self._condition.notify_all()

        for item in discarded:
            self._reject_item(item, error)
        return False

    def metrics(self) -> TransportQueueMetrics:
        """Return an atomic immutable snapshot of queue state and counters."""

        with self._condition:
            return TransportQueueMetrics(
                queue_limit=self._queue_limit,
                current_depth=len(self._queue),
                peak_depth=self._peak_depth,
                submitted=self._submitted,
                enqueued=self._enqueued,
                dequeued=self._dequeued,
                backpressured=self._backpressured,
                rejected_full=self._rejected_full,
                rejected_after_shutdown=self._rejected_after_shutdown,
                discarded_during_shutdown=self._discarded_during_shutdown,
            )

    def _reject_item(self, item: ItemT, error: Exception) -> None:
        if self._reject is not None:
            self._reject(item, error)


class RawInboundQueue(RawInboundSource, RawInboundSink, TransportQueueControl):
    """Bounded lossless queue of WebSocket text frames."""

    __slots__ = ("_queue",)

    def __init__(self, queue_limit: int) -> None:
        self._queue = _BoundedTransportQueue(
            queue_limit=queue_limit,
            queue_name="Raw inbound queue",
            item_type=str,
        )

    def submit(self, frame: TextFrame, *, timeout: float | None = None) -> bool:
        """Submit a text frame, applying transport-level backpressure."""

        return self._queue.submit(frame, timeout=timeout)

    def receive(self, *, timeout: float | None = None) -> TextFrame:
        """Return the next text frame in wire order."""

        return self._queue.receive(timeout=timeout)

    def stop_accepting(self) -> None:
        """Reject new text frames while allowing accepted frames to drain."""

        self._queue.stop_accepting()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until all queued frames have been received."""

        return self._queue.drain(timeout=timeout)

    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop and drain the queue, discarding explicitly after timeout."""

        return self._queue.shutdown(timeout=timeout)

    def metrics(self) -> TransportQueueMetrics:
        """Return an atomic queue metrics snapshot."""

        return self._queue.metrics()


class RawOutboundQueue(RawOutboundSource, RawOutboundSink, TransportQueueControl):
    """Bounded queue of serialized frames with per-frame receipts."""

    __slots__ = ("_queue",)

    def __init__(self, queue_limit: int) -> None:
        self._queue = _BoundedTransportQueue(
            queue_limit=queue_limit,
            queue_name="Raw outbound queue",
            item_type=OutboundFrame,
            reject=self._fail_frame,
        )

    def submit(self, frame: OutboundFrame, *, timeout: float | None = None) -> bool:
        """Submit a frame or fail its receipt when it cannot be accepted."""

        if not isinstance(frame, OutboundFrame):
            raise TypeError("frame must be OutboundFrame")
        if frame.receipt.done():
            raise ValueError("frame receipt must be pending")
        return self._queue.submit(frame, timeout=timeout)

    def receive(self, *, timeout: float | None = None) -> OutboundFrame:
        """Return the next serialized frame in FIFO order."""

        return self._queue.receive(timeout=timeout)

    def stop_accepting(self) -> None:
        """Reject new frames while allowing accepted frames to drain."""

        self._queue.stop_accepting()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until all queued frames have been received."""

        return self._queue.drain(timeout=timeout)

    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop and drain, failing receipts for timed-out frames."""

        return self._queue.shutdown(timeout=timeout)

    def metrics(self) -> TransportQueueMetrics:
        """Return an atomic queue metrics snapshot."""

        return self._queue.metrics()

    @staticmethod
    def _fail_frame(frame: OutboundFrame, error: Exception) -> None:
        frame.receipt._finish(error=error)


class SessionEventQueue(SessionEventSource, SessionEventSink, TransportQueueControl):
    """Bounded lossless queue of typed transport lifecycle events."""

    __slots__ = ("_queue",)

    def __init__(self, queue_limit: int) -> None:
        self._queue = _BoundedTransportQueue(
            queue_limit=queue_limit,
            queue_name="Session event queue",
            item_type=SessionEvent,
        )

    def submit(self, event: SessionEvent, *, timeout: float | None = None) -> bool:
        """Submit a lifecycle event with lossless backpressure."""

        return self._queue.submit(event, timeout=timeout)

    def receive(self, *, timeout: float | None = None) -> SessionEvent:
        """Return the next lifecycle event in publication order."""

        return self._queue.receive(timeout=timeout)

    def stop_accepting(self) -> None:
        """Reject new lifecycle events while allowing the queue to drain."""

        self._queue.stop_accepting()

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until all queued lifecycle events have been received."""

        return self._queue.drain(timeout=timeout)

    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop and drain, recording any timeout discard."""

        return self._queue.shutdown(timeout=timeout)

    def metrics(self) -> TransportQueueMetrics:
        """Return an atomic queue metrics snapshot."""

        return self._queue.metrics()


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

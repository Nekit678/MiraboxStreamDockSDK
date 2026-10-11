"""Track individual handlers without retaining events or interrupting user code."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import TypeVar

from .config import _require_positive_finite_number
from .metrics import ActiveCallbackMetrics

logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")


class _CallbackMonitor:
    def __init__(self, threshold: float | None = 1.0) -> None:
        if threshold is not None:
            _require_positive_finite_number("slow_callback_threshold", threshold)
        self._threshold = threshold
        self._lock = Lock()
        self._active: dict[object, tuple[str, str, str | None, float]] = {}
        self._slow_callbacks = 0
        self._peak_seconds = 0.0

    def invoke(
        self,
        function: Callable[[], ResultT],
        *,
        event_name: str,
        callback: str,
        context: str | None = None,
    ) -> ResultT:
        token = object()
        started = monotonic()
        with self._lock:
            self._active[token] = (event_name, callback, context, started)
        try:
            return function()
        finally:
            elapsed = max(0.0, monotonic() - started)
            slow = self._threshold is not None and elapsed >= self._threshold
            with self._lock:
                del self._active[token]
                self._peak_seconds = max(self._peak_seconds, elapsed)
                self._slow_callbacks += int(slow)
            if slow:
                logger.warning(
                    "Slow runtime callback finished; event_name=%s callback=%s "
                    "context=%s elapsed_seconds=%.6f threshold_seconds=%s",
                    event_name,
                    callback,
                    context,
                    elapsed,
                    self._threshold,
                    extra={
                        "event_name": event_name,
                        "callback": callback,
                        "context": context,
                        "elapsed_seconds": elapsed,
                    },
                )

    def snapshot(self) -> tuple[tuple[ActiveCallbackMetrics, ...], int, float]:
        with self._lock:
            now = monotonic()
            active = tuple(
                ActiveCallbackMetrics(event_name, callback, context, max(0.0, now - started))
                for event_name, callback, context, started in self._active.values()
            )
            return active, self._slow_callbacks, self._peak_seconds

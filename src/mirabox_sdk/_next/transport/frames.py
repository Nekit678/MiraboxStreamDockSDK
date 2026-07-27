"""Transport frame models and completion handles."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import InvalidStateError
from dataclasses import dataclass
from threading import Event, Lock
from typing import TypeAlias

TextFrame: TypeAlias = str

logger = logging.getLogger(__name__)


class TransportReceipt:
    """Completion handle for one attempted transport send.

    The connector completes a receipt exactly once. Consumers can only observe
    completion through :meth:`result`, :meth:`exception`, and :meth:`done`.
    """

    __slots__ = ("_callbacks", "_done", "_error", "_lock", "_waiter")

    def __init__(self) -> None:
        self._lock = Lock()
        self._done = False
        self._error: Exception | None = None
        self._waiter: Event | None = None
        self._callbacks: (
            Callable[[Exception | None], None] | list[Callable[[Exception | None], None]] | None
        ) = None

    def done(self) -> bool:
        """Return whether the transport attempt reached a terminal state."""

        with self._lock:
            return self._done

    def result(self, timeout: float | None = None) -> None:
        """Wait for completion and re-raise the recorded transport failure."""

        error = self._wait(timeout)
        if error is not None:
            raise error
        return None

    def exception(self, timeout: float | None = None) -> Exception | None:
        """Wait for completion and return the recorded failure, if any."""

        return self._wait(timeout)

    def _finish(self, *, error: Exception | None = None) -> None:
        """Complete the receipt once for internal transport components."""

        with self._lock:
            if self._done:
                raise InvalidStateError("TransportReceipt is already finished")
            self._done = True
            self._error = error
            waiter = self._waiter
            callbacks = self._callbacks
            self._callbacks = None

        if waiter is not None:
            waiter.set()
        if callbacks is None:
            return
        if callable(callbacks):
            self._invoke_callback(callbacks, error)
            return
        for callback in callbacks:
            self._invoke_callback(callback, error)

    def _add_done_callback(
        self,
        callback: Callable[[Exception | None], None],
    ) -> None:
        """Notify an internal boundary component when transport finishes."""

        with self._lock:
            if not self._done:
                callbacks = self._callbacks
                if callbacks is None:
                    self._callbacks = callback
                elif callable(callbacks):
                    self._callbacks = [callbacks, callback]
                else:
                    callbacks.append(callback)
                return
            error = self._error

        self._invoke_callback(callback, error)

    def _wait(self, timeout: float | None) -> Exception | None:
        with self._lock:
            if self._done:
                return self._error
            waiter = self._waiter
            if waiter is None:
                waiter = Event()
                self._waiter = waiter

        if not waiter.wait(timeout):
            with self._lock:
                if not self._done:
                    raise TimeoutError()

        with self._lock:
            return self._error

    @staticmethod
    def _invoke_callback(
        callback: Callable[[Exception | None], None],
        error: Exception | None,
    ) -> None:
        try:
            callback(error)
        except Exception:
            logger.exception("Transport receipt callback failed")


@dataclass(slots=True)
class OutboundFrame:
    """One serialized text frame and its transport completion handle."""

    payload: TextFrame
    receipt: TransportReceipt

"""Transport frame models and completion handles."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TypeAlias

TextFrame: TypeAlias = str


class TransportReceipt:
    """Completion handle for one attempted transport send.

    The connector completes a receipt exactly once. Consumers can only observe
    completion through :meth:`result`, :meth:`exception`, and :meth:`done`.
    """

    __slots__ = ("_future",)

    def __init__(self) -> None:
        self._future: Future[None] = Future()

    def done(self) -> bool:
        """Return whether the transport attempt reached a terminal state."""

        return self._future.done()

    def result(self, timeout: float | None = None) -> None:
        """Wait for completion and re-raise the recorded transport failure."""

        return self._future.result(timeout)

    def exception(self, timeout: float | None = None) -> Exception | None:
        """Wait for completion and return the recorded failure, if any."""

        return self._future.exception(timeout)

    def _finish(self, *, error: Exception | None = None) -> None:
        """Complete the receipt once for internal transport components."""

        if error is None:
            self._future.set_result(None)
        else:
            self._future.set_exception(error)

    def _add_done_callback(
        self,
        callback: Callable[[Exception | None], None],
    ) -> None:
        """Notify an internal boundary component when transport finishes."""

        self._future.add_done_callback(lambda future: callback(future.exception()))


@dataclass(slots=True)
class OutboundFrame:
    """One serialized text frame and its transport completion handle."""

    payload: TextFrame
    receipt: TransportReceipt

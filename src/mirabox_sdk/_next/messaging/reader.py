"""Sequential worker for decoding inbound transport frames."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import isfinite
from threading import Condition, Thread, current_thread
from time import monotonic

from ...events import UnknownStreamDockEvent
from ..protocol.ports import StreamDockEventDecoder
from ..transport.ports import RawInboundSource
from ..transport.queues import TransportQueueClosedError
from .ports import InboundEventSink

logger = logging.getLogger(__name__)

_RECEIVE_POLL_INTERVAL = 0.05


class EventReaderLifecycleError(RuntimeError):
    """Report an invalid EventReader lifecycle transition."""


@dataclass(frozen=True, slots=True)
class EventReaderMetrics:
    """Immutable point-in-time counters for the inbound reader."""

    frames_received: int
    decoded: int
    submitted: int
    rejected: int
    protocol_failures: int
    unknown_events: int
    sink_failures: int


class EventReader:
    """Decode raw frames in wire order on one dedicated worker thread."""

    def __init__(
        self,
        raw_inbound_source: RawInboundSource,
        decoder: StreamDockEventDecoder,
        inbound_event_sink: InboundEventSink,
    ) -> None:
        self._source = raw_inbound_source
        self._decoder = decoder
        self._sink = inbound_event_sink
        self._condition = Condition()
        self._thread: Thread | None = None
        self._started = False
        self._stop_requested = False
        self._stopped = False
        self._source_idle = False
        self._in_flight = False

        self._frames_received = 0
        self._decoded = 0
        self._submitted = 0
        self._rejected = 0
        self._protocol_failures = 0
        self._unknown_events = 0
        self._sink_failures = 0

    def start(self) -> None:
        """Start the reader once; repeated calls while running are harmless."""

        with self._condition:
            if self._started and not self._stopped:
                return
            if self._stopped or self._stop_requested:
                raise EventReaderLifecycleError("Event reader has already been stopped")

            thread = Thread(
                target=self._run,
                name="mirabox-next-event-reader",
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

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until the source is observed empty and no frame is in flight.

        Producers should be quiesced before calling this method. Otherwise a new
        frame may arrive after the idle observation.
        """

        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while not self._is_drained_locked():
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self, *, timeout: float | None = None) -> bool:
        """Request a graceful stop after already available frames are handled."""

        timeout = _validate_timeout(timeout)
        with self._condition:
            if self._stopped:
                return True
            self._stop_requested = True
            thread = self._thread
            if thread is None:
                self._stopped = True
                self._source_idle = True
                self._condition.notify_all()
                return True
            called_from_worker = thread is current_thread()

        self._stop_accepting(self._source, "Inbound frame source")
        if called_from_worker:
            return True
        thread.join(timeout)
        if thread.is_alive() and timeout is not None:
            self._stop_accepting(self._sink, "Inbound event sink")
            thread.join(_RECEIVE_POLL_INTERVAL * 2)
        return not thread.is_alive()

    def metrics(self) -> EventReaderMetrics:
        """Return an atomic immutable snapshot of reader counters."""

        with self._condition:
            return EventReaderMetrics(
                frames_received=self._frames_received,
                decoded=self._decoded,
                submitted=self._submitted,
                rejected=self._rejected,
                protocol_failures=self._protocol_failures,
                unknown_events=self._unknown_events,
                sink_failures=self._sink_failures,
            )

    def _run(self) -> None:
        try:
            while True:
                try:
                    frame = self._source.receive(timeout=_RECEIVE_POLL_INTERVAL)
                except TimeoutError:
                    with self._condition:
                        self._source_idle = True
                        self._condition.notify_all()
                        if self._stop_requested:
                            return
                    continue
                except TransportQueueClosedError:
                    with self._condition:
                        self._source_idle = True
                        self._condition.notify_all()
                    return
                except Exception as exc:
                    logger.error(
                        "Inbound frame source failed with %s",
                        type(exc).__name__,
                    )
                    with self._condition:
                        self._source_idle = True
                        self._condition.notify_all()
                    return

                with self._condition:
                    self._source_idle = False
                    self._frames_received += 1
                    self._in_flight = True
                    self._condition.notify_all()

                try:
                    self._process(frame)
                finally:
                    with self._condition:
                        self._in_flight = False
                        self._condition.notify_all()
        finally:
            with self._condition:
                self._stopped = True
                self._source_idle = True
                self._in_flight = False
                self._condition.notify_all()

    def _process(self, frame: str) -> None:
        try:
            event = self._decoder.decode(frame)
        except Exception as exc:
            logger.warning(
                "Discarded invalid inbound Stream Dock frame (%s)",
                type(exc).__name__,
            )
            with self._condition:
                self._protocol_failures += 1
            return

        with self._condition:
            self._decoded += 1
            if isinstance(event, UnknownStreamDockEvent):
                self._unknown_events += 1

        try:
            accepted = self._sink.submit(event)
        except Exception as exc:
            logger.error(
                "Inbound event sink failed for %s (%s)",
                type(event).__name__,
                type(exc).__name__,
            )
            with self._condition:
                self._sink_failures += 1
            return

        with self._condition:
            if accepted:
                self._submitted += 1
            else:
                self._rejected += 1

    def _is_drained_locked(self) -> bool:
        if self._stopped:
            return not self._in_flight
        return self._started and self._source_idle and not self._in_flight

    @staticmethod
    def _stop_accepting(port: object, port_name: str) -> None:
        stop_accepting = getattr(port, "stop_accepting", None)
        if not callable(stop_accepting):
            return
        try:
            stop_accepting()
        except Exception as exc:
            logger.error(
                "%s shutdown failed with %s",
                port_name,
                type(exc).__name__,
            )


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

"""Sequential worker for encoding outbound Stream Dock commands."""

from __future__ import annotations

import logging
from concurrent.futures import InvalidStateError
from math import isfinite
from threading import Condition, Thread, current_thread
from time import monotonic

from ..protocol.ports import StreamDockCommandEncoder
from ..transport.frames import OutboundFrame, TransportReceipt
from ..transport.ports import (
    QueueAcceptanceControl as TransportQueueAcceptanceControl,
)
from ..transport.ports import RawOutboundSink
from .metrics import CommandWriterMetrics
from .models import CommandFuture, CommandSubmission
from .outbound import OutboundCommandQueueClosedError
from .ports import (
    CommandWriterWorker,
    OutboundCommandSource,
)
from .ports import (
    QueueAcceptanceControl as MessagingQueueAcceptanceControl,
)

logger = logging.getLogger(__name__)

_RECEIVE_POLL_INTERVAL = 0.05


class CommandWriterError(RuntimeError):
    """Base error raised by the outbound command worker."""


class CommandWriterLifecycleError(CommandWriterError):
    """Report an invalid CommandWriter lifecycle transition."""


class RawOutboundRejectedError(CommandWriterError):
    """Report that a serialized frame was refused by the transport queue."""


class CommandWriterStoppedError(CommandWriterError):
    """Report a command whose transport completion outlived the writer."""


class CommandWriter(CommandWriterWorker):
    """Encode accepted commands in FIFO order and bridge transport receipts."""

    def __init__(
        self,
        command_source: OutboundCommandSource,
        encoder: StreamDockCommandEncoder,
        raw_outbound_sink: RawOutboundSink,
    ) -> None:
        self._source = command_source
        self._encoder = encoder
        self._sink = raw_outbound_sink
        self._condition = Condition()
        self._thread: Thread | None = None
        self._started = False
        self._stop_requested = False
        self._stopped = False
        self._source_idle = False
        self._in_flight: CommandSubmission | None = None
        self._pending: dict[TransportReceipt, CommandFuture] = {}

        self._commands_received = 0
        self._serialized = 0
        self._frames_enqueued = 0
        self._serialization_failures = 0
        self._raw_outbound_failures = 0
        self._completed = 0
        self._completion_failures = 0
        self._discarded_during_shutdown = 0

    def start(self) -> None:
        """Start the writer once; repeated calls while running are harmless."""

        with self._condition:
            if self._started and not self._stopped:
                return
            if self._stopped or self._stop_requested:
                raise CommandWriterLifecycleError("Command writer has already been stopped")

            thread = Thread(
                target=self._run,
                name="mirabox-next-command-writer",
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
        """Wait for source idle and terminal completion of submitted frames."""

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
        """Stop after queued commands are handled and fail pending receipts."""

        timeout = _validate_timeout(timeout)
        with self._condition:
            if self._stopped:
                self._fail_pending_locked()
                return True
            self._stop_requested = True
            thread = self._thread
            if thread is None:
                self._stopped = True
                self._source_idle = True
                self._condition.notify_all()
                return True
            called_from_worker = thread is current_thread()

        self._stop_accepting(self._source, "Outbound command source")
        if called_from_worker:
            return True
        thread.join(timeout)
        if thread.is_alive() and timeout is not None:
            self._stop_accepting(self._sink, "Raw outbound sink")
            thread.join(_RECEIVE_POLL_INTERVAL * 2)
        return not thread.is_alive()

    def metrics(self) -> CommandWriterMetrics:
        """Return an atomic immutable snapshot of writer counters."""

        with self._condition:
            return CommandWriterMetrics(
                commands_received=self._commands_received,
                serialized=self._serialized,
                frames_enqueued=self._frames_enqueued,
                serialization_failures=self._serialization_failures,
                raw_outbound_failures=self._raw_outbound_failures,
                completed=self._completed,
                completion_failures=self._completion_failures,
                discarded_during_shutdown=self._discarded_during_shutdown,
            )

    def _run(self) -> None:
        try:
            while True:
                try:
                    submission = self._source.receive(timeout=_RECEIVE_POLL_INTERVAL)
                except TimeoutError:
                    with self._condition:
                        self._source_idle = True
                        self._condition.notify_all()
                        if self._stop_requested:
                            return
                    continue
                except OutboundCommandQueueClosedError:
                    with self._condition:
                        self._source_idle = True
                        self._condition.notify_all()
                    return
                except Exception as exc:
                    logger.error(
                        "Outbound command source failed with %s",
                        type(exc).__name__,
                    )
                    with self._condition:
                        self._source_idle = True
                        self._condition.notify_all()
                    return

                with self._condition:
                    self._source_idle = False
                    self._commands_received += 1
                    self._in_flight = submission
                    self._condition.notify_all()

                try:
                    self._process(submission)
                finally:
                    with self._condition:
                        self._in_flight = None
                        self._condition.notify_all()
        finally:
            with self._condition:
                if self._stop_requested:
                    self._fail_pending_locked()
                self._stopped = True
                self._source_idle = True
                self._in_flight = None
                self._condition.notify_all()

    def _process(self, submission: CommandSubmission) -> None:
        try:
            payload = self._encoder.encode(submission.command)
        except Exception as exc:
            logger.error(
                "Failed to serialize outbound %s (%s)",
                type(submission.command).__name__,
                type(exc).__name__,
            )
            with self._condition:
                self._serialization_failures += 1
            self._finish_completion(submission.completion, error=exc)
            return

        with self._condition:
            self._serialized += 1

        receipt = TransportReceipt()
        completion = submission.completion
        with self._condition:
            self._pending[receipt] = completion
        receipt._add_done_callback(lambda error: self._complete_receipt(receipt, completion, error))
        frame = OutboundFrame(payload, receipt)

        try:
            accepted = self._sink.submit(frame)
        except Exception as exc:
            logger.error(
                "Raw outbound sink failed for %s (%s)",
                type(submission.command).__name__,
                type(exc).__name__,
            )
            with self._condition:
                self._raw_outbound_failures += 1
            self._finish_receipt(receipt, error=exc)
            return

        with self._condition:
            if accepted:
                self._frames_enqueued += 1
            else:
                self._raw_outbound_failures += 1

        if not accepted:
            self._finish_receipt(
                receipt,
                error=RawOutboundRejectedError("Raw outbound queue rejected serialized frame"),
            )

    def _complete_receipt(
        self,
        receipt: TransportReceipt,
        completion: CommandFuture,
        error: Exception | None,
    ) -> None:
        with self._condition:
            self._pending.pop(receipt, None)
        self._finish_completion(completion, error=error)
        with self._condition:
            self._condition.notify_all()

    def _finish_completion(
        self,
        completion: CommandFuture,
        *,
        error: Exception | None,
    ) -> None:
        try:
            completion._finish(error=error)
        except InvalidStateError:
            return

        with self._condition:
            if error is None:
                self._completed += 1
            else:
                self._completion_failures += 1
            self._condition.notify_all()

    @staticmethod
    def _finish_receipt(
        receipt: TransportReceipt,
        *,
        error: Exception,
    ) -> None:
        try:
            receipt._finish(error=error)
        except InvalidStateError:
            pass

    def _fail_pending_locked(self) -> None:
        pending = tuple(self._pending.items())
        self._discarded_during_shutdown += len(pending)
        error = CommandWriterStoppedError(
            "Command writer stopped before transport receipt completed"
        )
        for receipt, completion in pending:
            self._pending.pop(receipt, None)
            self._finish_completion(completion, error=error)

    def _is_drained_locked(self) -> bool:
        if self._stopped:
            return self._in_flight is None and not self._pending
        return self._started and self._source_idle and self._in_flight is None and not self._pending

    @staticmethod
    def _stop_accepting(port: object, port_name: str) -> None:
        if not isinstance(
            port,
            (MessagingQueueAcceptanceControl, TransportQueueAcceptanceControl),
        ):
            return
        try:
            port.stop_accepting()
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

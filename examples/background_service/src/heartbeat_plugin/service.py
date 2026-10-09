"""A plugin-owned worker with readiness, cancellation and bounded shutdown."""

from __future__ import annotations

import logging
from threading import Event, Lock, Thread
from time import monotonic

from mirabox_sdk import ApplicationContext, LogMessageCommand, OutboundCommandBusClosedError

logger = logging.getLogger("mirabox_sdk.examples.heartbeat")
POLL_INTERVAL = 0.05
COMMAND_TIMEOUT = 0.25
JOIN_TIMEOUT = 1.0
HEARTBEAT_INTERVAL = 1.0


class HeartbeatService:
    """Send periodic log messages; retain worker failures for application cleanup.

    A worker uses the shared sender directly, never an action's mutable state.
    ``stop()`` also works before session readiness or independently of SDK stop.
    A failed join retains ownership of the thread; it never pretends to kill it.
    """

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context
        self._local_stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._failure: BaseException | None = None
        self.finished = Event()

    @property
    def failure(self) -> BaseException | None:
        with self._lock:
            return self._failure

    def start(self) -> None:
        # Services start before the runtime; waiting for readiness here deadlocks.
        with self._lock:
            if self._thread is not None or self._local_stop.is_set():
                raise RuntimeError("Heartbeat service can only start once before stop")
            thread = Thread(target=self._run, name="plugin-heartbeat", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                # The SDK does not call stop() for a service whose start() failed.
                self._thread = None
                self._local_stop.set()
                self.finished.set()
                raise

    def stop(self) -> None:
        self._local_stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(JOIN_TIMEOUT)
            if thread.is_alive():
                raise TimeoutError("Heartbeat worker is still running; resources remain owned")
        failure = self.failure
        if failure is not None:
            # SDK records this in shutdown_outcome.cleanup_failures and preserves the cause.
            raise RuntimeError("Heartbeat worker failed") from failure

    def _cancelled(self) -> bool:
        return self._local_stop.is_set() or self._context.stop_signal.requested

    def _pause(self) -> None:
        deadline = monotonic() + HEARTBEAT_INTERVAL
        while not self._cancelled():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            self._local_stop.wait(min(POLL_INTERVAL, remaining))

    def _run(self) -> None:
        try:
            readiness = self._context.session_readiness
            while not self._cancelled() and not readiness.ready:
                if readiness.terminal:
                    if readiness.failure is not None:
                        raise readiness.failure
                    return
                readiness.wait(POLL_INTERVAL)

            count = 0
            while not self._cancelled():
                count += 1
                try:
                    completion = self._context.stream_dock.send_async(
                        LogMessageCommand(message=f"Heartbeat {count}")
                    )
                    completion.result(timeout=COMMAND_TIMEOUT)
                except OutboundCommandBusClosedError:
                    # Only a closed bus during requested cancellation is expected.
                    if not self._cancelled():
                        raise
                    return
                self._pause()
        except BaseException as exc:
            with self._lock:
                self._failure = exc
            logger.error("Heartbeat worker failed; exception_type=%s", type(exc).__name__)
        finally:
            self.finished.set()

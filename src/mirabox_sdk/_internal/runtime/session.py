"""Single-session initialization policy and readiness coordination."""

from __future__ import annotations

import logging
from math import isfinite
from threading import Condition, Lock

from ...commands import GetGlobalSettingsCommand, RegisterPluginCommand
from ..transport.session import Connected, Disconnected, SessionEvent, TransportError
from .metrics import SessionCoordinatorMetrics
from .models import SessionState, transition_session_state
from .ports import SessionEventCoordinator, SessionReadiness, StreamDockSender

logger = logging.getLogger(__name__)


class SessionReadinessStateError(RuntimeError):
    """Report an attempt to open a terminal readiness gate."""


class SessionReadinessGate(SessionReadiness):
    """One-way readiness latch with explicit pre-readiness termination."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._ready = False
        self._terminal = False
        self._failure: Exception | None = None

    @property
    def ready(self) -> bool:
        with self._condition:
            return self._ready

    @property
    def terminal(self) -> bool:
        with self._condition:
            return self._terminal

    @property
    def failure(self) -> Exception | None:
        with self._condition:
            return self._failure

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for readiness or terminal state and return readiness status."""

        timeout = _validate_timeout(timeout)
        with self._condition:
            self._condition.wait_for(
                lambda: self._ready or self._terminal,
                timeout=timeout,
            )
            return self._ready

    def open(self) -> None:
        """Permanently open the latch after successful initialization."""

        with self._condition:
            if self._ready:
                return
            if self._terminal:
                raise SessionReadinessStateError("terminal readiness cannot be opened")
            self._ready = True
            self._condition.notify_all()

    def close(self) -> None:
        """Unblock waiters when a not-yet-ready session ends normally."""

        with self._condition:
            self._terminal = True
            self._condition.notify_all()

    def fail(self, error: Exception) -> None:
        """Unblock waiters with the first fatal pre-readiness failure."""

        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception")
        with self._condition:
            if self._ready or self._terminal:
                return
            self._failure = error
            self._terminal = True
            self._condition.notify_all()


class SessionCoordinator(SessionEventCoordinator):
    """Initialize and track one typed Stream Dock boundary session."""

    def __init__(
        self,
        sender: StreamDockSender,
        *,
        register_event: str,
        plugin_uuid: str,
        readiness: SessionReadinessGate | None = None,
    ) -> None:
        if not isinstance(sender, StreamDockSender):
            raise TypeError("sender must implement StreamDockSender")
        if not isinstance(register_event, str) or not register_event.strip():
            raise ValueError("register_event must be a non-empty string")
        if not isinstance(plugin_uuid, str) or not plugin_uuid.strip():
            raise ValueError("plugin_uuid must be a non-empty string")
        if readiness is not None and not isinstance(readiness, SessionReadinessGate):
            raise TypeError("readiness must be a SessionReadinessGate or None")

        self._sender = sender
        self._register_event = register_event
        self._plugin_uuid = plugin_uuid
        self._readiness = readiness or SessionReadinessGate()
        self._condition = Condition()
        self._event_lock = Lock()
        self._state = SessionState.WAITING_CONNECTED
        self._last_close_reason: str | None = None

        self._events_received = 0
        self._connected = 0
        self._invalid_transitions = 0
        self._initialization_started = 0
        self._initialization_succeeded = 0
        self._initialization_failed = 0
        self._registration_failures = 0
        self._initial_settings_request_failures = 0
        self._disconnected = 0
        self._last_close_code: int | None = None
        self._transport_errors = 0
        self._source_poll_timeouts = 0
        self._source_closed = 0

    @property
    def state(self) -> SessionState:
        with self._condition:
            return self._state

    @property
    def readiness(self) -> SessionReadinessGate:
        return self._readiness

    @property
    def last_close_reason(self) -> str | None:
        """Return the last close reason without writing it to diagnostics."""

        with self._condition:
            return self._last_close_reason

    def handle(self, event: SessionEvent) -> None:
        """Apply one typed session event in source FIFO order."""

        if not isinstance(event, SessionEvent):
            raise TypeError("event must be a SessionEvent")

        with self._event_lock:
            with self._condition:
                self._events_received += 1

            if isinstance(event, Connected):
                self._handle_connected()
            elif isinstance(event, Disconnected):
                self._handle_disconnected(event)
            elif isinstance(event, TransportError):
                self._handle_transport_error(event)
            else:
                with self._condition:
                    self._invalid_transitions += 1
                    state = self._state
                logger.warning(
                    "Ignoring unsupported session event; event_type=%s session_state=%s",
                    type(event).__name__,
                    state.value,
                )

    def handle_event(self, event: SessionEvent) -> None:
        """Compatibility spelling for callers that name the handled value."""

        self.handle(event)

    def record_source_poll_timeout(self) -> None:
        with self._condition:
            self._source_poll_timeouts += 1

    def record_source_closed(self) -> None:
        with self._condition:
            self._source_closed += 1
        self._readiness.close()

    def fail_readiness(self, error: Exception) -> None:
        self._readiness.fail(error)

    def metrics(self) -> SessionCoordinatorMetrics:
        """Return an immutable point-in-time session snapshot."""

        with self._condition:
            return SessionCoordinatorMetrics(
                events_received=self._events_received,
                connected=self._connected,
                invalid_transitions=self._invalid_transitions,
                initialization_started=self._initialization_started,
                initialization_succeeded=self._initialization_succeeded,
                initialization_failed=self._initialization_failed,
                registration_failures=self._registration_failures,
                initial_settings_request_failures=self._initial_settings_request_failures,
                disconnected=self._disconnected,
                last_close_code=self._last_close_code,
                transport_errors=self._transport_errors,
                source_poll_timeouts=self._source_poll_timeouts,
                source_closed=self._source_closed,
            )

    def _handle_connected(self) -> None:
        with self._condition:
            self._connected += 1
            if self._state is not SessionState.WAITING_CONNECTED or self._readiness.terminal:
                self._invalid_transitions += 1
                state = self._state
                logger.warning(
                    "Ignoring invalid session transition; event_type=Connected session_state=%s",
                    state.value,
                )
                return
            self._state = transition_session_state(
                self._state,
                SessionState.INITIALIZING,
            )
            self._initialization_started += 1

        try:
            self._sender.send(RegisterPluginCommand(self._register_event, self._plugin_uuid))
        except Exception as exc:
            self._record_initialization_failure(exc, phase="registration")
            raise

        try:
            self._sender.send(GetGlobalSettingsCommand(self._plugin_uuid))
        except Exception as exc:
            self._record_initialization_failure(exc, phase="initial_settings_request")
            raise

        with self._condition:
            self._state = transition_session_state(self._state, SessionState.READY)
            self._initialization_succeeded += 1
        self._readiness.open()

    def _record_initialization_failure(self, error: Exception, *, phase: str) -> None:
        with self._condition:
            self._initialization_failed += 1
            if phase == "registration":
                self._registration_failures += 1
            else:
                self._initial_settings_request_failures += 1
            self._state = transition_session_state(self._state, SessionState.FAILED)
        self._readiness.fail(error)
        logger.error(
            "Mandatory session initialization failed; phase=%s exception_type=%s",
            phase,
            type(error).__name__,
        )

    def _handle_disconnected(self, event: Disconnected) -> None:
        with self._condition:
            self._disconnected += 1
            self._last_close_code = event.status_code
            self._last_close_reason = event.reason
            if self._state is SessionState.READY:
                self._state = transition_session_state(
                    self._state,
                    SessionState.DISCONNECTED,
                )
            else:
                self._invalid_transitions += 1
                state = self._state
                logger.warning(
                    "Observed disconnect before a valid ready transition; session_state=%s",
                    state.value,
                )
        self._readiness.close()
        logger.info("Stream Dock session disconnected; status_code=%s", event.status_code)

    def _handle_transport_error(self, event: TransportError) -> None:
        with self._condition:
            self._transport_errors += 1
        logger.error(
            "Stream Dock transport error observed; exception_type=%s",
            type(event.error).__name__,
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
        raise ValueError("timeout must be a non-negative finite number or None")
    return float(timeout)

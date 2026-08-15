"""Pure state and dispatch-result models for the runtime dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class RuntimeSchedulerKind(StrEnum):
    """Available runtime scheduling policies."""

    SEQUENTIAL = "sequential"
    KEYED_SERIAL = "keyed_serial"


class RuntimeLifecycleState(StrEnum):
    """Lifecycle state of one single-run runtime instance."""

    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        """Return whether no further lifecycle transition is allowed."""

        return self in (type(self).STOPPED, type(self).FAILED)


class SessionState(StrEnum):
    """Initialization state of one boundary session."""

    WAITING_CONNECTED = "waiting_connected"
    INITIALIZING = "initializing"
    READY = "ready"
    DISCONNECTED = "disconnected"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        """Return whether the session cannot become ready again."""

        return self in (type(self).DISCONNECTED, type(self).FAILED)


class DispatchOutcome(StrEnum):
    """Terminal ownership outcome for one accepted inbound event."""

    HANDLED = "handled"
    IGNORED = "ignored"
    CALLBACK_FAILED = "callback_failed"
    DISCARDED_DURING_SHUTDOWN = "discarded_during_shutdown"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Terminal dispatch outcome and the original failure, when present."""

    outcome: DispatchOutcome
    error: Exception | None = None


class InvalidRuntimeStateTransitionError(RuntimeError):
    """Report a transition outside the runtime lifecycle graph."""

    def __init__(
        self,
        current: RuntimeLifecycleState,
        target: RuntimeLifecycleState,
    ) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid runtime state transition: {current.value} -> {target.value}")


class InvalidSessionStateTransitionError(RuntimeError):
    """Report a transition outside the single-session state graph."""

    def __init__(self, current: SessionState, target: SessionState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid session state transition: {current.value} -> {target.value}")


_RUNTIME_TRANSITIONS: Final = MappingProxyType(
    {
        RuntimeLifecycleState.NEW: frozenset(
            (RuntimeLifecycleState.STARTING, RuntimeLifecycleState.STOPPED)
        ),
        RuntimeLifecycleState.STARTING: frozenset(
            (
                RuntimeLifecycleState.RUNNING,
                RuntimeLifecycleState.STOPPING,
                RuntimeLifecycleState.FAILED,
            )
        ),
        RuntimeLifecycleState.RUNNING: frozenset(
            (RuntimeLifecycleState.STOPPING, RuntimeLifecycleState.FAILED)
        ),
        RuntimeLifecycleState.STOPPING: frozenset(
            (RuntimeLifecycleState.STOPPED, RuntimeLifecycleState.FAILED)
        ),
        RuntimeLifecycleState.STOPPED: frozenset(),
        RuntimeLifecycleState.FAILED: frozenset(),
    }
)

_SESSION_TRANSITIONS: Final = MappingProxyType(
    {
        SessionState.WAITING_CONNECTED: frozenset((SessionState.INITIALIZING,)),
        SessionState.INITIALIZING: frozenset((SessionState.READY, SessionState.FAILED)),
        SessionState.READY: frozenset((SessionState.DISCONNECTED,)),
        SessionState.DISCONNECTED: frozenset(),
        SessionState.FAILED: frozenset(),
    }
)


def transition_runtime_state(
    current: RuntimeLifecycleState,
    target: RuntimeLifecycleState,
) -> RuntimeLifecycleState:
    """Validate and return one runtime lifecycle transition."""

    if not isinstance(current, RuntimeLifecycleState):
        raise TypeError("current must be a RuntimeLifecycleState")
    if not isinstance(target, RuntimeLifecycleState):
        raise TypeError("target must be a RuntimeLifecycleState")
    if target not in _RUNTIME_TRANSITIONS[current]:
        raise InvalidRuntimeStateTransitionError(current, target)
    return target


def transition_session_state(current: SessionState, target: SessionState) -> SessionState:
    """Validate and return one single-session state transition."""

    if not isinstance(current, SessionState):
        raise TypeError("current must be a SessionState")
    if not isinstance(target, SessionState):
        raise TypeError("target must be a SessionState")
    if target not in _SESSION_TRANSITIONS[current]:
        raise InvalidSessionStateTransitionError(current, target)
    return target

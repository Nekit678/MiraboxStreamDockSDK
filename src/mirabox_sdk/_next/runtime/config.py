"""Configuration models for the Stream Dock runtime dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .models import RuntimeSchedulerKind


def _require_positive_finite_number(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")


def _require_positive_integer(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_optional_timeout(name: str, value: object) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative finite number or None")


@dataclass(frozen=True, slots=True)
class RuntimeDispatcherConfig:
    """Immutable limits and timeouts owned by the runtime dispatcher."""

    session_poll_interval: float = 0.05
    event_poll_interval: float = 0.05
    scheduler_kind: RuntimeSchedulerKind = RuntimeSchedulerKind.KEYED_SERIAL
    worker_count: int = 4
    scheduler_pending_limit: int = 64
    runtime_drain_timeout: float | None = 5.0
    worker_stop_timeout: float | None = 5.0
    callback_timeout: float | None = None

    def __post_init__(self) -> None:
        _require_positive_finite_number("session_poll_interval", self.session_poll_interval)
        _require_positive_finite_number("event_poll_interval", self.event_poll_interval)

        if not isinstance(self.scheduler_kind, RuntimeSchedulerKind):
            raise ValueError("scheduler_kind must be a RuntimeSchedulerKind")

        _require_positive_integer("worker_count", self.worker_count)
        _require_positive_integer("scheduler_pending_limit", self.scheduler_pending_limit)

        _require_optional_timeout("runtime_drain_timeout", self.runtime_drain_timeout)
        _require_optional_timeout("worker_stop_timeout", self.worker_stop_timeout)
        _require_optional_timeout("callback_timeout", self.callback_timeout)

        if self.scheduler_kind is RuntimeSchedulerKind.SEQUENTIAL and self.worker_count != 1:
            raise ValueError("sequential scheduler requires worker_count == 1")

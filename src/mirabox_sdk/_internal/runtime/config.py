"""Configuration models for the Stream Dock runtime dispatcher."""

from __future__ import annotations

import warnings
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
    """Immutable runtime limits. Shutdown waits share ``shutdown_timeout``.

    ``callback_drain_timeout`` only bounds shutdown drain; it does not monitor
    callbacks during normal dispatch. ``callback_timeout`` is its deprecated alias.
    """

    session_poll_interval: float = 0.05
    event_poll_interval: float = 0.05
    scheduler_kind: RuntimeSchedulerKind = RuntimeSchedulerKind.KEYED_SERIAL
    worker_count: int = 4
    scheduler_pending_limit: int = 64
    runtime_drain_timeout: float | None = 5.0
    worker_stop_timeout: float | None = 5.0
    callback_timeout: float | None = None
    callback_drain_timeout: float | None = None
    shutdown_timeout: float | None = 5.0

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
        _require_optional_timeout("callback_drain_timeout", self.callback_drain_timeout)
        _require_optional_timeout("shutdown_timeout", self.shutdown_timeout)
        if self.callback_timeout is not None:
            if self.callback_drain_timeout is not None:
                raise ValueError(
                    "callback_timeout and callback_drain_timeout are mutually exclusive"
                )
            warnings.warn(
                "callback_timeout is a shutdown wait; use callback_drain_timeout instead",
                DeprecationWarning,
                stacklevel=2,
            )

        if self.scheduler_kind is RuntimeSchedulerKind.SEQUENTIAL and self.worker_count != 1:
            raise ValueError("sequential scheduler requires worker_count == 1")

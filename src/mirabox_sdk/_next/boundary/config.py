"""Configuration models for the experimental boundary."""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite


@dataclass(frozen=True, slots=True)
class BoundaryQueueConfig:
    """Positive capacity limits for every queue owned by the boundary."""

    raw_inbound_limit: int
    inbound_event_limit: int
    outbound_command_limit: int
    raw_outbound_limit: int
    session_event_limit: int

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class BoundaryShutdownConfig:
    """Timeouts applied to each bounded graceful-shutdown stage."""

    raw_inbound_drain_timeout: float | None = 5.0
    inbound_event_drain_timeout: float | None = 5.0
    outbound_command_drain_timeout: float | None = 5.0
    raw_outbound_drain_timeout: float | None = 5.0
    session_event_drain_timeout: float | None = 5.0
    worker_stop_timeout: float | None = 5.0
    connector_stop_timeout: float | None = 5.0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{field.name} must be a non-negative finite number or None")

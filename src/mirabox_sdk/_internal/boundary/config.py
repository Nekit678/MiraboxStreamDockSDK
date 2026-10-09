"""Configuration models for the typed boundary implementation."""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite

from ..transport.buffer_limits import DEFAULT_MAX_MESSAGE_BYTES, DEFAULT_QUEUE_BYTE_LIMIT


@dataclass(frozen=True, slots=True)
class BoundaryQueueConfig:
    """Positive item and byte limits for boundary queues.

    Raw queues count UTF-8 bytes and enforce ``max_message_bytes``. Typed queues
    estimate retained Python DTO/JSON bytes, including COW backing storage.
    Byte budgets cover queued items; consumers release them on receive.
    The session queue has an item limit for transport lifecycle notifications.
    """

    raw_inbound_limit: int
    inbound_event_limit: int
    outbound_command_limit: int
    raw_outbound_limit: int
    session_event_limit: int
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    raw_inbound_byte_limit: int = DEFAULT_QUEUE_BYTE_LIMIT
    inbound_event_byte_limit: int = DEFAULT_QUEUE_BYTE_LIMIT
    outbound_command_byte_limit: int = DEFAULT_QUEUE_BYTE_LIMIT
    raw_outbound_byte_limit: int = DEFAULT_QUEUE_BYTE_LIMIT

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

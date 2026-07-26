"""Configuration models for the experimental boundary."""

from __future__ import annotations

from dataclasses import dataclass, fields


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

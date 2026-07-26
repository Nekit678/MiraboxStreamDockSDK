"""Facade contract and queue configuration for the experimental boundary."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable

from .messaging.inbound import InboundEventSource
from .messaging.outbound import OutboundCommandSink
from .transport.lifecycle import SessionEventSource


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


@runtime_checkable
class StreamDockBoundary(Protocol):
    """Typed facade consumed by the next SDK layer."""

    @property
    @abstractmethod
    def events(self) -> InboundEventSource:
        """Return the typed inbound event source."""

        ...

    @property
    @abstractmethod
    def commands(self) -> OutboundCommandSink:
        """Return the typed outbound command sink."""

        ...

    @property
    @abstractmethod
    def session_events(self) -> SessionEventSource:
        """Return the typed session event source."""

        ...

    @abstractmethod
    def run_forever(self) -> None:
        """Run the composed boundary until the transport closes."""

        ...

    @abstractmethod
    def close(self) -> None:
        """Idempotently request graceful boundary shutdown."""

        ...

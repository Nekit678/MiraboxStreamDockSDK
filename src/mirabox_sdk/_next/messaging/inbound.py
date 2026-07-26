"""Typed inbound event ports."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from ...events import StreamDockEvent


@runtime_checkable
class InboundEventSource(Protocol):
    """Source of decoded Stream Dock events for the next SDK layer."""

    @abstractmethod
    def receive(self) -> StreamDockEvent:
        """Return the next accepted typed event."""

        ...


@runtime_checkable
class InboundEventSink(Protocol):
    """Sink used by the protocol reader to submit decoded events."""

    @abstractmethod
    def submit(self, event: StreamDockEvent) -> bool:
        """Submit one event according to the typed queue policy."""

        ...

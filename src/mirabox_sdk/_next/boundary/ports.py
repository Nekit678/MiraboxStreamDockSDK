"""Ports exposed by the composed Stream Dock boundary."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from ..messaging.ports import InboundEventSource, OutboundCommandSink
from ..transport.ports import SessionEventSource


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

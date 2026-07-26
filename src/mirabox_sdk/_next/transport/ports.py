"""Ports owned by the transport layer."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from .frames import OutboundFrame, TextFrame
from .session import SessionEvent


@runtime_checkable
class RawInboundSource(Protocol):
    """Source of WebSocket text frames for the protocol reader."""

    @abstractmethod
    def receive(self, *, timeout: float | None = None) -> TextFrame:
        """Return the next accepted inbound text frame."""

        ...


@runtime_checkable
class RawInboundSink(Protocol):
    """Sink used by a connector to submit WebSocket text frames."""

    @abstractmethod
    def submit(self, frame: TextFrame) -> bool:
        """Submit one frame, returning whether it was accepted."""

        ...


@runtime_checkable
class RawOutboundSource(Protocol):
    """Source of serialized frames for the transport sender."""

    @abstractmethod
    def receive(self) -> OutboundFrame:
        """Return the next accepted outbound frame."""

        ...


@runtime_checkable
class RawOutboundSink(Protocol):
    """Sink used by the command writer to submit serialized frames."""

    @abstractmethod
    def submit(self, frame: OutboundFrame) -> bool:
        """Submit one frame, returning whether it was accepted."""

        ...


@runtime_checkable
class SessionEventSource(Protocol):
    """Source of typed transport lifecycle events."""

    @abstractmethod
    def receive(self) -> SessionEvent:
        """Return the next lifecycle event."""

        ...


@runtime_checkable
class SessionEventSink(Protocol):
    """Sink used by the connector to publish lifecycle events."""

    @abstractmethod
    def submit(self, event: SessionEvent) -> bool:
        """Submit one lifecycle event, returning whether it was accepted."""

        ...


@runtime_checkable
class WebSocketConnector(Protocol):
    """Lifecycle contract for an API-independent WebSocket connector."""

    @abstractmethod
    def run_forever(self) -> None:
        """Run the WebSocket lifecycle loop until it closes."""

        ...

    @abstractmethod
    def close(self) -> None:
        """Idempotently request connector shutdown."""

        ...

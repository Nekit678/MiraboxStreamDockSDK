"""Ports owned by the transport side of the experimental boundary."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from .frames import OutboundFrame, TextFrame


@runtime_checkable
class RawInboundSource(Protocol):
    """Source of WebSocket text frames for the protocol reader."""

    @abstractmethod
    def receive(self) -> TextFrame:
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

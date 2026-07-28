"""Ports owned by the transport layer."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from .frames import OutboundFrame, TextFrame
from .metrics import TransportQueueMetrics, WebSocketConnectorMetrics
from .session import SessionEvent


class SessionEventSourceClosedError(RuntimeError):
    """Report terminal closure of a typed session event source."""


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
    def submit(self, frame: TextFrame, *, timeout: float | None = None) -> bool:
        """Submit one frame, returning whether it was accepted."""

        ...


@runtime_checkable
class RawOutboundSource(Protocol):
    """Source of serialized frames for the transport sender."""

    @abstractmethod
    def receive(self, *, timeout: float | None = None) -> OutboundFrame:
        """Return the next accepted outbound frame."""

        ...


@runtime_checkable
class RawOutboundSink(Protocol):
    """Sink used by the command writer to submit serialized frames."""

    @abstractmethod
    def submit(self, frame: OutboundFrame, *, timeout: float | None = None) -> bool:
        """Submit one frame, returning whether it was accepted."""

        ...


@runtime_checkable
class SessionEventSource(Protocol):
    """Source of typed transport lifecycle events."""

    @abstractmethod
    def receive(self, *, timeout: float | None = None) -> SessionEvent:
        """Return the next lifecycle event.

        Raises:
            SessionEventSourceClosedError: If the source reached terminal
                closure and has no accepted event left to return.
        """

        ...


@runtime_checkable
class SessionEventSink(Protocol):
    """Sink used by the connector to publish lifecycle events."""

    @abstractmethod
    def submit(self, event: SessionEvent, *, timeout: float | None = None) -> bool:
        """Submit one lifecycle event, returning whether it was accepted."""

        ...


@runtime_checkable
class QueueAcceptanceControl(Protocol):
    """Optional queue capability used to unblock work during shutdown."""

    @abstractmethod
    def stop_accepting(self) -> None:
        """Reject new items while allowing accepted items to drain."""

        ...


@runtime_checkable
class TransportQueueControl(QueueAcceptanceControl, Protocol):
    """Lifecycle and observability contract for a transport queue."""

    @abstractmethod
    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until all accepted items have been received."""

        ...

    @abstractmethod
    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop submissions and drain within the optional timeout."""

        ...

    @abstractmethod
    def metrics(self) -> TransportQueueMetrics:
        """Return an immutable point-in-time queue metrics snapshot."""

        ...


@runtime_checkable
class WebSocketConnector(Protocol):
    """Lifecycle and observability contract for a WebSocket connector."""

    @abstractmethod
    def run_forever(self) -> None:
        """Run the WebSocket lifecycle loop until it closes."""

        ...

    @abstractmethod
    def close(self) -> None:
        """Idempotently request connector shutdown."""

        ...

    @abstractmethod
    def metrics(self) -> WebSocketConnectorMetrics:
        """Return an immutable point-in-time transport metrics snapshot."""

        ...

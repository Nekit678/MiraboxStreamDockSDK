"""Typed messaging ports for the experimental boundary."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from ...commands import StreamDockCommand
from ...events import StreamDockEvent
from .metrics import (
    CommandWriterMetrics,
    EventReaderMetrics,
    InboundEventQueueMetrics,
    OutboundCommandQueueMetrics,
)
from .models import CommandFuture, CommandSubmission


class InboundEventSourceClosedError(RuntimeError):
    """Report terminal closure of a typed inbound event source."""


@runtime_checkable
class InboundEventSource(Protocol):
    """Source of decoded Stream Dock events for the next SDK layer.

    Every successful ``receive`` must be paired with exactly one ``task_done``
    after all handler work for that event has finished.
    """

    @abstractmethod
    def receive(self, *, timeout: float | None = None) -> StreamDockEvent:
        """Return the next accepted typed event for processing.

        Raises:
            InboundEventSourceClosedError: If the source reached terminal
                closure and has no accepted event left to return.
        """

        ...

    @abstractmethod
    def task_done(self) -> None:
        """Acknowledge completed handling of one event returned by ``receive``."""

        ...


@runtime_checkable
class InboundEventSink(Protocol):
    """Sink used by the protocol reader to submit decoded events."""

    @abstractmethod
    def submit(self, event: StreamDockEvent, *, timeout: float | None = None) -> bool:
        """Submit one event according to the typed queue policy."""

        ...


@runtime_checkable
class OutboundCommandSource(Protocol):
    """Source of accepted command submissions for the command writer."""

    @abstractmethod
    def receive(self, *, timeout: float | None = None) -> CommandSubmission:
        """Return the next accepted command submission in FIFO order."""

        ...


@runtime_checkable
class OutboundCommandSink(Protocol):
    """Typed command port exposed to the next SDK layer."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None:
        """Submit a command and wait for its terminal result."""

        ...

    @abstractmethod
    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Submit a command and return its completion handle."""

        ...


@runtime_checkable
class QueueAcceptanceControl(Protocol):
    """Optional queue capability used to unblock work during shutdown."""

    @abstractmethod
    def stop_accepting(self) -> None:
        """Reject new items while allowing accepted items to drain."""

        ...


@runtime_checkable
class InboundEventQueueControl(QueueAcceptanceControl, Protocol):
    """Lifecycle and observability contract for a typed inbound queue."""

    @abstractmethod
    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until all accepted events have been acknowledged."""

        ...

    @abstractmethod
    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop submissions and drain within the optional timeout."""

        ...

    @abstractmethod
    def metrics(self) -> InboundEventQueueMetrics:
        """Return an immutable point-in-time queue metrics snapshot."""

        ...


@runtime_checkable
class OutboundCommandQueueControl(QueueAcceptanceControl, Protocol):
    """Lifecycle and observability contract for a typed outbound queue."""

    @abstractmethod
    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until all accepted commands have been received by a writer."""

        ...

    @abstractmethod
    def shutdown(self, *, timeout: float | None = None) -> bool:
        """Stop submissions and drain within the optional timeout."""

        ...

    @abstractmethod
    def metrics(self) -> OutboundCommandQueueMetrics:
        """Return an immutable point-in-time queue metrics snapshot."""

        ...


@runtime_checkable
class EventReaderWorker(Protocol):
    """Lifecycle and observability contract for an inbound protocol worker."""

    @abstractmethod
    def start(self) -> None:
        """Start the worker."""

        ...

    @abstractmethod
    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until the worker has no available or in-flight frame."""

        ...

    @abstractmethod
    def stop(self, *, timeout: float | None = None) -> bool:
        """Request graceful worker shutdown."""

        ...

    @abstractmethod
    def metrics(self) -> EventReaderMetrics:
        """Return an immutable point-in-time worker metrics snapshot."""

        ...


@runtime_checkable
class CommandWriterWorker(Protocol):
    """Lifecycle and observability contract for an outbound protocol worker."""

    @abstractmethod
    def start(self) -> None:
        """Start the worker."""

        ...

    @abstractmethod
    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until the worker has no pending or in-flight command."""

        ...

    @abstractmethod
    def stop(self, *, timeout: float | None = None) -> bool:
        """Request graceful worker shutdown."""

        ...

    @abstractmethod
    def metrics(self) -> CommandWriterMetrics:
        """Return an immutable point-in-time worker metrics snapshot."""

        ...

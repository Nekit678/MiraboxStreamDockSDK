"""Typed messaging ports for the experimental boundary."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from ...commands import StreamDockCommand
from ...events import StreamDockEvent
from .models import CommandFuture, CommandSubmission


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


@runtime_checkable
class OutboundCommandSource(Protocol):
    """Source of accepted command submissions for the command writer."""

    @abstractmethod
    def receive(self) -> CommandSubmission:
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

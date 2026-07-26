"""Typed transport lifecycle events and their ports."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SessionEvent:
    """Base type for transport lifecycle events."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Connected(SessionEvent):
    """Report that the WebSocket connection opened."""


@dataclass(frozen=True, slots=True)
class Disconnected(SessionEvent):
    """Report that the WebSocket connection closed."""

    status_code: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class TransportError(SessionEvent):
    """Report a transport failure without exposing protocol messages."""

    error: Exception


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

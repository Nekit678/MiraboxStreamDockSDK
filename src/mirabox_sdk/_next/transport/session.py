"""Transport lifecycle models."""

from __future__ import annotations

from dataclasses import dataclass


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

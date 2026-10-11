"""Typed messaging models and completion handles."""

from __future__ import annotations

from dataclasses import dataclass

from ...commands import StreamDockCommand
from ...completion import CommandFuture
from ...events import StreamDockEvent


@dataclass(slots=True)
class SizedEvent:
    """Owned event and its retained size for one queue-to-scheduler transfer.

    The producer must not mutate the payload after admission. Coalescing replaces
    both fields before transfer; no size is cached on the public mutable DTO.
    A size accepted by the source is exact, even for a smaller scheduler budget.
    """

    event: StreamDockEvent
    size: int = 0


@dataclass(slots=True)
class CommandSubmission:
    """One accepted typed command and its completion handle."""

    command: StreamDockCommand
    completion: CommandFuture

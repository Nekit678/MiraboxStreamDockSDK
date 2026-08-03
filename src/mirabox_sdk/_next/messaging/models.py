"""Typed messaging models and completion handles."""

from __future__ import annotations

from dataclasses import dataclass

from ...commands import StreamDockCommand
from ...completion import CommandFuture


@dataclass(slots=True)
class CommandSubmission:
    """One accepted typed command and its completion handle."""

    command: StreamDockCommand
    completion: CommandFuture

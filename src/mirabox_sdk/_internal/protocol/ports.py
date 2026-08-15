"""Ports owned by the Stream Dock protocol layer."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from ...commands import StreamDockCommand
from ...events import StreamDockEvent


@runtime_checkable
class DecodedEventParser(Protocol):
    """Port for converting one decoded JSON value into a typed API event."""

    @abstractmethod
    def parse(self, value: object) -> StreamDockEvent:
        """Validate a decoded JSON value and return its typed event."""

        ...


@runtime_checkable
class StreamDockEventDecoder(Protocol):
    """Port for converting one text frame into one typed API event."""

    @abstractmethod
    def decode(self, frame: str) -> StreamDockEvent:
        """Decode and validate one Stream Dock text frame."""

        ...


@runtime_checkable
class StreamDockCommandEncoder(Protocol):
    """Port for converting one typed API command into one text frame."""

    @abstractmethod
    def encode(self, command: StreamDockCommand) -> str:
        """Validate and serialize one Stream Dock command."""

        ...

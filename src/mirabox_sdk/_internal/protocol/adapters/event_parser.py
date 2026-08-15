"""Adapter from the public pure event parser to the boundary parser port."""

from __future__ import annotations

from ....events import StreamDockEvent
from ....parser import parse_stream_dock_event
from ..ports import DecodedEventParser


class EventParserAdapter(DecodedEventParser):
    """Expose the canonical event parser through the boundary parser port."""

    __slots__ = ()

    def parse(self, value: object) -> StreamDockEvent:
        """Return the typed representation of one decoded event envelope."""

        return parse_stream_dock_event(value)

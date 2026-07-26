"""Explicit temporary adapter around the existing event parser."""

from __future__ import annotations

from ....events import StreamDockEvent
from ....parser import parse_stream_dock_event
from ..ports import DecodedEventParser


class LegacyEventParserAdapter(DecodedEventParser):
    """Adapt the existing pure event parser to the new parser port."""

    __slots__ = ()

    def parse(self, value: object) -> StreamDockEvent:
        """Return the existing parser's typed representation of ``value``."""

        return parse_stream_dock_event(value)

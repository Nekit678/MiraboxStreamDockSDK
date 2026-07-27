"""Immutable aggregate metrics for the composed Stream Dock boundary."""

from __future__ import annotations

from dataclasses import dataclass

from ..messaging.metrics import (
    CommandWriterMetrics,
    EventReaderMetrics,
    InboundEventQueueMetrics,
    OutboundCommandQueueMetrics,
)
from ..transport.metrics import TransportQueueMetrics, WebSocketConnectorMetrics


@dataclass(frozen=True, slots=True)
class StreamDockBoundaryMetrics:
    """One point-in-time snapshot of every boundary-owned component."""

    raw_inbound: TransportQueueMetrics
    event_reader: EventReaderMetrics
    inbound_events: InboundEventQueueMetrics
    outbound_commands: OutboundCommandQueueMetrics
    command_writer: CommandWriterMetrics
    raw_outbound: TransportQueueMetrics
    connector: WebSocketConnectorMetrics
    session_events: TransportQueueMetrics

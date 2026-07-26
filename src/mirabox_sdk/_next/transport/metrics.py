"""Immutable metric snapshots owned by the transport layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TransportQueueMetrics:
    """Point-in-time metrics for one transport queue."""

    queue_limit: int
    current_depth: int
    peak_depth: int
    submitted: int
    enqueued: int
    dequeued: int
    backpressured: int
    rejected_full: int
    rejected_after_shutdown: int
    discarded_during_shutdown: int

    @property
    def rejected(self) -> int:
        """Return the number of items refused before queueing."""

        return self.rejected_full + self.rejected_after_shutdown


@dataclass(frozen=True, slots=True)
class WebSocketConnectorMetrics:
    """Point-in-time metrics exposed by a WebSocket connector."""

    connect_count: int
    disconnect_count: int
    last_close_code: int | None
    transport_error_count: int
    session_events_rejected: int
    inbound_frames_received: int
    inbound_frames_forwarded: int
    inbound_frames_rejected: int
    binary_frames_rejected: int
    outbound_frames_received: int
    outbound_frames_sent: int
    outbound_send_failures: int
    outbound_drain_timeouts: int
    outbound_discarded_during_shutdown: int

"""Immutable metric snapshots owned by the messaging layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InboundEventQueueMetrics:
    """Point-in-time metrics for the typed inbound queue."""

    queue_limit: int
    current_depth: int
    peak_depth: int
    submitted: int
    enqueued: int
    coalesced: int
    dequeued: int
    in_flight: int
    acknowledged: int
    backpressured: int
    dropped_newest: int
    dropped_oldest: int
    rejected_full: int
    rejected_after_shutdown: int
    discarded_during_shutdown: int

    @property
    def dropped(self) -> int:
        """Return every event explicitly not delivered by this queue."""

        return (
            self.dropped_newest
            + self.dropped_oldest
            + self.rejected_full
            + self.rejected_after_shutdown
            + self.discarded_during_shutdown
        )


@dataclass(frozen=True, slots=True)
class OutboundCommandQueueMetrics:
    """Point-in-time metrics for the typed outbound queue."""

    queue_limit: int
    current_depth: int
    peak_depth: int
    submitted: int
    enqueued: int
    coalesced: int
    dequeued: int
    rejected_full: int
    rejected_after_shutdown: int
    discarded_during_shutdown: int

    @property
    def rejected(self) -> int:
        """Return the number of commands refused before acceptance."""

        return self.rejected_full + self.rejected_after_shutdown


@dataclass(frozen=True, slots=True)
class EventReaderMetrics:
    """Point-in-time counters for the inbound reader."""

    frames_received: int
    decoded: int
    submitted: int
    rejected: int
    protocol_failures: int
    unknown_events: int
    sink_failures: int


@dataclass(frozen=True, slots=True)
class CommandWriterMetrics:
    """Point-in-time counters for the outbound writer."""

    commands_received: int
    serialized: int
    frames_enqueued: int
    serialization_failures: int
    raw_outbound_failures: int
    completed: int
    completion_failures: int
    discarded_during_shutdown: int

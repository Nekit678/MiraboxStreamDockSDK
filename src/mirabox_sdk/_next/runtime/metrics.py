"""Immutable metric snapshots owned by the runtime dispatcher."""

from __future__ import annotations

from dataclasses import dataclass

from ..boundary.metrics import StreamDockBoundaryMetrics


@dataclass(frozen=True, slots=True)
class SessionCoordinatorMetrics:
    """Point-in-time counters for session consumption and initialization."""

    events_received: int = 0
    connected: int = 0
    invalid_transitions: int = 0
    initialization_started: int = 0
    initialization_succeeded: int = 0
    initialization_failed: int = 0
    registration_failures: int = 0
    initial_settings_request_failures: int = 0
    disconnected: int = 0
    last_close_code: int | None = None
    transport_errors: int = 0
    source_poll_timeouts: int = 0
    source_closed: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeEventPumpMetrics:
    """Point-in-time counters for inbound event ownership and acknowledgement."""

    events_received: int = 0
    events_acknowledged: int = 0
    acknowledgement_failures: int = 0
    submitted_to_scheduler: int = 0
    discarded_during_shutdown: int = 0
    source_poll_timeouts: int = 0
    source_closed: int = 0
    current_owned: int = 0
    peak_owned: int = 0


@dataclass(frozen=True, slots=True)
class HandlerSchedulerMetrics:
    """Point-in-time scheduler admission, activity, and completion counters."""

    accepted: int = 0
    completed: int = 0
    current_pending: int = 0
    peak_pending: int = 0
    current_active_callbacks: int = 0
    peak_active_callbacks: int = 0
    active_contexts: int = 0
    barriers_processed: int = 0
    callback_failures: int = 0
    callback_timeouts: int = 0
    discarded_during_shutdown: int = 0
    admission_backpressure: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeRouterMetrics:
    """Point-in-time counters for known and forward-compatible routing."""

    known_events_routed: int = 0
    unknown_events_delivered: int = 0


@dataclass(frozen=True, slots=True)
class ActionContextMetrics:
    """Point-in-time counters for action ownership and state transitions."""

    action_instances_created: int = 0
    duplicate_appearances: int = 0
    unknown_action_uuids: int = 0
    missing_contexts: int = 0
    actions_removed: int = 0
    appearance_rollbacks: int = 0
    settings_updates: int = 0
    settings_update_failures: int = 0
    title_updates: int = 0
    broadcasts: int = 0
    broadcast_targets: int = 0
    broadcast_failures: int = 0
    global_settings_updates: int = 0
    global_settings_replays: int = 0


@dataclass(frozen=True, slots=True)
class StreamDockRuntimeMetrics:
    """One point-in-time snapshot of runtime and boundary diagnostics."""

    session: SessionCoordinatorMetrics
    event_pump: RuntimeEventPumpMetrics
    scheduler: HandlerSchedulerMetrics
    routing: RuntimeRouterMetrics
    actions: ActionContextMetrics
    boundary: StreamDockBoundaryMetrics

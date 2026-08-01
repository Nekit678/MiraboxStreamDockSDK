"""Application-facing and internal ports for the runtime dispatcher."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ...commands import StreamDockCommand
from ...events import (
    ApplicationDidLaunchEvent,
    ApplicationDidTerminateEvent,
    DeviceDidConnectEvent,
    DeviceDidDisconnectEvent,
    DialDownEvent,
    DialRotateEvent,
    DialUpEvent,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    KeyDownEvent,
    KeyUpEvent,
    PropertyInspectorDidAppearEvent,
    PropertyInspectorDidDisappearEvent,
    SendToPluginEvent,
    StreamDockEvent,
    SystemDidWakeUpEvent,
    TitleParameters,
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from ...json_types import JsonObject
from ..messaging.models import CommandFuture
from .metrics import HandlerSchedulerMetrics, StreamDockRuntimeMetrics
from .models import DispatchResult


@runtime_checkable
class RuntimeAction(Protocol):
    """Minimum identity exposed by an application-owned action instance."""

    @property
    @abstractmethod
    def action(self) -> str:
        """Return the manifest action UUID."""

        ...

    @property
    @abstractmethod
    def context(self) -> str:
        """Return the opaque Stream Dock context."""

        ...


class RuntimeActionCallbacks(RuntimeAction, Protocol):
    """Application state and callback surface used by runtime transitions."""

    settings: object
    title: str
    title_parameters: TitleParameters | None

    @abstractmethod
    def update_settings_from_wire(self, settings: JsonObject) -> None:
        """Atomically decode and replace action settings from a wire event."""

        ...

    @abstractmethod
    def on_will_appear(self, event: WillAppearEvent) -> None: ...

    @abstractmethod
    def on_will_disappear(self, event: WillDisappearEvent | None = None) -> None: ...

    @abstractmethod
    def on_did_receive_settings(self, event: DidReceiveSettingsEvent) -> None: ...

    @abstractmethod
    def on_title_parameters_did_change(
        self,
        event: TitleParametersDidChangeEvent,
    ) -> None: ...

    @abstractmethod
    def on_key_down(self, event: KeyDownEvent) -> None: ...

    @abstractmethod
    def on_key_up(self, event: KeyUpEvent) -> None: ...

    @abstractmethod
    def on_touch_tap(self, event: TouchTapEvent) -> None: ...

    @abstractmethod
    def on_dial_down(self, event: DialDownEvent) -> None: ...

    @abstractmethod
    def on_dial_up(self, event: DialUpEvent) -> None: ...

    @abstractmethod
    def on_dial_rotate(self, event: DialRotateEvent) -> None: ...

    @abstractmethod
    def on_property_inspector_did_appear(
        self,
        event: PropertyInspectorDidAppearEvent,
    ) -> None: ...

    @abstractmethod
    def on_property_inspector_did_disappear(
        self,
        event: PropertyInspectorDidDisappearEvent,
    ) -> None: ...

    @abstractmethod
    def on_send_to_plugin(self, event: SendToPluginEvent) -> None: ...

    @abstractmethod
    def on_did_receive_global_settings(
        self,
        event: DidReceiveGlobalSettingsEvent,
    ) -> None: ...

    @abstractmethod
    def on_device_did_connect(self, event: DeviceDidConnectEvent) -> None: ...

    @abstractmethod
    def on_device_did_disconnect(self, event: DeviceDidDisconnectEvent) -> None: ...

    @abstractmethod
    def on_application_did_launch(self, event: ApplicationDidLaunchEvent) -> None: ...

    @abstractmethod
    def on_application_did_terminate(
        self,
        event: ApplicationDidTerminateEvent,
    ) -> None: ...

    @abstractmethod
    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None: ...


@runtime_checkable
class ActionFactory(Protocol):
    """Create one application action for a visible Stream Dock context."""

    @abstractmethod
    def create(
        self,
        action_uuid: str,
        context: str,
        initial_settings: JsonObject,
    ) -> RuntimeActionCallbacks | None:
        """Return a new action, or ``None`` for an unknown action UUID."""

        ...


@runtime_checkable
class PluginHooks(Protocol):
    """Plugin-scope callbacks that are not action routes."""

    @abstractmethod
    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        """Observe one forward-compatible event exactly once."""

        ...


@runtime_checkable
class StreamDockSender(Protocol):
    """Narrow typed command port supplied to runtime collaborators."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None:
        """Submit a command and wait for its terminal result."""

        ...

    @abstractmethod
    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        """Submit a command and return its boundary completion handle."""

        ...


@runtime_checkable
class RuntimeLifecycle(Protocol):
    """Single-run lifecycle exposed by the runtime facade."""

    @abstractmethod
    def run_forever(self) -> None:
        """Run until remote disconnect, close, or a fatal failure."""

        ...

    @abstractmethod
    def close(self) -> None:
        """Idempotently request graceful runtime shutdown."""

        ...

    @abstractmethod
    def metrics(self) -> StreamDockRuntimeMetrics:
        """Return an immutable aggregate runtime snapshot."""

        ...


@runtime_checkable
class DispatchCompletion(Protocol):
    """Read-only terminal result for scheduler-owned dispatch work."""

    @abstractmethod
    def done(self) -> bool:
        """Return whether dispatch ownership reached a terminal outcome."""

        ...

    @abstractmethod
    def result(self, timeout: float | None = None) -> DispatchResult:
        """Wait for and return the terminal dispatch result."""

        ...

    @abstractmethod
    def add_done_callback(
        self,
        callback: Callable[[DispatchCompletion], None],
    ) -> None:
        """Invoke ``callback`` once after terminal completion."""

        ...


@runtime_checkable
class HandlerScheduler(Protocol):
    """Bounded, replaceable scheduler for typed runtime events."""

    @abstractmethod
    def start(self) -> None:
        """Start scheduler-owned workers, if any."""

        ...

    @abstractmethod
    def submit(self, event: StreamDockEvent) -> DispatchCompletion:
        """Accept one event and return its terminal completion."""

        ...

    @abstractmethod
    def stop_accepting(self) -> None:
        """Reject new events while retaining ownership of accepted work."""

        ...

    @abstractmethod
    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait for accepted work to reach terminal outcomes."""

        ...

    @abstractmethod
    def stop(self, *, timeout: float | None = None) -> bool:
        """Stop scheduler-owned workers within the optional timeout."""

        ...

    @abstractmethod
    def metrics(self) -> HandlerSchedulerMetrics:
        """Return an immutable scheduler snapshot."""

        ...


@runtime_checkable
class ActionContextManager(Protocol):
    """Own application actions addressed by opaque Stream Dock contexts."""

    @abstractmethod
    def create(self, event: WillAppearEvent) -> RuntimeActionCallbacks | None:
        """Create and retain an action for an appearance event."""

        ...

    @abstractmethod
    def get(self, context: str) -> RuntimeActionCallbacks | None:
        """Return the current action for ``context``, if any."""

        ...

    @abstractmethod
    def remove(
        self,
        context: str,
        *,
        expected: RuntimeActionCallbacks | None = None,
    ) -> RuntimeActionCallbacks | None:
        """Remove one action, optionally only when its identity matches."""

        ...

    @abstractmethod
    def snapshot(self) -> tuple[RuntimeActionCallbacks, ...]:
        """Return an immutable action snapshot."""

        ...

    @abstractmethod
    def clear(self) -> tuple[RuntimeActionCallbacks, ...]:
        """Atomically remove and return all retained actions."""

        ...

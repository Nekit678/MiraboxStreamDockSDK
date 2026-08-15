"""Plugin-scope callbacks for known and forward-compatible runtime events."""

from __future__ import annotations

from ...events import (
    ApplicationDidLaunchEvent,
    ApplicationDidTerminateEvent,
    DeviceDidConnectEvent,
    DeviceDidDisconnectEvent,
    DidReceiveGlobalSettingsEvent,
    SystemDidWakeUpEvent,
    UnknownStreamDockEvent,
)
from .ports import PluginHooks


class Plugin:
    """Base class for plugin-wide callbacks.

    Subclass this class and override only the lifecycle or settings callbacks
    needed by the plugin. Known broadcast events are delivered to the plugin
    before they are delivered to the snapshot of active actions. Callback
    failures are isolated, so a failing plugin callback does not prevent
    action delivery.
    """

    def on_did_receive_global_settings(self, event: DidReceiveGlobalSettingsEvent) -> None:
        """Observe global settings after runtime state has been updated."""

    def on_device_did_connect(self, event: DeviceDidConnectEvent) -> None:
        """Observe a newly connected Stream Dock device."""

    def on_device_did_disconnect(self, event: DeviceDidDisconnectEvent) -> None:
        """Observe a disconnected Stream Dock device."""

    def on_application_did_launch(self, event: ApplicationDidLaunchEvent) -> None:
        """Observe an application launch reported by Stream Dock."""

    def on_application_did_terminate(self, event: ApplicationDidTerminateEvent) -> None:
        """Observe an application termination reported by Stream Dock."""

    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
        """Observe system wake-up."""

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        """Observe one forward-compatible event exactly once."""


class LegacyPluginHooksAdapter(Plugin):
    """Preserve the legacy unknown-event-only ``PluginHooks`` contract."""

    def __init__(self, hooks: PluginHooks) -> None:
        self._hooks = hooks

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        self._hooks.on_unhandled_event(event)

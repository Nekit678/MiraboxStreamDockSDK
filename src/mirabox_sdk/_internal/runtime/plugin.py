"""Plugin-scope callbacks for known and forward-compatible runtime events."""

from __future__ import annotations

import logging
from threading import Lock

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

logger = logging.getLogger(__name__)


class Plugin:
    """Base class for plugin-wide callbacks.

    Subclass this class and override only the lifecycle or settings callbacks
    needed by the plugin. Known broadcast events are delivered to the plugin
    before they are delivered to the snapshot of active actions. Callback
    failures are isolated, so a failing plugin callback does not prevent
    action delivery.
    """

    def on_ready(self) -> None:
        """Start session work once, before inbound protocol callbacks.

        Registration and the initial settings request have completed, so
        commands may be sent. This does not wait for a settings response.
        Return promptly; use an owned worker for long-running work.
        """

    def on_stop(self) -> None:
        """Release work started by ``on_ready()``, even if that callback failed.

        Called once on the runtime cleanup worker after protocol callbacks
        finish, and before services stop. Cleanup is deferred after a shutdown
        timeout. Join owned workers here; application work can observe the
        context stop signal before cleanup starts. The transport is closed.
        No call is made if ``on_ready()`` was never attempted.
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


class PluginSessionLifecycle:
    """Own the once-only ready/stop pair independently from wire events."""

    def __init__(self, plugin: Plugin) -> None:
        self._plugin = plugin
        self._lock = Lock()
        self._ready_started = False
        self._stopped = False

    def ready(self) -> None:
        with self._lock:
            if self._ready_started or self._stopped:
                return
            self._ready_started = True
        self._invoke("on_ready")

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            ready_started = self._ready_started
        if ready_started:
            self._invoke("on_stop")

    def _invoke(self, callback: str) -> None:
        try:
            getattr(self._plugin, callback)()
        except Exception as exc:
            logger.error(
                "Plugin session callback failed; callback=%s exception_type=%s",
                callback,
                type(exc).__name__,
            )
            if callback == "on_stop":
                raise

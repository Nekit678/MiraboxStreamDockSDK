"""Reusable Stream Dock plugin runtime and event dispatcher."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Generic, TypeVar

from .action import Action
from .action_registry import ActionRegistry
from .codecs import JsonCodec
from .commands import (
    GetGlobalSettingsCommand,
    RegisterPluginCommand,
    SetGlobalSettingsCommand,
)
from .errors import JsonCodecDecodeError
from .events import (
    ActionEvent,
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
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from .json_types import JsonObject
from .protocols import (
    LifecycleService,
    StreamDockActionDependencies,
    StreamDockConnection,
    StreamDockListener,
)
from .registration import PluginLaunchArguments

logger = logging.getLogger(__name__)

DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)
GlobalSettingsT = TypeVar("GlobalSettingsT")


class StreamDockPlugin(StreamDockListener, Generic[DependenciesT]):
    """Manage registration, actions, event dispatch, and service lifecycle."""

    def __init__(
        self,
        launch_arguments: PluginLaunchArguments,
        *,
        stream_dock: StreamDockConnection,
        action_registry: ActionRegistry[DependenciesT],
        action_dependencies: DependenciesT,
        services: Iterable[LifecycleService] = (),
    ) -> None:
        self.actions: dict[str, Action[Any, DependenciesT]] = {}
        self.global_settings: JsonObject = {}
        self.launch_arguments = launch_arguments
        self.plugin_uuid = launch_arguments.plugin_uuid
        self.register_event = launch_arguments.register_event
        self.info = launch_arguments.info
        self.stream_dock = stream_dock
        self.action_registry = action_registry
        self.action_dependencies = action_dependencies
        self._services = tuple(services)
        self._started_services: list[LifecycleService] = []
        self._has_run = False
        self._stopped = False
        self.stream_dock.set_listener(self)

    def run(self) -> None:
        """Start plugin services and process Stream Dock events until disconnected."""

        if self._stopped:
            raise RuntimeError("Cannot run a stopped Stream Dock plugin")
        if self._has_run:
            raise RuntimeError("Stream Dock plugin has already been run")
        self._has_run = True
        logger.info("Starting Stream Dock plugin %s", self.plugin_uuid)
        for service in self._services:
            service.start()
            self._started_services.append(service)
        self.stream_dock.run_forever()

    def stop(self) -> None:
        """Release actions, services, and the connection exactly once."""

        if self._stopped:
            return
        self._stopped = True

        for action in tuple(self.actions.values()):
            try:
                action.on_will_disappear()
            except Exception:
                logger.exception("Failed to release action context %s", action.context)
        self.actions.clear()

        for service in reversed(self._started_services):
            try:
                service.stop()
            except Exception:
                logger.exception("Failed to stop plugin service %r", service)
        self._started_services.clear()

        try:
            self.stream_dock.close()
        except Exception:
            logger.exception("Failed to close Stream Dock connection")
        logger.info("Stream Dock plugin %s stopped", self.plugin_uuid)

    def on_stream_dock_connected(self) -> None:
        self.stream_dock.send(RegisterPluginCommand(self.register_event, self.plugin_uuid))
        self.get_global_settings()

    def on_stream_dock_event(self, event: StreamDockEvent) -> None:
        try:
            self._dispatch(event)
        except Exception:
            logger.exception("Failed to process Stream Dock event %s", event.event_name)

    def _dispatch(self, event: StreamDockEvent) -> None:
        if isinstance(event, WillAppearEvent):
            self._create_action(event)
            return

        if isinstance(event, WillDisappearEvent):
            action = self.actions.pop(event.context, None)
            if action is not None:
                action.on_will_disappear(event)
            return

        if isinstance(event, DidReceiveGlobalSettingsEvent):
            self.global_settings = event.settings
            self._dispatch_broadcast_event(event)
            return

        if not isinstance(event, ActionEvent):
            self._dispatch_broadcast_event(event)
            return

        action = self.actions.get(event.context)
        if action is None:
            return

        if isinstance(event, DidReceiveSettingsEvent):
            try:
                action.update_settings_from_wire(event.settings)
            except JsonCodecDecodeError as exc:
                raise JsonCodecDecodeError(
                    exc.reason,
                    event_name=event.event_name,
                    path=("payload", "settings", *exc.path),
                ) from exc
            action.on_did_receive_settings(event)
            return

        if isinstance(event, TitleParametersDidChangeEvent):
            action.title = event.title
            action.title_parameters = event.title_parameters
            action.on_title_parameters_did_change(event)
            return

        self._dispatch_action_event(action, event)

    def _create_action(self, event: WillAppearEvent) -> None:
        if event.context in self.actions:
            return

        try:
            action = self.action_registry.create(
                event.action,
                event.context,
                event.settings,
                self.action_dependencies,
            )
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                event_name=event.event_name,
                path=("payload", "settings", *exc.path),
            ) from exc
        if action is None:
            logger.error("Unknown action UUID: %s", event.action)
            return
        self.actions[event.context] = action
        try:
            action.on_will_appear(event)
        except Exception:
            self.actions.pop(event.context, None)
            try:
                action.on_will_disappear()
            except Exception:
                logger.exception("Failed to roll back action context %s", event.context)
            raise

    @staticmethod
    def _dispatch_action_event(
        action: Action[Any, DependenciesT],
        event: ActionEvent,
    ) -> None:
        if isinstance(event, KeyDownEvent):
            action.on_key_down(event)
        elif isinstance(event, KeyUpEvent):
            action.on_key_up(event)
        elif isinstance(event, TouchTapEvent):
            action.on_touch_tap(event)
        elif isinstance(event, DialDownEvent):
            action.on_dial_down(event)
        elif isinstance(event, DialUpEvent):
            action.on_dial_up(event)
        elif isinstance(event, DialRotateEvent):
            action.on_dial_rotate(event)
        elif isinstance(event, PropertyInspectorDidAppearEvent):
            action.on_property_inspector_did_appear(event)
        elif isinstance(event, PropertyInspectorDidDisappearEvent):
            action.on_property_inspector_did_disappear(event)
        elif isinstance(event, SendToPluginEvent):
            action.on_send_to_plugin(event)

    def _dispatch_broadcast_event(self, event: StreamDockEvent) -> None:
        for action in tuple(self.actions.values()):
            try:
                self._dispatch_broadcast_event_to_action(action, event)
            except Exception:
                logger.exception(
                    "Failed to process broadcast Stream Dock event %s for action %s context %s",
                    event.event_name,
                    action.action,
                    action.context,
                )

    @staticmethod
    def _dispatch_broadcast_event_to_action(
        action: Action[Any, DependenciesT],
        event: StreamDockEvent,
    ) -> None:
        if isinstance(event, DidReceiveGlobalSettingsEvent):
            action.on_did_receive_global_settings(event)
        elif isinstance(event, DeviceDidConnectEvent):
            action.on_device_did_connect(event)
        elif isinstance(event, DeviceDidDisconnectEvent):
            action.on_device_did_disconnect(event)
        elif isinstance(event, ApplicationDidLaunchEvent):
            action.on_application_did_launch(event)
        elif isinstance(event, ApplicationDidTerminateEvent):
            action.on_application_did_terminate(event)
        elif isinstance(event, SystemDidWakeUpEvent):
            action.on_system_did_wake_up(event)

    def set_global_settings(self, settings: JsonObject) -> None:
        self.global_settings = settings
        self.stream_dock.send(SetGlobalSettingsCommand(self.plugin_uuid, settings))

    def set_typed_global_settings(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Encode and persist plugin-owned global settings."""

        command = SetGlobalSettingsCommand.from_settings(self.plugin_uuid, settings, codec)
        self.global_settings = command.settings
        self.stream_dock.send(command)

    def get_global_settings(self) -> None:
        self.stream_dock.send(GetGlobalSettingsCommand(self.plugin_uuid))

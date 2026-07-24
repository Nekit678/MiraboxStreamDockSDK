"""Reusable Stream Dock plugin runtime and event dispatcher."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, Generic, TypeVar

from .action import Action
from .action_registry import ActionRegistry
from .codecs import JSON_OBJECT_CODEC, JsonCodec
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
from .json_types import (
    JsonObject,
    _copy_on_write_json_object,
    _CopyOnWriteJsonSource,
    _prepare_copy_on_write_json_object,
    clone_json_object,
)
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
    """Manage registration, action dispatch, and plugin service lifecycle.

    One runtime owns one Stream Dock connection and one action registry. It
    creates an :class:`Action` for each ``willAppear`` context, routes targeted
    events to that instance, and broadcasts device, application, system, and
    global-settings events to all active actions.

    ``DependenciesT`` is the application-defined dependency container passed to
    every action constructed by the registry.

    Attributes:
        actions: Active action instances keyed by their opaque context IDs.
        global_settings: Isolated copy of the latest raw plugin-wide settings.
        launch_arguments: Validated values supplied by Stream Dock at startup.
        plugin_uuid: UUID used for registration and global-settings commands.
        register_event: Runtime-provided event name used for registration.
        info: Parsed host, device, theme, and manifest metadata.
        stream_dock: Connection used for incoming events and outgoing commands.
        action_registry: Registry used to resolve manifest action UUIDs.
        action_dependencies: Shared dependency container passed to new actions.
    """

    def __init__(
        self,
        launch_arguments: PluginLaunchArguments,
        *,
        stream_dock: StreamDockConnection,
        action_registry: ActionRegistry[DependenciesT],
        action_dependencies: DependenciesT,
        services: Iterable[LifecycleService] = (),
    ) -> None:
        """Create a plugin runtime and attach it to a connection.

        Args:
            launch_arguments: Validated executable launch arguments.
            stream_dock: Connection that will carry protocol traffic. This
                runtime installs itself as the connection listener immediately.
            action_registry: Mapping from manifest UUIDs to action classes.
            action_dependencies: Dependency container passed to every created
                action.
            services: Optional plugin-owned services. They start in iteration
                order and stop in reverse order.
        """

        self.actions: dict[str, Action[Any, DependenciesT]] = {}
        self._global_settings_snapshot: JsonObject = {}
        self._global_settings_source = _prepare_copy_on_write_json_object(
            self._global_settings_snapshot
        )
        self._global_settings: JsonObject = _copy_on_write_json_object(self._global_settings_source)
        self._global_settings_snapshot_dirty = False
        self._global_settings_loaded = False
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

    @property
    def global_settings(self) -> JsonObject:
        """Return the isolated mutable view of the latest global settings.

        Container mutations validate and isolate new values before committing.
        Invalid JSON values raise :class:`ValueError` without changing settings.
        """

        return self._global_settings

    @global_settings.setter
    def global_settings(self, settings: JsonObject) -> None:
        loaded = self._global_settings_loaded
        self._replace_global_settings(clone_json_object(settings))
        self._global_settings_loaded = loaded

    def run(self) -> None:
        """Start services and process Stream Dock events until disconnection.

        A runtime is single-use: it cannot be run more than once or restarted
        after :meth:`stop`. Successfully started services are recorded for
        reverse-order cleanup by :meth:`stop`.

        Raises:
            RuntimeError: If this runtime was already run or stopped.
            Exception: Any exception raised while starting a service or running
                the connection loop is propagated to the caller.
        """

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
        """Release actions, started services, and the connection exactly once.

        Active actions receive ``on_will_disappear(None)``. Services stop in
        reverse startup order. Cleanup failures are logged and do not prevent
        the remaining resources from being released; repeated calls are no-ops.
        """

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
        """Register the plugin and request global settings after connection.

        This callback is invoked by the connection when its WebSocket opens.
        Registration uses the exact event name and UUID supplied at launch.
        """

        self.stream_dock.send(RegisterPluginCommand(self.register_event, self.plugin_uuid))
        self.get_global_settings()

    def on_stream_dock_event(self, event: StreamDockEvent) -> None:
        """Dispatch one parsed event while isolating callback failures.

        Args:
            event: Known or forward-compatible unknown event from the
                connection.

        Callback exceptions are logged with the event name and are not allowed
        to escape into the WebSocket receive loop.
        """

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
            snapshot = clone_json_object(event.settings)
            self._replace_global_settings(snapshot)
            self._dispatch_global_settings(self._global_settings_source)
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

        if self._global_settings_loaded:
            self._dispatch_broadcast_event_to_action_safely(
                action,
                self._new_global_settings_event(),
            )

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
            self._dispatch_broadcast_event_to_action_safely(action, event)

    def _dispatch_global_settings(self, source: _CopyOnWriteJsonSource) -> None:
        for action in tuple(self.actions.values()):
            self._dispatch_broadcast_event_to_action_safely(
                action,
                DidReceiveGlobalSettingsEvent(settings=_copy_on_write_json_object(source)),
            )

    def _new_global_settings_event(self) -> DidReceiveGlobalSettingsEvent:
        return DidReceiveGlobalSettingsEvent(
            settings=_copy_on_write_json_object(self._current_global_settings_source())
        )

    def _current_global_settings_source(self) -> _CopyOnWriteJsonSource:
        """Materialize pending public mutations for the next isolated replay."""

        if self._global_settings_snapshot_dirty:
            self._global_settings_snapshot = clone_json_object(self._global_settings)
            self._global_settings_source = _prepare_copy_on_write_json_object(
                self._global_settings_snapshot
            )
            self._global_settings_snapshot_dirty = False
        return self._global_settings_source

    def _replace_global_settings(
        self,
        snapshot: JsonObject | _CopyOnWriteJsonSource,
    ) -> None:
        global_settings: JsonObject

        def mark_snapshot_dirty_after_mutation() -> None:
            if self._global_settings is global_settings:
                self._global_settings_snapshot_dirty = True

        if isinstance(snapshot, _CopyOnWriteJsonSource):
            source = snapshot
            owned_snapshot = source._value
        else:
            source = _prepare_copy_on_write_json_object(snapshot)
            owned_snapshot = snapshot
        global_settings = _copy_on_write_json_object(
            source,
            on_mutation=mark_snapshot_dirty_after_mutation,
        )
        self._global_settings_snapshot = owned_snapshot
        self._global_settings_source = source
        self._global_settings = global_settings
        self._global_settings_snapshot_dirty = False
        self._global_settings_loaded = True

    def _dispatch_broadcast_event_to_action_safely(
        self,
        action: Action[Any, DependenciesT],
        event: StreamDockEvent,
    ) -> None:
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

    def update_global_settings(self, update: Callable[[JsonObject], None]) -> None:
        """Atomically update and persist raw global settings.

        ``update`` receives an isolated copy-on-write draft. The draft is sent
        to Stream Dock and replaces :attr:`global_settings` only after the
        callback returns, the complete result passes JSON validation, and the
        command is sent successfully. Callback, validation, or send failures
        leave the current public view and replay snapshot unchanged.

        Args:
            update: Callback that applies one or more changes to the draft.

        Raises:
            JsonCodecEncodeError: If the completed draft is not a finite JSON
                object.
            Exception: Any exception raised by ``update`` or while sending the
                command.
        """

        draft = _copy_on_write_json_object(self._current_global_settings_source())
        update(draft)
        self.set_global_settings(draft)

    def set_global_settings(self, settings: JsonObject) -> None:
        """Validate and persist raw plugin-wide settings.

        Local :attr:`global_settings` is updated with an isolated copy only
        after the command is sent successfully.

        Args:
            settings: JSON-compatible object shared by all plugin actions.

        Raises:
            JsonCodecEncodeError: If ``settings`` is not a finite JSON object.
        """

        command = SetGlobalSettingsCommand.from_settings(
            self.plugin_uuid,
            settings,
            JSON_OBJECT_CODEC,
        )
        self._send_global_settings(command)

    def set_typed_global_settings(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Encode and persist typed plugin-wide settings.

        Local :attr:`global_settings` stores the encoded JSON representation and
        changes only after the command is sent successfully.

        Args:
            settings: Plugin-owned global settings value.
            codec: Codec that converts the value to a JSON object.

        Raises:
            JsonCodecEncodeError: If encoding fails or produces invalid JSON.
        """

        command = SetGlobalSettingsCommand.from_settings(self.plugin_uuid, settings, codec)
        self._send_global_settings(command)

    def _send_global_settings(self, command: SetGlobalSettingsCommand) -> None:
        self.stream_dock.send(command)
        self._replace_global_settings(command._owned_settings_source())

    def get_global_settings(self) -> None:
        """Request the latest persisted plugin-wide settings.

        The response arrives asynchronously as
        :class:`DidReceiveGlobalSettingsEvent`, updates
        :attr:`global_settings`, and is broadcast to active actions.
        """

        self.stream_dock.send(GetGlobalSettingsCommand(self.plugin_uuid))

"""Reusable Stream Dock plugin runtime and event dispatcher."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Generic, TypeVar

from .action import Action
from .action_registry import ActionRegistry
from .codecs import JsonCodec
from .commands import GetGlobalSettingsCommand, RegisterPluginCommand
from .events import (
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    EventDescriptor,
    StreamDockEvent,
    TitleParametersDidChangeEvent,
    UnknownStreamDockEvent,
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
from .stores import ActionStore, GlobalSettingsStore

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
        actions: Read-only snapshot of active action instances keyed by their
            opaque context IDs.
        global_settings: Isolated copy of the latest raw plugin-wide settings.
        launch_arguments: Validated values supplied by Stream Dock at startup.
        plugin_uuid: UUID used for registration and global-settings commands.
        register_event: Runtime-provided event name used for registration.
        info: Parsed host, device, theme, and manifest metadata.
        stream_dock: Connection used for incoming events and outgoing commands.
        action_registry: Registry used to resolve manifest action UUIDs.
        action_dependencies: Shared dependency container passed to new actions.
        action_store: Context-indexed owner of active action instances.
        global_settings_store: Owner of global-settings state and persistence.
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

        self.launch_arguments = launch_arguments
        self.plugin_uuid = launch_arguments.plugin_uuid
        self.register_event = launch_arguments.register_event
        self.info = launch_arguments.info
        self.stream_dock = stream_dock
        self.action_registry = action_registry
        self.action_dependencies = action_dependencies
        self.action_store = ActionStore(action_registry, action_dependencies)
        self.global_settings_store = GlobalSettingsStore(self.plugin_uuid, stream_dock)
        self._services = tuple(services)
        self._started_services: list[LifecycleService] = []
        self._has_run = False
        self._stopped = False
        self.stream_dock.set_listener(self)

    @property
    def actions(self) -> Mapping[str, Action[Any, DependenciesT]]:
        """Return a read-only point-in-time snapshot of active actions."""

        return self.action_store.actions

    @actions.setter
    def actions(self, actions: Mapping[str, Action[Any, DependenciesT]]) -> None:
        """Replace active actions; retained for test and migration compatibility."""

        self.action_store.replace(actions)

    @property
    def global_settings(self) -> JsonObject:
        """Return the isolated mutable view of the latest global settings.

        Container mutations validate and isolate new values before committing.
        Invalid JSON values raise :class:`ValueError` without changing settings.
        """

        return self.global_settings_store.settings

    @global_settings.setter
    def global_settings(self, settings: JsonObject) -> None:
        self.global_settings_store.replace_local(settings)

    def run(self) -> None:
        """Start services and process Stream Dock events until disconnection.

        Call this once on the application's lifecycle thread. A runtime cannot
        be restarted after return or after :meth:`stop`. Successfully started
        services are recorded for reverse-order cleanup by :meth:`stop`.

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

        The connection first stops accepting and drains inbound events. Active
        actions then receive ``on_will_disappear(None)`` and services stop in
        reverse startup order. Call this on the lifecycle thread after
        :meth:`run` returns; another thread that needs to interrupt the blocking
        loop should call ``stream_dock.close()``. Cleanup failures are logged
        and do not prevent the remaining resources from being released;
        repeated calls are no-ops.
        """

        if self._stopped:
            return
        self._stopped = True

        try:
            self.stream_dock.close()
        except Exception as exc:
            logger.error(
                "Failed to close Stream Dock connection; exception_type=%s",
                type(exc).__name__,
            )

        for action in self.action_store.clear():
            try:
                action.on_will_disappear()
            except Exception as exc:
                logger.error(
                    "Failed to release action context %s; exception_type=%s",
                    action.context,
                    type(exc).__name__,
                )

        for service in reversed(self._started_services):
            try:
                service.stop()
            except Exception as exc:
                logger.error(
                    "Failed to stop plugin service; service_type=%s exception_type=%s",
                    type(service).__name__,
                    type(exc).__name__,
                )
        self._started_services.clear()

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
        except Exception as exc:
            logger.error(
                "Failed to process Stream Dock event %s; exception_type=%s",
                event.event_name,
                type(exc).__name__,
            )

    def on_unhandled_event(self, _event: UnknownStreamDockEvent) -> None:
        """Handle an event that has no descriptor in this SDK version.

        Override this no-op hook to observe :class:`UnknownStreamDockEvent`
        instances preserved by the parser. It is invoked once at plugin scope;
        unknown envelopes are not broadcast to action instances because their
        routing semantics are not yet known.

        Args:
            _event: Forward-compatible event containing its raw wire name and
                complete decoded envelope.
        """

        pass

    def _dispatch(self, event: StreamDockEvent) -> None:
        descriptor = self._descriptor_for_event(event)
        if descriptor is None:
            if not isinstance(event, UnknownStreamDockEvent):
                raise AssertionError(
                    f"Known Stream Dock event {event.event_name!r} has no matching descriptor"
                )
            self.on_unhandled_event(event)
            return
        if descriptor.runtime_handler is not None:
            handler = getattr(self, descriptor.runtime_handler)
            handler(event, descriptor)
            return
        self._dispatch_registered_event(event, descriptor)

    @staticmethod
    def _descriptor_for_event(event: StreamDockEvent) -> EventDescriptor | None:
        from .parser import EVENT_REGISTRY

        descriptor = EVENT_REGISTRY.get(event.event_name)
        if descriptor is None or not isinstance(event, descriptor.event_class):
            return None
        return descriptor

    def _dispatch_registered_event(
        self,
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        self.action_store.dispatch(event, descriptor)

    def _handle_will_appear_event(
        self,
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        if not isinstance(event, WillAppearEvent):  # pragma: no cover - registry invariant
            raise AssertionError("willAppear descriptor received the wrong event class")
        self._create_action(event, descriptor)

    def _handle_will_disappear_event(
        self,
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        if not isinstance(event, WillDisappearEvent):  # pragma: no cover - registry invariant
            raise AssertionError("willDisappear descriptor received the wrong event class")
        self.action_store.remove_and_dispatch(event, descriptor)

    def _handle_did_receive_settings_event(
        self,
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        if not isinstance(event, DidReceiveSettingsEvent):  # pragma: no cover
            raise AssertionError("didReceiveSettings descriptor received the wrong event class")
        self.action_store.update_settings_and_dispatch(event, descriptor)

    def _handle_title_parameters_did_change_event(
        self,
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        if not isinstance(event, TitleParametersDidChangeEvent):  # pragma: no cover
            raise AssertionError(
                "titleParametersDidChange descriptor received the wrong event class"
            )
        self.action_store.update_title_and_dispatch(event, descriptor)

    def _handle_did_receive_global_settings_event(
        self,
        event: StreamDockEvent,
        descriptor: EventDescriptor,
    ) -> None:
        if not isinstance(event, DidReceiveGlobalSettingsEvent):  # pragma: no cover
            raise AssertionError(
                "didReceiveGlobalSettings descriptor received the wrong event class"
            )
        source = self.global_settings_store.receive(event.settings)
        self.action_store.broadcast_factory(
            lambda: self.global_settings_store.new_event(source),
            descriptor,
        )

    def _create_action(
        self,
        event: WillAppearEvent,
        descriptor: EventDescriptor,
    ) -> None:
        action = self.action_store.create_and_dispatch(event, descriptor)
        if action is None:
            return

        if self.global_settings_store.loaded:
            global_settings_event = self._new_global_settings_event()
            global_settings_descriptor = self._descriptor_for_event(global_settings_event)
            if global_settings_descriptor is None:  # pragma: no cover - registry invariant
                raise AssertionError("Global settings event is not registered")
            self.action_store.dispatch_safely(
                action,
                global_settings_event,
                global_settings_descriptor,
            )

    def _new_global_settings_event(self) -> DidReceiveGlobalSettingsEvent:
        return self.global_settings_store.new_event()

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

        self.global_settings_store.update(update)

    def set_global_settings(self, settings: JsonObject) -> None:
        """Validate and persist raw plugin-wide settings.

        Local :attr:`global_settings` is updated with an isolated copy only
        after the command is sent successfully.

        Args:
            settings: JSON-compatible object shared by all plugin actions.

        Raises:
            JsonCodecEncodeError: If ``settings`` is not a finite JSON object.
        """

        self.global_settings_store.set(settings)

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

        self.global_settings_store.set_typed(settings, codec)

    def get_global_settings(self) -> None:
        """Request the latest persisted plugin-wide settings.

        The response arrives asynchronously as
        :class:`DidReceiveGlobalSettingsEvent`, updates
        :attr:`global_settings`, and is broadcast to active actions.
        """

        self.stream_dock.send(GetGlobalSettingsCommand(self.plugin_uuid))

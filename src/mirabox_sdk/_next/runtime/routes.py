"""Immutable runtime-only routing policy for typed Stream Dock events."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from ...events import (
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
    StreamDockEventType,
    SystemDidWakeUpEvent,
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from .ports import RuntimeActionCallbacks


class RuntimeEventScope(StrEnum):
    """Application destination selected for one typed event."""

    ACTION = "action"
    BROADCAST = "broadcast"
    PLUGIN = "plugin"


class DispatchOrdering(StrEnum):
    """Scheduling constraint associated with one event route."""

    CONTEXT = "context"
    GLOBAL_BARRIER = "global_barrier"


class RuntimeTransition(StrEnum):
    """State change applied before the selected application callback."""

    NONE = "none"
    CREATE_ACTION = "create_action"
    REMOVE_ACTION = "remove_action"
    UPDATE_ACTION_SETTINGS = "update_action_settings"
    UPDATE_ACTION_TITLE = "update_action_title"
    UPDATE_GLOBAL_SETTINGS = "update_global_settings"


@dataclass(frozen=True, slots=True)
class RuntimeEventRoute:
    """Runtime behavior for one already-decoded event class."""

    event_class: type[StreamDockEvent]
    scope: RuntimeEventScope
    ordering: DispatchOrdering
    callback: str
    transition: RuntimeTransition = RuntimeTransition.NONE

    @property
    def wire_name(self) -> str:
        """Return the known wire name declared by :attr:`event_class`."""

        event_type = getattr(self.event_class, "event", None)
        return event_type.value if isinstance(event_type, StreamDockEventType) else ""


class RuntimeEventRegistryError(RuntimeError):
    """Report an invalid or incomplete runtime route registry."""


class RuntimeEventRouteMismatchError(RuntimeEventRegistryError):
    """Report a typed event whose class disagrees with its known wire route."""

    def __init__(
        self,
        wire_name: str,
        expected: type[StreamDockEvent],
        actual: type[StreamDockEvent],
    ) -> None:
        self.wire_name = wire_name
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"runtime route for {wire_name!r} expects {expected.__name__}, got {actual.__name__}"
        )


@dataclass(frozen=True, slots=True, init=False)
class RuntimeEventRegistry(Mapping[type[StreamDockEvent], RuntimeEventRoute]):
    """Validated immutable routes indexed by event class and wire name."""

    _by_event_class: Mapping[type[StreamDockEvent], RuntimeEventRoute]
    _by_wire_name: Mapping[str, RuntimeEventRoute]
    _routes: tuple[RuntimeEventRoute, ...]

    def __init__(
        self,
        routes: Sequence[RuntimeEventRoute],
        *,
        action_api: type[Any] = RuntimeActionCallbacks,
    ) -> None:
        if not isinstance(action_api, type):
            raise TypeError("action_api must be a class")

        by_event_class: dict[type[StreamDockEvent], RuntimeEventRoute] = {}
        by_wire_name: dict[str, RuntimeEventRoute] = {}
        ordered_routes: list[RuntimeEventRoute] = []

        for route in routes:
            self._validate_route(route, action_api=action_api)
            if route.event_class in by_event_class:
                raise RuntimeEventRegistryError(
                    f"duplicate runtime route for event class {route.event_class.__name__}"
                )
            if route.wire_name in by_wire_name:
                raise RuntimeEventRegistryError(
                    f"duplicate runtime route for wire name {route.wire_name!r}"
                )
            by_event_class[route.event_class] = route
            by_wire_name[route.wire_name] = route
            ordered_routes.append(route)

        expected_wire_names = {event_type.value for event_type in StreamDockEventType}
        actual_wire_names = set(by_wire_name)
        if actual_wire_names != expected_wire_names:
            missing = sorted(expected_wire_names - actual_wire_names)
            extra = sorted(actual_wire_names - expected_wire_names)
            raise RuntimeEventRegistryError(
                "runtime event registry does not match StreamDockEventType; "
                f"missing={missing}, extra={extra}"
            )

        object.__setattr__(self, "_by_event_class", MappingProxyType(by_event_class))
        object.__setattr__(self, "_by_wire_name", MappingProxyType(by_wire_name))
        object.__setattr__(self, "_routes", tuple(ordered_routes))

    @staticmethod
    def _validate_route(
        route: RuntimeEventRoute,
        *,
        action_api: type[Any],
    ) -> None:
        if not isinstance(route, RuntimeEventRoute):
            raise TypeError("routes must contain RuntimeEventRoute instances")
        if not isinstance(route.event_class, type) or not issubclass(
            route.event_class, StreamDockEvent
        ):
            raise RuntimeEventRegistryError("route event_class must extend StreamDockEvent")
        if issubclass(route.event_class, UnknownStreamDockEvent):
            raise RuntimeEventRegistryError("unknown events bypass the runtime route registry")
        event_type = getattr(route.event_class, "event", None)
        if not isinstance(event_type, StreamDockEventType):
            raise RuntimeEventRegistryError(
                f"runtime route class {route.event_class.__name__} has no known event type"
            )
        if not isinstance(route.scope, RuntimeEventScope):
            raise RuntimeEventRegistryError("route scope must be a RuntimeEventScope")
        if not isinstance(route.ordering, DispatchOrdering):
            raise RuntimeEventRegistryError("route ordering must be a DispatchOrdering")
        if not isinstance(route.transition, RuntimeTransition):
            raise RuntimeEventRegistryError("route transition must be a RuntimeTransition")

        is_action_event = issubclass(route.event_class, ActionEvent)
        if (route.scope is RuntimeEventScope.ACTION) != is_action_event:
            raise RuntimeEventRegistryError(
                f"runtime route {route.wire_name!r} has invalid scope {route.scope.value!r}"
            )
        if not isinstance(route.callback, str) or not route.callback:
            raise RuntimeEventRegistryError("route callback must be a non-empty string")
        if not callable(getattr(action_api, route.callback, None)):
            raise RuntimeEventRegistryError(
                f"runtime route {route.wire_name!r} selects missing Action callback "
                f"{route.callback!r}"
            )

    @property
    def routes(self) -> tuple[RuntimeEventRoute, ...]:
        """Return routes in their validated construction order."""

        return self._routes

    def __getitem__(self, event_class: type[StreamDockEvent]) -> RuntimeEventRoute:
        return self._by_event_class[event_class]

    def __iter__(self) -> Iterator[type[StreamDockEvent]]:
        return iter(self._by_event_class)

    def __len__(self) -> int:
        return len(self._by_event_class)

    def get_by_wire_name(self, wire_name: str) -> RuntimeEventRoute | None:
        """Return the route for a known wire name, if present."""

        return self._by_wire_name.get(wire_name)

    def route_for(self, event: StreamDockEvent) -> RuntimeEventRoute | None:
        """Resolve a typed event and bypass forward-compatible unknown events."""

        if not isinstance(event, StreamDockEvent):
            raise TypeError("event must be a StreamDockEvent")
        if isinstance(event, UnknownStreamDockEvent):
            return None

        route = self._by_event_class.get(type(event))
        if route is not None:
            return route

        route = self._by_wire_name.get(event.event_name)
        if route is not None:
            raise RuntimeEventRouteMismatchError(
                event.event_name,
                route.event_class,
                type(event),
            )
        raise RuntimeEventRegistryError(f"typed event {type(event).__name__} has no runtime route")


_DEFAULT_RUNTIME_EVENT_ROUTES: Final = (
    RuntimeEventRoute(
        WillAppearEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_will_appear",
        RuntimeTransition.CREATE_ACTION,
    ),
    RuntimeEventRoute(
        WillDisappearEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_will_disappear",
        RuntimeTransition.REMOVE_ACTION,
    ),
    RuntimeEventRoute(
        DidReceiveSettingsEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_did_receive_settings",
        RuntimeTransition.UPDATE_ACTION_SETTINGS,
    ),
    RuntimeEventRoute(
        TitleParametersDidChangeEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_title_parameters_did_change",
        RuntimeTransition.UPDATE_ACTION_TITLE,
    ),
    RuntimeEventRoute(
        KeyDownEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_key_down",
    ),
    RuntimeEventRoute(
        KeyUpEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_key_up",
    ),
    RuntimeEventRoute(
        TouchTapEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_touch_tap",
    ),
    RuntimeEventRoute(
        DialDownEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_dial_down",
    ),
    RuntimeEventRoute(
        DialUpEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_dial_up",
    ),
    RuntimeEventRoute(
        DialRotateEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_dial_rotate",
    ),
    RuntimeEventRoute(
        PropertyInspectorDidAppearEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_property_inspector_did_appear",
    ),
    RuntimeEventRoute(
        PropertyInspectorDidDisappearEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_property_inspector_did_disappear",
    ),
    RuntimeEventRoute(
        SendToPluginEvent,
        RuntimeEventScope.ACTION,
        DispatchOrdering.CONTEXT,
        "on_send_to_plugin",
    ),
    RuntimeEventRoute(
        DidReceiveGlobalSettingsEvent,
        RuntimeEventScope.BROADCAST,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_did_receive_global_settings",
        RuntimeTransition.UPDATE_GLOBAL_SETTINGS,
    ),
    RuntimeEventRoute(
        DeviceDidConnectEvent,
        RuntimeEventScope.BROADCAST,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_device_did_connect",
    ),
    RuntimeEventRoute(
        DeviceDidDisconnectEvent,
        RuntimeEventScope.BROADCAST,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_device_did_disconnect",
    ),
    RuntimeEventRoute(
        ApplicationDidLaunchEvent,
        RuntimeEventScope.BROADCAST,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_application_did_launch",
    ),
    RuntimeEventRoute(
        ApplicationDidTerminateEvent,
        RuntimeEventScope.BROADCAST,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_application_did_terminate",
    ),
    RuntimeEventRoute(
        SystemDidWakeUpEvent,
        RuntimeEventScope.BROADCAST,
        DispatchOrdering.GLOBAL_BARRIER,
        "on_system_did_wake_up",
    ),
)


RUNTIME_EVENT_REGISTRY: Final = RuntimeEventRegistry(_DEFAULT_RUNTIME_EVENT_ROUTES)

"""Deterministic application fakes for synchronous runtime tests."""

from __future__ import annotations

from dataclasses import dataclass

from mirabox_sdk import (
    Action,
    Controller,
    Coordinates,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    JsonObject,
    KeyDownEvent,
    StreamDockSender,
    SystemDidWakeUpEvent,
    TitleAlignment,
    TitleParameters,
    TitleParametersDidChangeEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from mirabox_sdk._next.runtime.ports import RuntimeActionCallbacks

ACTION_UUID = "com.example.runtime.action"


@dataclass(frozen=True, slots=True)
class FakeDependencies:
    stream_dock: StreamDockSender


class RecordingAction(Action[JsonObject, FakeDependencies]):
    """Action that records callback events and state visible during callbacks."""

    def __init__(
        self,
        action: str,
        context: str,
        settings: JsonObject,
        dependencies: FakeDependencies,
    ) -> None:
        super().__init__(action, context, settings, dependencies)
        self.events: list[object | None] = []
        self.settings_seen: list[object] = []
        self.titles_seen: list[tuple[str, TitleParameters | None]] = []

    def on_will_appear(self, event: WillAppearEvent) -> None:
        self.events.append(event)

    def on_will_disappear(self, event: WillDisappearEvent | None = None) -> None:
        self.events.append(event)

    def on_did_receive_settings(self, event: DidReceiveSettingsEvent) -> None:
        self.events.append(event)
        self.settings_seen.append(self.settings)

    def on_title_parameters_did_change(
        self,
        event: TitleParametersDidChangeEvent,
    ) -> None:
        self.events.append(event)
        self.titles_seen.append((self.title, self.title_parameters))

    def on_key_down(self, event: KeyDownEvent) -> None:
        self.events.append(event)

    def on_did_receive_global_settings(self, event: DidReceiveGlobalSettingsEvent) -> None:
        self.events.append(event)

    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
        self.events.append(event)


class RecordingActionFactory:
    """Factory with configurable action type and explicit unknown UUIDs."""

    def __init__(
        self,
        stream_dock: StreamDockSender,
        action_type: type[RecordingAction] = RecordingAction,
    ) -> None:
        self._dependencies = FakeDependencies(stream_dock)
        self._action_type = action_type
        self.calls: list[tuple[str, str, JsonObject]] = []
        self.unknown_uuids: set[str] = set()
        self.instances: list[RecordingAction] = []

    def create(
        self,
        action_uuid: str,
        context: str,
        initial_settings: JsonObject,
    ) -> RuntimeActionCallbacks | None:
        self.calls.append((action_uuid, context, initial_settings))
        if action_uuid in self.unknown_uuids:
            return None
        action = self._action_type(
            action_uuid,
            context,
            self._action_type.decode_settings(initial_settings),
            self._dependencies,
        )
        self.instances.append(action)
        return action


class RecordingPluginHooks:
    def __init__(self) -> None:
        self.events: list[UnknownStreamDockEvent] = []
        self.error: Exception | None = None

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        self.events.append(event)
        if self.error is not None:
            raise self.error


def will_appear_event(
    *,
    context: str = "button",
    action: str = ACTION_UUID,
    settings: JsonObject | None = None,
) -> WillAppearEvent:
    return WillAppearEvent(
        action=action,
        context=context,
        device="device",
        settings={"count": 1} if settings is None else settings,
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def will_disappear_event(*, context: str = "button") -> WillDisappearEvent:
    return WillDisappearEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def did_receive_settings_event(
    settings: JsonObject,
    *,
    context: str = "button",
) -> DidReceiveSettingsEvent:
    return DidReceiveSettingsEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings=settings,
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def title_event(*, context: str = "button") -> TitleParametersDidChangeEvent:
    parameters = TitleParameters(
        font_family="Arial",
        font_size=14,
        font_style="Bold",
        font_underline=False,
        show_title=True,
        alignment=TitleAlignment.BOTTOM,
        color="#ffffffff",
    )
    return TitleParametersDidChangeEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        title="Updated",
        title_parameters=parameters,
        controller=Controller.KEYPAD,
    )


def key_down_event(*, context: str = "button") -> KeyDownEvent:
    return KeyDownEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )

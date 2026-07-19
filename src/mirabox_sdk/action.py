"""Reusable base class for one action instance on a Stream Dock device."""

from __future__ import annotations

from typing import Any, ClassVar, Generic, TypeVar, cast

from .codecs import JSON_OBJECT_CODEC, JsonCodec, decode_with_codec
from .commands import (
    GetSettingsCommand,
    LogMessageCommand,
    OpenUrlCommand,
    SendToPropertyInspectorCommand,
    SetImageCommand,
    SetSettingsCommand,
    SetStateCommand,
    SetTitleCommand,
    ShowAlertCommand,
    ShowOkCommand,
    StreamDockCommand,
)
from .events import (
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
    SystemDidWakeUpEvent,
    TitleParameters,
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from .json_types import JsonObject
from .protocols import StreamDockActionDependencies

SettingsT = TypeVar("SettingsT")
PayloadT = TypeVar("PayloadT")
DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)


class Action(Generic[SettingsT, DependenciesT]):
    """Base class for one action instance placed on a Stream Dock device."""

    settings_codec: ClassVar[JsonCodec[Any]] = JSON_OBJECT_CODEC

    def __init__(
        self,
        action: str,
        context: str,
        settings: SettingsT,
        dependencies: DependenciesT,
    ) -> None:
        self.action = action
        self.context = context
        self.settings = settings
        self.title = ""
        self.title_parameters: TitleParameters | None = None
        self.dependencies = dependencies
        self._stream_dock = dependencies.stream_dock

    def _send(self, command: StreamDockCommand) -> None:
        self._stream_dock.send(command)

    def send_to_property_inspector(self, payload: JsonObject) -> None:
        self._send(
            SendToPropertyInspectorCommand(
                action=self.action,
                context=self.context,
                payload=payload,
            )
        )

    def send_typed_to_property_inspector(
        self,
        payload: PayloadT,
        codec: JsonCodec[PayloadT],
    ) -> None:
        self._send(
            SendToPropertyInspectorCommand.from_payload(
                action=self.action,
                context=self.context,
                payload=payload,
                codec=codec,
            )
        )

    def set_state(self, state: int) -> None:
        self._send(SetStateCommand(self.context, state))

    def set_title(
        self,
        title: str,
        *,
        target: int = 0,
        state: int | None = None,
    ) -> None:
        self._send(SetTitleCommand(self.context, title, target, state))

    @classmethod
    def decode_settings(cls, settings: JsonObject) -> SettingsT:
        return cast(SettingsT, decode_with_codec(settings, cls.settings_codec))

    def update_settings_from_wire(self, settings: JsonObject) -> None:
        self.settings = self.decode_settings(settings)

    def set_settings(self, settings: SettingsT) -> None:
        command = SetSettingsCommand.from_settings(
            context=self.context,
            settings=settings,
            codec=cast(JsonCodec[SettingsT], self.settings_codec),
        )
        self._send(command)
        self.settings = settings

    def get_settings(self) -> None:
        self._send(GetSettingsCommand(self.context))

    def set_image(
        self,
        image: str,
        *,
        target: int = 0,
        state: int | None = None,
    ) -> None:
        self._send(SetImageCommand(self.context, image, target, state))

    def show_ok(self) -> None:
        self._send(ShowOkCommand(self.context))

    def show_alert(self) -> None:
        self._send(ShowAlertCommand(self.context))

    def open_url(self, url: str) -> None:
        self._send(OpenUrlCommand(url))

    def log_message(self, message: str) -> None:
        self._send(LogMessageCommand(message))

    def on_will_appear(self, _event: WillAppearEvent) -> None:
        pass

    def on_will_disappear(self, _event: WillDisappearEvent | None = None) -> None:
        pass

    def on_did_receive_settings(self, _event: DidReceiveSettingsEvent) -> None:
        pass

    def on_did_receive_global_settings(self, _event: DidReceiveGlobalSettingsEvent) -> None:
        pass

    def on_title_parameters_did_change(
        self,
        _event: TitleParametersDidChangeEvent,
    ) -> None:
        pass

    def on_key_down(self, _event: KeyDownEvent) -> None:
        pass

    def on_key_up(self, _event: KeyUpEvent) -> None:
        pass

    def on_touch_tap(self, _event: TouchTapEvent) -> None:
        pass

    def on_dial_down(self, _event: DialDownEvent) -> None:
        pass

    def on_dial_up(self, _event: DialUpEvent) -> None:
        pass

    def on_dial_rotate(self, _event: DialRotateEvent) -> None:
        pass

    def on_property_inspector_did_appear(
        self,
        _event: PropertyInspectorDidAppearEvent,
    ) -> None:
        pass

    def on_property_inspector_did_disappear(
        self,
        _event: PropertyInspectorDidDisappearEvent,
    ) -> None:
        pass

    def on_send_to_plugin(self, _event: SendToPluginEvent) -> None:
        pass

    def on_device_did_connect(self, _event: DeviceDidConnectEvent) -> None:
        pass

    def on_device_did_disconnect(self, _event: DeviceDidDisconnectEvent) -> None:
        pass

    def on_application_did_launch(self, _event: ApplicationDidLaunchEvent) -> None:
        pass

    def on_application_did_terminate(self, _event: ApplicationDidTerminateEvent) -> None:
        pass

    def on_system_did_wake_up(self, _event: SystemDidWakeUpEvent) -> None:
        pass

"""Typed events received from MiraBox Stream Dock."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeVar

from .codecs import JsonCodec, decode_with_codec
from .errors import JsonCodecDecodeError
from .json_types import JsonObject

DecodedT = TypeVar("DecodedT")


class Controller(StrEnum):
    KEYPAD = "Keypad"
    ENCODER = "Encoder"
    KNOB = "Knob"
    INFORMATION = "Information"
    SECONDARY_SCREEN = "SecondaryScreen"


class TitleAlignment(StrEnum):
    BOTTOM = "bottom"
    MIDDLE = "middle"
    TOP = "top"


class StreamDockEventType(StrEnum):
    WILL_APPEAR = "willAppear"
    WILL_DISAPPEAR = "willDisappear"
    DID_RECEIVE_SETTINGS = "didReceiveSettings"
    TITLE_PARAMETERS_DID_CHANGE = "titleParametersDidChange"
    KEY_DOWN = "keyDown"
    KEY_UP = "keyUp"
    TOUCH_TAP = "touchTap"
    DIAL_DOWN = "dialDown"
    DIAL_UP = "dialUp"
    DIAL_ROTATE = "dialRotate"
    PROPERTY_INSPECTOR_DID_APPEAR = "propertyInspectorDidAppear"
    PROPERTY_INSPECTOR_DID_DISAPPEAR = "propertyInspectorDidDisappear"
    SEND_TO_PLUGIN = "sendToPlugin"
    DID_RECEIVE_GLOBAL_SETTINGS = "didReceiveGlobalSettings"
    DEVICE_DID_CONNECT = "deviceDidConnect"
    DEVICE_DID_DISCONNECT = "deviceDidDisconnect"
    APPLICATION_DID_LAUNCH = "applicationDidLaunch"
    APPLICATION_DID_TERMINATE = "applicationDidTerminate"
    SYSTEM_DID_WAKE_UP = "systemDidWakeUp"


@dataclass(frozen=True, slots=True)
class Coordinates:
    column: int
    row: int


@dataclass(frozen=True, slots=True)
class DeviceSize:
    columns: int
    rows: int


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    name: str
    type: int
    size: DeviceSize


class StreamDockEvent:
    """Base for known and forward-compatible unknown events."""

    @property
    def event_name(self) -> str:
        return str(object.__getattribute__(self, "event"))


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownStreamDockEvent(StreamDockEvent):
    event: str
    data: JsonObject


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionEvent(StreamDockEvent):
    action: str
    context: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceActionEvent(ActionEvent):
    device: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionPayloadEvent(DeviceActionEvent):
    settings: JsonObject
    coordinates: Coordinates

    def decode_settings(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode action-owned settings without coupling the protocol to their schema."""

        try:
            return decode_with_codec(self.settings, codec)
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                event_name=self.event_name,
                path=("payload", "settings", *exc.path),
            ) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class WillAppearEvent(ActionPayloadEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.WILL_APPEAR
    controller: Controller
    is_in_multi_action: bool
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WillDisappearEvent(ActionPayloadEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.WILL_DISAPPEAR
    controller: Controller
    is_in_multi_action: bool
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DidReceiveSettingsEvent(ActionPayloadEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.DID_RECEIVE_SETTINGS
    is_in_multi_action: bool
    controller: Controller | None = None
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyEvent(ActionPayloadEvent):
    is_in_multi_action: bool
    controller: Controller | None = None
    state: int | None = None
    user_desired_state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyDownEvent(KeyEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.KEY_DOWN


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyUpEvent(KeyEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.KEY_UP


@dataclass(frozen=True, slots=True, kw_only=True)
class TouchTapEvent(KeyEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.TOUCH_TAP


@dataclass(frozen=True, slots=True, kw_only=True)
class DialPressEvent(ActionPayloadEvent):
    controller: Controller


@dataclass(frozen=True, slots=True, kw_only=True)
class DialDownEvent(DialPressEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.DIAL_DOWN


@dataclass(frozen=True, slots=True, kw_only=True)
class DialUpEvent(DialPressEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.DIAL_UP


@dataclass(frozen=True, slots=True, kw_only=True)
class DialRotateEvent(ActionPayloadEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.DIAL_ROTATE
    ticks: int
    pressed: bool
    controller: Controller | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyInspectorDidAppearEvent(DeviceActionEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.PROPERTY_INSPECTOR_DID_APPEAR


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyInspectorDidDisappearEvent(DeviceActionEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.PROPERTY_INSPECTOR_DID_DISAPPEAR


@dataclass(frozen=True, slots=True)
class PropertyInspectorMessage:
    name: str | None
    value: JsonObject

    def decode(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode a plugin-defined Property Inspector message."""

        try:
            return decode_with_codec(self.value, codec)
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                path=("payload", *exc.path),
            ) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class SendToPluginEvent(ActionEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.SEND_TO_PLUGIN
    message: PropertyInspectorMessage
    device: str | None = None

    def decode_message(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode the Property Inspector payload and attach event diagnostics."""

        try:
            return self.message.decode(codec)
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                event_name=self.event_name,
                path=exc.path,
            ) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class DidReceiveGlobalSettingsEvent(StreamDockEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.DID_RECEIVE_GLOBAL_SETTINGS
    settings: JsonObject

    def decode_settings(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode plugin-owned global settings."""

        try:
            return decode_with_codec(self.settings, codec)
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                event_name=self.event_name,
                path=("payload", "settings", *exc.path),
            ) from exc


@dataclass(frozen=True, slots=True)
class TitleParameters:
    font_family: str
    font_size: int
    font_style: str
    font_underline: bool
    show_title: bool
    alignment: TitleAlignment
    color: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TitleParametersDidChangeEvent(ActionPayloadEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.TITLE_PARAMETERS_DID_CHANGE
    title: str
    title_parameters: TitleParameters
    controller: Controller | None = None
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceDidConnectEvent(StreamDockEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.DEVICE_DID_CONNECT
    device: str
    info: DeviceInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceDidDisconnectEvent(StreamDockEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.DEVICE_DID_DISCONNECT
    device: str
    info: DeviceInfo | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationDidLaunchEvent(StreamDockEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.APPLICATION_DID_LAUNCH
    application: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationDidTerminateEvent(StreamDockEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.APPLICATION_DID_TERMINATE
    application: str


@dataclass(frozen=True, slots=True)
class SystemDidWakeUpEvent(StreamDockEvent):
    event: ClassVar[StreamDockEventType] = StreamDockEventType.SYSTEM_DID_WAKE_UP

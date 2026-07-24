"""Typed events received from MiraBox Stream Dock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeVar

from .codecs import JsonCodec, decode_with_codec
from .errors import JsonCodecDecodeError
from .json_types import JsonObject

DecodedT = TypeVar("DecodedT")


class Controller(StrEnum):
    """Control surface that produced an action event.

    Values are preserved exactly as Stream Dock sends them. ``KEYPAD`` covers
    ordinary keys, ``ENCODER`` and ``KNOB`` cover rotary controls,
    ``INFORMATION`` represents an information surface, and
    ``SECONDARY_SCREEN`` represents an auxiliary display.
    """

    KEYPAD = "Keypad"
    ENCODER = "Encoder"
    KNOB = "Knob"
    INFORMATION = "Information"
    SECONDARY_SCREEN = "SecondaryScreen"


class TitleAlignment(StrEnum):
    """Vertical alignment reported for an action title."""

    BOTTOM = "bottom"
    MIDDLE = "middle"
    TOP = "top"


class StreamDockEventType(StrEnum):
    """Wire names of all incoming events understood by this SDK version."""

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
    """Zero-based location of an action on a Stream Dock layout.

    Attributes:
        column: Horizontal grid index counted from the left.
        row: Vertical grid index counted from the top.
    """

    column: int
    row: int


@dataclass(frozen=True, slots=True)
class DeviceSize:
    """Grid dimensions reported for a Stream Dock device.

    Attributes:
        columns: Number of action columns on the device.
        rows: Number of action rows on the device.
    """

    columns: int
    rows: int


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Metadata supplied when a Stream Dock device connects.

    Attributes:
        name: User-visible device name.
        type: Numeric device type assigned by Stream Dock.
        size: Available action-grid dimensions.
    """

    name: str
    type: int
    size: DeviceSize


class StreamDockEvent:
    """Base class for known and forward-compatible unknown events.

    Concrete known events expose a class-level :class:`StreamDockEventType` as
    ``event``. :class:`UnknownStreamDockEvent` stores the unrecognized wire name
    on the instance instead.
    """

    @property
    def event_name(self) -> str:
        """Return the event's raw protocol name as a plain string."""

        return str(object.__getattribute__(self, "event"))


class EventScope(StrEnum):
    """Runtime destination for a recognized Stream Dock event."""

    ACTION = "action"
    BROADCAST = "broadcast"


EventParser = Callable[[JsonObject, str], StreamDockEvent]


@dataclass(frozen=True, slots=True)
class EventDescriptor:
    """Single source of parser and runtime dispatch metadata for one event.

    Attributes:
        wire_name: Exact event name received from Stream Dock.
        event_class: Typed model returned by the parser.
        parser: Function that validates an envelope and constructs the model.
        scope: Whether the event targets one action or all active actions.
        callback: :class:`Action` callback selected by the runtime.
        runtime_handler: Optional runtime method for events that update state or
            create/remove action instances before invoking ``callback``.
    """

    wire_name: str
    event_class: type[StreamDockEvent]
    parser: EventParser
    scope: EventScope
    callback: str
    runtime_handler: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownStreamDockEvent(StreamDockEvent):
    """A structurally valid event whose name is not known to this SDK version.

    Attributes:
        event: Unrecognized wire event name.
        data: Deep copy of the complete decoded event envelope, allowing newer
            protocol data to be inspected without stopping the plugin.
    """

    event: str
    data: JsonObject


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionEvent(StreamDockEvent):
    """Base event routed to one action instance.

    Attributes:
        action: Exact action UUID declared in the plugin manifest.
        context: Opaque identifier of the concrete action instance.
    """

    action: str
    context: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceActionEvent(ActionEvent):
    """Action event that also identifies the originating device.

    Attributes:
        device: Opaque Stream Dock device identifier.
    """

    device: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionPayloadEvent(DeviceActionEvent):
    """Action event carrying settings and layout coordinates.

    Attributes:
        settings: Raw persisted settings received for the action context.
        coordinates: Zero-based location of the action on its device.
    """

    settings: JsonObject
    coordinates: Coordinates

    def decode_settings(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode action-owned settings with a plugin-supplied codec.

        Args:
            codec: Codec for the plugin's settings type.

        Returns:
            Decoded settings value returned by ``codec``.

        Raises:
            JsonCodecDecodeError: If decoding fails. The error includes this
                event's name and the ``$.payload.settings`` wire path.
        """

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
    """Report that an action context became visible or active.

    The runtime uses this event to construct the registered :class:`Action`.

    Attributes:
        controller: Control surface hosting the action.
        is_in_multi_action: Whether the action belongs to a multi-action.
        state: Current zero-based state when supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.WILL_APPEAR
    controller: Controller
    is_in_multi_action: bool
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WillDisappearEvent(ActionPayloadEvent):
    """Report that an action context is no longer visible or active.

    Attributes:
        controller: Control surface that hosted the action.
        is_in_multi_action: Whether the action belonged to a multi-action.
        state: Last zero-based state when supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.WILL_DISAPPEAR
    controller: Controller
    is_in_multi_action: bool
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DidReceiveSettingsEvent(ActionPayloadEvent):
    """Deliver the latest persisted settings for an action context.

    Attributes:
        is_in_multi_action: Whether the action belongs to a multi-action.
        controller: Originating control surface when supplied by Stream Dock.
        state: Current zero-based state when supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.DID_RECEIVE_SETTINGS
    is_in_multi_action: bool
    controller: Controller | None = None
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyEvent(ActionPayloadEvent):
    """Common payload for key press, release, and touch-tap events.

    Attributes:
        is_in_multi_action: Whether the action belongs to a multi-action.
        controller: Originating control surface when supplied by Stream Dock.
        state: Current zero-based state when supplied by Stream Dock.
        user_desired_state: State requested by the user interaction, when the
            host reports one separately from the current state.
    """

    is_in_multi_action: bool
    controller: Controller | None = None
    state: int | None = None
    user_desired_state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyDownEvent(KeyEvent):
    """Report that the user pressed a key or equivalent virtual control."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.KEY_DOWN


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyUpEvent(KeyEvent):
    """Report that the user released a key or equivalent virtual control."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.KEY_UP


@dataclass(frozen=True, slots=True, kw_only=True)
class TouchTapEvent(KeyEvent):
    """Report a tap on a touch-capable control surface."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.TOUCH_TAP


@dataclass(frozen=True, slots=True, kw_only=True)
class DialPressEvent(ActionPayloadEvent):
    """Common payload for pressing or releasing a rotary control.

    Attributes:
        controller: Rotary control surface that produced the event.
    """

    controller: Controller


@dataclass(frozen=True, slots=True, kw_only=True)
class DialDownEvent(DialPressEvent):
    """Report that the user pressed an encoder or dial."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.DIAL_DOWN


@dataclass(frozen=True, slots=True, kw_only=True)
class DialUpEvent(DialPressEvent):
    """Report that the user released an encoder or dial."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.DIAL_UP


@dataclass(frozen=True, slots=True, kw_only=True)
class DialRotateEvent(ActionPayloadEvent):
    """Report rotation of an encoder or dial.

    Attributes:
        ticks: Signed number of rotation steps; the sign indicates direction.
        pressed: Whether the rotary control was held down while rotating.
        controller: Originating control surface when supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.DIAL_ROTATE
    ticks: int
    pressed: bool
    controller: Controller | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyInspectorDidAppearEvent(DeviceActionEvent):
    """Report that the settings UI for an action context was opened."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.PROPERTY_INSPECTOR_DID_APPEAR


@dataclass(frozen=True, slots=True, kw_only=True)
class PropertyInspectorDidDisappearEvent(DeviceActionEvent):
    """Report that the settings UI for an action context was closed."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.PROPERTY_INSPECTOR_DID_DISAPPEAR


@dataclass(frozen=True, slots=True)
class PropertyInspectorMessage:
    """Normalized plugin-defined message from a Property Inspector.

    The complete ``sendToPlugin`` payload is preserved in :attr:`value`. When
    that object contains a string ``event`` field, the parser also exposes it as
    :attr:`name` for convenient message dispatch; all other fields remain
    unchanged in :attr:`value`.

    Attributes:
        name: Optional plugin-defined message name.
        value: Plugin-defined JSON object containing the message body.
    """

    name: str | None
    value: JsonObject

    def decode(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode the message body with a plugin-supplied codec.

        Args:
            codec: Codec for the expected message type.

        Returns:
            Decoded message returned by ``codec``.

        Raises:
            JsonCodecDecodeError: If decoding fails. The error path is rooted
                at the event's ``payload`` field.
        """

        try:
            return decode_with_codec(self.value, codec)
        except JsonCodecDecodeError as exc:
            raise JsonCodecDecodeError(
                exc.reason,
                path=("payload", *exc.path),
            ) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class SendToPluginEvent(ActionEvent):
    """Deliver a message from a Property Inspector to its action instance.

    Attributes:
        message: Normalized plugin-defined message.
        device: Originating device identifier when supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.SEND_TO_PLUGIN
    message: PropertyInspectorMessage
    device: str | None = None

    def decode_message(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode the Property Inspector payload and attach event diagnostics.

        Args:
            codec: Codec for the expected message type.

        Returns:
            Decoded message returned by ``codec``.

        Raises:
            JsonCodecDecodeError: If decoding fails. The error includes the
                ``sendToPlugin`` event name and payload path.
        """

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
    """Deliver settings shared by every action in the plugin.

    Attributes:
        settings: Raw plugin-wide settings object received from Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.DID_RECEIVE_GLOBAL_SETTINGS
    settings: JsonObject

    def decode_settings(self, codec: JsonCodec[DecodedT]) -> DecodedT:
        """Decode plugin-owned global settings with a supplied codec.

        Args:
            codec: Codec for the plugin's global settings type.

        Returns:
            Decoded settings returned by ``codec``.

        Raises:
            JsonCodecDecodeError: If decoding fails. The error includes this
                event's name and the ``$.payload.settings`` wire path.
        """

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
    """Formatting currently applied to an action title.

    Attributes:
        font_family: Font family selected in Stream Dock.
        font_size: Font size reported by Stream Dock.
        font_style: Protocol font-style value.
        font_underline: Whether the title is underlined.
        show_title: Whether Stream Dock should render the title.
        alignment: Vertical title alignment.
        color: Protocol color string, typically an RGBA hex value.
    """

    font_family: str
    font_size: int
    font_style: str
    font_underline: bool
    show_title: bool
    alignment: TitleAlignment
    color: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TitleParametersDidChangeEvent(ActionPayloadEvent):
    """Report a change to an action's title or title formatting.

    Attributes:
        title: Current title text.
        title_parameters: Current title formatting settings.
        controller: Originating control surface when supplied by Stream Dock.
        state: Current zero-based state when supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.TITLE_PARAMETERS_DID_CHANGE
    title: str
    title_parameters: TitleParameters
    controller: Controller | None = None
    state: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceDidConnectEvent(StreamDockEvent):
    """Report that a Stream Dock device connected.

    Attributes:
        device: Opaque device identifier used by later events.
        info: Device name, numeric type, and grid dimensions.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.DEVICE_DID_CONNECT
    device: str
    info: DeviceInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceDidDisconnectEvent(StreamDockEvent):
    """Report that a Stream Dock device disconnected.

    Attributes:
        device: Opaque identifier of the disconnected device.
        info: Device metadata when supplied by the host, otherwise ``None``.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.DEVICE_DID_DISCONNECT
    device: str
    info: DeviceInfo | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationDidLaunchEvent(StreamDockEvent):
    """Report that an application monitored by Stream Dock launched.

    Attributes:
        application: Application identifier supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.APPLICATION_DID_LAUNCH
    application: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationDidTerminateEvent(StreamDockEvent):
    """Report that an application monitored by Stream Dock terminated.

    Attributes:
        application: Application identifier supplied by Stream Dock.
    """

    event: ClassVar[StreamDockEventType] = StreamDockEventType.APPLICATION_DID_TERMINATE
    application: str


@dataclass(frozen=True, slots=True)
class SystemDidWakeUpEvent(StreamDockEvent):
    """Report that the host system resumed from sleep."""

    event: ClassVar[StreamDockEventType] = StreamDockEventType.SYSTEM_DID_WAKE_UP

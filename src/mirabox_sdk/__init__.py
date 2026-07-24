"""Build typed Python plugins for MiraBox Stream Dock.

The package-level namespace is the supported public API. It provides an action
runtime, immutable incoming-event and outgoing-command models, WebSocket
transport, typed JSON codecs, executable launch validation, logging, and the
version-matched Property Inspector browser client.

Typical applications register :class:`Action` subclasses in an
:class:`ActionRegistry`, construct :class:`StreamDockPlugin` with a
:class:`WebSocketStreamDockConnection`, and pass the resulting factory to
:func:`run_plugin_cli`.

SDK logging is silent by default. Call :func:`configure_logging` explicitly when
transport or event-dispatch diagnostics are required.
"""

__version__ = "0.3.1"

from .action import Action
from .action_registry import ActionRegistry
from .cli import build_plugin_argument_parser, parse_plugin_cli_arguments, run_plugin_cli
from .codecs import (
    JSON_OBJECT_CODEC,
    FunctionalJsonCodec,
    JsonCodec,
    JsonObjectCodec,
    decode_with_codec,
    encode_with_codec,
)
from .commands import (
    GetGlobalSettingsCommand,
    GetSettingsCommand,
    LogMessageCommand,
    OpenUrlCommand,
    RegisterPluginCommand,
    SendToPropertyInspectorCommand,
    SetGlobalSettingsCommand,
    SetImageCommand,
    SetSettingsCommand,
    SetStateCommand,
    SetTitleCommand,
    ShowAlertCommand,
    ShowOkCommand,
    StreamDockCommand,
    ValidatedWireMessage,
)
from .connection import WebSocketStreamDockConnection
from .errors import (
    InvalidFieldError,
    InvalidPluginLaunchArgumentsError,
    InvalidRegistrationInfoError,
    JsonCodecDecodeError,
    JsonCodecEncodeError,
    JsonCodecError,
    MalformedEventError,
    StreamDockProtocolError,
    UnsupportedEventError,
)
from .events import (
    ActionEvent,
    ActionPayloadEvent,
    ApplicationDidLaunchEvent,
    ApplicationDidTerminateEvent,
    Controller,
    Coordinates,
    DeviceActionEvent,
    DeviceDidConnectEvent,
    DeviceDidDisconnectEvent,
    DeviceInfo,
    DeviceSize,
    DialDownEvent,
    DialPressEvent,
    DialRotateEvent,
    DialUpEvent,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    KeyDownEvent,
    KeyEvent,
    KeyUpEvent,
    PropertyInspectorDidAppearEvent,
    PropertyInspectorDidDisappearEvent,
    PropertyInspectorMessage,
    SendToPluginEvent,
    StreamDockEvent,
    StreamDockEventType,
    SystemDidWakeUpEvent,
    TitleAlignment,
    TitleParameters,
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from .json_types import JsonObject, JsonValue, OwnedJsonPayload, ValidatedJsonObject
from .logging_config import configure_logging
from .parser import parse_stream_dock_event
from .plugin import StreamDockPlugin
from .protocols import (
    LifecycleService,
    PluginApplication,
    StreamDockActionDependencies,
    StreamDockConnection,
    StreamDockListener,
    StreamDockSender,
)
from .registration import (
    PluginLaunchArguments,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationDeviceInfo,
    RegistrationInfo,
    RegistrationPluginInfo,
    parse_plugin_launch_arguments,
    parse_registration_info,
)
from .resources import (
    PROPERTY_INSPECTOR_CLIENT_FILENAME,
    copy_property_inspector_client,
    property_inspector_client_bytes,
)

__all__ = [
    "__version__",
    "Action",
    "ActionRegistry",
    "ActionEvent",
    "ActionPayloadEvent",
    "ApplicationDidLaunchEvent",
    "ApplicationDidTerminateEvent",
    "Controller",
    "Coordinates",
    "DeviceActionEvent",
    "DeviceDidConnectEvent",
    "DeviceDidDisconnectEvent",
    "DeviceInfo",
    "DeviceSize",
    "DialDownEvent",
    "DialPressEvent",
    "DialRotateEvent",
    "DialUpEvent",
    "DidReceiveGlobalSettingsEvent",
    "DidReceiveSettingsEvent",
    "GetGlobalSettingsCommand",
    "GetSettingsCommand",
    "FunctionalJsonCodec",
    "InvalidFieldError",
    "InvalidPluginLaunchArgumentsError",
    "InvalidRegistrationInfoError",
    "JSON_OBJECT_CODEC",
    "JsonObject",
    "JsonObjectCodec",
    "JsonCodec",
    "JsonCodecDecodeError",
    "JsonCodecEncodeError",
    "JsonCodecError",
    "JsonValue",
    "KeyDownEvent",
    "KeyEvent",
    "KeyUpEvent",
    "LifecycleService",
    "LogMessageCommand",
    "MalformedEventError",
    "OpenUrlCommand",
    "OwnedJsonPayload",
    "PropertyInspectorDidAppearEvent",
    "PropertyInspectorDidDisappearEvent",
    "PropertyInspectorMessage",
    "PluginLaunchArguments",
    "PluginApplication",
    "PROPERTY_INSPECTOR_CLIENT_FILENAME",
    "RegisterPluginCommand",
    "RegistrationApplicationInfo",
    "RegistrationColors",
    "RegistrationDeviceInfo",
    "RegistrationInfo",
    "RegistrationPluginInfo",
    "SendToPluginEvent",
    "SendToPropertyInspectorCommand",
    "SetGlobalSettingsCommand",
    "SetImageCommand",
    "SetSettingsCommand",
    "SetStateCommand",
    "SetTitleCommand",
    "ShowAlertCommand",
    "ShowOkCommand",
    "StreamDockCommand",
    "StreamDockActionDependencies",
    "StreamDockConnection",
    "StreamDockEvent",
    "StreamDockEventType",
    "StreamDockListener",
    "StreamDockPlugin",
    "StreamDockProtocolError",
    "StreamDockSender",
    "SystemDidWakeUpEvent",
    "TitleAlignment",
    "TitleParameters",
    "TitleParametersDidChangeEvent",
    "TouchTapEvent",
    "UnknownStreamDockEvent",
    "UnsupportedEventError",
    "ValidatedJsonObject",
    "ValidatedWireMessage",
    "WebSocketStreamDockConnection",
    "WillAppearEvent",
    "WillDisappearEvent",
    "build_plugin_argument_parser",
    "configure_logging",
    "copy_property_inspector_client",
    "decode_with_codec",
    "encode_with_codec",
    "parse_plugin_cli_arguments",
    "parse_plugin_launch_arguments",
    "parse_registration_info",
    "parse_stream_dock_event",
    "property_inspector_client_bytes",
    "run_plugin_cli",
]

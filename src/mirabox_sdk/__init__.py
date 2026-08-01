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

__version__ = "0.4.0"

from typing import TYPE_CHECKING

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
    EventDescriptor,
    EventScope,
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
from .inbound import InboundOverflowPolicy, InboundQueueMetrics
from .json_types import JsonObject, JsonValue, OwnedJsonPayload, ValidatedJsonObject
from .logging_config import LoggingOverflowPolicy, configure_logging, dropped_log_records
from .outbound import (
    CommandFuture,
    OutboundCommandBusClosedError,
    OutboundCommandBusError,
    OutboundQueueFullError,
    OutboundQueueMetrics,
)
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
from .stores import ActionStore, GlobalSettingsStore

if TYPE_CHECKING:
    from collections.abc import Mapping

    EVENT_REGISTRY: Mapping[str, EventDescriptor]


def __getattr__(name: str) -> object:
    """Lazily expose migration-only compatibility objects."""

    if name == "EVENT_REGISTRY":
        from .parser import EVENT_REGISTRY

        globals()[name] = EVENT_REGISTRY
        return EVENT_REGISTRY
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    "Action",
    "ActionRegistry",
    "ActionStore",
    "ActionEvent",
    "ActionPayloadEvent",
    "ApplicationDidLaunchEvent",
    "ApplicationDidTerminateEvent",
    "CommandFuture",
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
    "EVENT_REGISTRY",
    "EventDescriptor",
    "EventScope",
    "GetGlobalSettingsCommand",
    "GetSettingsCommand",
    "GlobalSettingsStore",
    "FunctionalJsonCodec",
    "InvalidFieldError",
    "InvalidPluginLaunchArgumentsError",
    "InvalidRegistrationInfoError",
    "InboundOverflowPolicy",
    "InboundQueueMetrics",
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
    "LoggingOverflowPolicy",
    "MalformedEventError",
    "OpenUrlCommand",
    "OutboundCommandBusClosedError",
    "OutboundCommandBusError",
    "OutboundQueueFullError",
    "OutboundQueueMetrics",
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
    "dropped_log_records",
    "encode_with_codec",
    "parse_plugin_cli_arguments",
    "parse_plugin_launch_arguments",
    "parse_registration_info",
    "parse_stream_dock_event",
    "property_inspector_client_bytes",
    "run_plugin_cli",
]

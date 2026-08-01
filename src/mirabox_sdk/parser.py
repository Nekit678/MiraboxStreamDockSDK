"""Strict conversion from decoded JSON values to typed MiraBox events."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, NoReturn

from .errors import InvalidFieldError, MalformedEventError, UnsupportedEventError
from .events import (
    ApplicationDidLaunchEvent,
    ApplicationDidTerminateEvent,
    Controller,
    Coordinates,
    DeviceDidConnectEvent,
    DeviceDidDisconnectEvent,
    DeviceInfo,
    DeviceSize,
    DialDownEvent,
    DialRotateEvent,
    DialUpEvent,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    EventCodecDescriptor,
    KeyDownEvent,
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
from .json_types import JsonObject, clone_json_object, is_json_value

if TYPE_CHECKING:
    from .events import EventDescriptor

    EVENT_REGISTRY: Mapping[str, EventDescriptor]


def _invalid(
    event_name: str | None,
    path: tuple[str | int, ...],
    reason: str,
) -> NoReturn:
    raise InvalidFieldError(reason, event_name=event_name, path=path)


def _clone_event_object(value: object) -> JsonObject:
    try:
        return clone_json_object(value)
    except ValueError:
        raise MalformedEventError("message contains a non-JSON value") from None


def _require_string(
    data: JsonObject,
    key: str,
    event_name: str | None,
    path: tuple[str | int, ...] = (),
) -> str:
    field_path = (*path, key)
    if key not in data:
        _invalid(event_name, field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, str):
        _invalid(event_name, field_path, "expected string")
    return value


def _optional_string(
    data: JsonObject,
    key: str,
    event_name: str,
    path: tuple[str | int, ...] = (),
) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str):
        _invalid(event_name, (*path, key), "expected string")
    return value


def _require_int(
    data: JsonObject,
    key: str,
    event_name: str,
    path: tuple[str | int, ...] = (),
) -> int:
    field_path = (*path, key)
    if key not in data:
        _invalid(event_name, field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        _invalid(event_name, field_path, "expected integer")
    return value


def _optional_int(
    data: JsonObject,
    key: str,
    event_name: str,
    path: tuple[str | int, ...] = (),
) -> int | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        _invalid(event_name, (*path, key), "expected integer")
    return value


def _require_bool(
    data: JsonObject,
    key: str,
    event_name: str,
    path: tuple[str | int, ...] = (),
) -> bool:
    field_path = (*path, key)
    if key not in data:
        _invalid(event_name, field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, bool):
        _invalid(event_name, field_path, "expected boolean")
    return value


def _require_object(
    data: JsonObject,
    key: str,
    event_name: str,
    path: tuple[str | int, ...] = (),
) -> JsonObject:
    field_path = (*path, key)
    if key not in data:
        _invalid(event_name, field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, dict):
        _invalid(event_name, field_path, "expected object")
    return value


def _controller(
    payload: JsonObject,
    event_name: str,
    *,
    required: bool,
) -> Controller | None:
    raw_controller = (
        _require_string(payload, "controller", event_name, ("payload",))
        if required
        else _optional_string(payload, "controller", event_name, ("payload",))
    )
    if raw_controller is None:
        return None
    try:
        return Controller(raw_controller)
    except ValueError:
        supported = ", ".join(repr(item.value) for item in Controller)
        _invalid(
            event_name,
            ("payload", "controller"),
            f"unsupported controller {raw_controller!r}; expected one of {supported}",
        )


def _coordinates(payload: JsonObject, event_name: str) -> Coordinates:
    value = _require_object(payload, "coordinates", event_name, ("payload",))
    return Coordinates(
        column=_require_int(value, "column", event_name, ("payload", "coordinates")),
        row=_require_int(value, "row", event_name, ("payload", "coordinates")),
    )


def _device_info(
    data: JsonObject,
    event_name: str,
    *,
    required: bool,
) -> DeviceInfo | None:
    if "deviceInfo" not in data:
        if required:
            _invalid(event_name, ("deviceInfo",), "required field is missing")
        return None
    value = _require_object(data, "deviceInfo", event_name)
    size = _require_object(value, "size", event_name, ("deviceInfo",))
    return DeviceInfo(
        name=_require_string(value, "name", event_name, ("deviceInfo",)),
        type=_require_int(value, "type", event_name, ("deviceInfo",)),
        size=DeviceSize(
            columns=_require_int(size, "columns", event_name, ("deviceInfo", "size")),
            rows=_require_int(size, "rows", event_name, ("deviceInfo", "size")),
        ),
    )


def _title_parameters(payload: JsonObject, event_name: str) -> TitleParameters:
    value = _require_object(payload, "titleParameters", event_name, ("payload",))
    raw_alignment = _require_string(
        value,
        "titleAlignment",
        event_name,
        ("payload", "titleParameters"),
    )
    try:
        alignment = TitleAlignment(raw_alignment)
    except ValueError:
        supported = ", ".join(repr(item.value) for item in TitleAlignment)
        _invalid(
            event_name,
            ("payload", "titleParameters", "titleAlignment"),
            f"unsupported alignment {raw_alignment!r}; expected one of {supported}",
        )
    path = ("payload", "titleParameters")
    return TitleParameters(
        font_family=_require_string(value, "fontFamily", event_name, path),
        font_size=_require_int(value, "fontSize", event_name, path),
        font_style=_require_string(value, "fontStyle", event_name, path),
        font_underline=_require_bool(value, "fontUnderline", event_name, path),
        show_title=_require_bool(value, "showTitle", event_name, path),
        alignment=alignment,
        color=_require_string(value, "titleColor", event_name, path),
    )


def _action_identity(
    data: JsonObject,
    event_name: str,
    *,
    require_device: bool,
) -> tuple[str, str, str | None]:
    action = _require_string(data, "action", event_name)
    context = _require_string(data, "context", event_name)
    device = (
        _require_string(data, "device", event_name)
        if require_device
        else _optional_string(data, "device", event_name)
    )
    return action, context, device


def _action_payload(
    data: JsonObject,
    event_name: str,
) -> tuple[str, str, str, JsonObject, Coordinates, JsonObject]:
    action, context, device = _action_identity(data, event_name, require_device=True)
    if device is None:  # pragma: no cover - narrowed by require_device
        raise AssertionError("required device was not parsed")
    payload = _require_object(data, "payload", event_name)
    settings = _clone_event_object(_require_object(payload, "settings", event_name, ("payload",)))
    return action, context, device, settings, _coordinates(payload, event_name), payload


def _parse_visibility_event(
    data: JsonObject,
    event_name: str,
    event_class: type[WillAppearEvent] | type[WillDisappearEvent],
) -> WillAppearEvent | WillDisappearEvent:
    action, context, device, settings, coordinates, payload = _action_payload(data, event_name)
    return event_class(
        action=action,
        context=context,
        device=device,
        settings=settings,
        coordinates=coordinates,
        controller=_controller(payload, event_name, required=True),
        is_in_multi_action=_require_bool(
            payload,
            "isInMultiAction",
            event_name,
            ("payload",),
        ),
        state=_optional_int(payload, "state", event_name, ("payload",)),
    )


def _parse_will_appear(data: JsonObject, event_name: str) -> WillAppearEvent:
    return _parse_visibility_event(data, event_name, WillAppearEvent)


def _parse_will_disappear(data: JsonObject, event_name: str) -> WillDisappearEvent:
    return _parse_visibility_event(data, event_name, WillDisappearEvent)


def _parse_did_receive_settings(
    data: JsonObject,
    event_name: str,
) -> DidReceiveSettingsEvent:
    action, context, device, settings, coordinates, payload = _action_payload(data, event_name)
    return DidReceiveSettingsEvent(
        action=action,
        context=context,
        device=device,
        settings=settings,
        coordinates=coordinates,
        is_in_multi_action=_require_bool(
            payload,
            "isInMultiAction",
            event_name,
            ("payload",),
        ),
        controller=_controller(payload, event_name, required=False),
        state=_optional_int(payload, "state", event_name, ("payload",)),
    )


def _parse_key_event(
    data: JsonObject,
    event_name: str,
    event_class: type[KeyDownEvent] | type[KeyUpEvent] | type[TouchTapEvent],
) -> KeyDownEvent | KeyUpEvent | TouchTapEvent:
    action, context, device, settings, coordinates, payload = _action_payload(data, event_name)
    return event_class(
        action=action,
        context=context,
        device=device,
        settings=settings,
        coordinates=coordinates,
        is_in_multi_action=_require_bool(
            payload,
            "isInMultiAction",
            event_name,
            ("payload",),
        ),
        controller=_controller(payload, event_name, required=False),
        state=_optional_int(payload, "state", event_name, ("payload",)),
        user_desired_state=_optional_int(
            payload,
            "userDesiredState",
            event_name,
            ("payload",),
        ),
    )


def _parse_key_down(data: JsonObject, event_name: str) -> KeyDownEvent:
    return _parse_key_event(data, event_name, KeyDownEvent)


def _parse_key_up(data: JsonObject, event_name: str) -> KeyUpEvent:
    return _parse_key_event(data, event_name, KeyUpEvent)


def _parse_touch_tap(data: JsonObject, event_name: str) -> TouchTapEvent:
    return _parse_key_event(data, event_name, TouchTapEvent)


def _parse_dial_press_event(
    data: JsonObject,
    event_name: str,
    event_class: type[DialDownEvent] | type[DialUpEvent],
) -> DialDownEvent | DialUpEvent:
    action, context, device, settings, coordinates, payload = _action_payload(data, event_name)
    controller = _controller(payload, event_name, required=True)
    if controller is None:  # pragma: no cover - narrowed by required
        raise AssertionError("required controller was not parsed")
    return event_class(
        action=action,
        context=context,
        device=device,
        settings=settings,
        coordinates=coordinates,
        controller=controller,
    )


def _parse_dial_down(data: JsonObject, event_name: str) -> DialDownEvent:
    return _parse_dial_press_event(data, event_name, DialDownEvent)


def _parse_dial_up(data: JsonObject, event_name: str) -> DialUpEvent:
    return _parse_dial_press_event(data, event_name, DialUpEvent)


def _parse_dial_rotate(data: JsonObject, event_name: str) -> DialRotateEvent:
    action, context, device, settings, coordinates, payload = _action_payload(data, event_name)
    return DialRotateEvent(
        action=action,
        context=context,
        device=device,
        settings=settings,
        coordinates=coordinates,
        ticks=_require_int(payload, "ticks", event_name, ("payload",)),
        pressed=_require_bool(payload, "pressed", event_name, ("payload",)),
        controller=_controller(payload, event_name, required=False),
    )


def _parse_title_parameters_did_change(
    data: JsonObject,
    event_name: str,
) -> TitleParametersDidChangeEvent:
    action, context, device, settings, coordinates, payload = _action_payload(data, event_name)
    return TitleParametersDidChangeEvent(
        action=action,
        context=context,
        device=device,
        settings=settings,
        coordinates=coordinates,
        title=_require_string(payload, "title", event_name, ("payload",)),
        title_parameters=_title_parameters(payload, event_name),
        controller=_controller(payload, event_name, required=False),
        state=_optional_int(payload, "state", event_name, ("payload",)),
    )


def _parse_property_inspector_event(
    data: JsonObject,
    event_name: str,
    event_class: (type[PropertyInspectorDidAppearEvent] | type[PropertyInspectorDidDisappearEvent]),
) -> PropertyInspectorDidAppearEvent | PropertyInspectorDidDisappearEvent:
    action, context, device = _action_identity(data, event_name, require_device=True)
    if device is None:  # pragma: no cover - narrowed by require_device
        raise AssertionError("required device was not parsed")
    return event_class(action=action, context=context, device=device)


def _parse_property_inspector_did_appear(
    data: JsonObject,
    event_name: str,
) -> PropertyInspectorDidAppearEvent:
    return _parse_property_inspector_event(data, event_name, PropertyInspectorDidAppearEvent)


def _parse_property_inspector_did_disappear(
    data: JsonObject,
    event_name: str,
) -> PropertyInspectorDidDisappearEvent:
    return _parse_property_inspector_event(data, event_name, PropertyInspectorDidDisappearEvent)


def _parse_send_to_plugin(data: JsonObject, event_name: str) -> SendToPluginEvent:
    action, context, device = _action_identity(data, event_name, require_device=False)
    message = _clone_event_object(_require_object(data, "payload", event_name))
    return SendToPluginEvent(
        action=action,
        context=context,
        device=device,
        message=PropertyInspectorMessage(
            name=_optional_string(message, "event", event_name, ("payload",)),
            value=message,
        ),
    )


def _parse_did_receive_global_settings(
    data: JsonObject,
    event_name: str,
) -> DidReceiveGlobalSettingsEvent:
    payload = _require_object(data, "payload", event_name)
    return DidReceiveGlobalSettingsEvent(
        settings=_clone_event_object(_require_object(payload, "settings", event_name, ("payload",)))
    )


def _parse_device_did_connect(data: JsonObject, event_name: str) -> DeviceDidConnectEvent:
    info = _device_info(data, event_name, required=True)
    if info is None:  # pragma: no cover - narrowed by required
        raise AssertionError("required device info was not parsed")
    return DeviceDidConnectEvent(
        device=_require_string(data, "device", event_name),
        info=info,
    )


def _parse_device_did_disconnect(
    data: JsonObject,
    event_name: str,
) -> DeviceDidDisconnectEvent:
    return DeviceDidDisconnectEvent(
        device=_require_string(data, "device", event_name),
        info=_device_info(data, event_name, required=False),
    )


def _parse_application_event(
    data: JsonObject,
    event_name: str,
    event_class: type[ApplicationDidLaunchEvent] | type[ApplicationDidTerminateEvent],
) -> ApplicationDidLaunchEvent | ApplicationDidTerminateEvent:
    payload = _require_object(data, "payload", event_name)
    application = _require_string(payload, "application", event_name, ("payload",))
    return event_class(application=application)


def _parse_application_did_launch(
    data: JsonObject,
    event_name: str,
) -> ApplicationDidLaunchEvent:
    return _parse_application_event(data, event_name, ApplicationDidLaunchEvent)


def _parse_application_did_terminate(
    data: JsonObject,
    event_name: str,
) -> ApplicationDidTerminateEvent:
    return _parse_application_event(data, event_name, ApplicationDidTerminateEvent)


def _parse_system_did_wake_up(
    _data: JsonObject,
    _event_name: str,
) -> SystemDidWakeUpEvent:
    return SystemDidWakeUpEvent()


def _build_event_codec_registry(
    descriptors: tuple[EventCodecDescriptor, ...],
) -> Mapping[str, EventCodecDescriptor]:
    registry: dict[str, EventCodecDescriptor] = {}
    for descriptor in descriptors:
        if descriptor.wire_name in registry:
            raise RuntimeError(
                f"Duplicate Stream Dock event codec descriptor: {descriptor.wire_name}"
            )
        class_wire_name = str(getattr(descriptor.event_class, "event", ""))
        if class_wire_name != descriptor.wire_name:
            raise RuntimeError(
                f"Event codec descriptor {descriptor.wire_name!r} does not match "
                f"{descriptor.event_class.__name__}.event"
            )
        registry[descriptor.wire_name] = descriptor

    expected_wire_names = {event_type.value for event_type in StreamDockEventType}
    if registry.keys() != expected_wire_names:
        missing = sorted(expected_wire_names - registry.keys())
        extra = sorted(registry.keys() - expected_wire_names)
        raise RuntimeError(
            f"Stream Dock event codec registry does not match StreamDockEventType; "
            f"missing={missing}, extra={extra}"
        )
    return MappingProxyType(registry)


EVENT_CODEC_REGISTRY: Mapping[str, EventCodecDescriptor] = _build_event_codec_registry(
    (
        EventCodecDescriptor(
            wire_name=StreamDockEventType.WILL_APPEAR.value,
            event_class=WillAppearEvent,
            parser=_parse_will_appear,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.WILL_DISAPPEAR.value,
            event_class=WillDisappearEvent,
            parser=_parse_will_disappear,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.DID_RECEIVE_SETTINGS.value,
            event_class=DidReceiveSettingsEvent,
            parser=_parse_did_receive_settings,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.TITLE_PARAMETERS_DID_CHANGE.value,
            event_class=TitleParametersDidChangeEvent,
            parser=_parse_title_parameters_did_change,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.KEY_DOWN.value,
            event_class=KeyDownEvent,
            parser=_parse_key_down,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.KEY_UP.value,
            event_class=KeyUpEvent,
            parser=_parse_key_up,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.TOUCH_TAP.value,
            event_class=TouchTapEvent,
            parser=_parse_touch_tap,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.DIAL_DOWN.value,
            event_class=DialDownEvent,
            parser=_parse_dial_down,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.DIAL_UP.value,
            event_class=DialUpEvent,
            parser=_parse_dial_up,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.DIAL_ROTATE.value,
            event_class=DialRotateEvent,
            parser=_parse_dial_rotate,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.PROPERTY_INSPECTOR_DID_APPEAR.value,
            event_class=PropertyInspectorDidAppearEvent,
            parser=_parse_property_inspector_did_appear,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.PROPERTY_INSPECTOR_DID_DISAPPEAR.value,
            event_class=PropertyInspectorDidDisappearEvent,
            parser=_parse_property_inspector_did_disappear,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.SEND_TO_PLUGIN.value,
            event_class=SendToPluginEvent,
            parser=_parse_send_to_plugin,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.DID_RECEIVE_GLOBAL_SETTINGS.value,
            event_class=DidReceiveGlobalSettingsEvent,
            parser=_parse_did_receive_global_settings,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.DEVICE_DID_CONNECT.value,
            event_class=DeviceDidConnectEvent,
            parser=_parse_device_did_connect,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.DEVICE_DID_DISCONNECT.value,
            event_class=DeviceDidDisconnectEvent,
            parser=_parse_device_did_disconnect,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.APPLICATION_DID_LAUNCH.value,
            event_class=ApplicationDidLaunchEvent,
            parser=_parse_application_did_launch,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.APPLICATION_DID_TERMINATE.value,
            event_class=ApplicationDidTerminateEvent,
            parser=_parse_application_did_terminate,
        ),
        EventCodecDescriptor(
            wire_name=StreamDockEventType.SYSTEM_DID_WAKE_UP.value,
            event_class=SystemDidWakeUpEvent,
            parser=_parse_system_did_wake_up,
        ),
    )
)


def __getattr__(name: str) -> object:
    """Lazily expose the combined registry required by the legacy runtime."""

    if name == "EVENT_REGISTRY":
        from ._next.runtime._legacy import build_legacy_event_registry

        registry = build_legacy_event_registry(EVENT_CODEC_REGISTRY)
        globals()[name] = registry
        return registry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def parse_stream_dock_event(
    value: object,
    *,
    allow_unknown: bool = True,
) -> StreamDockEvent:
    """Validate one decoded message and return a typed event.

    Unknown but structurally valid event envelopes are preserved by default as
    :class:`UnknownStreamDockEvent`. Set ``allow_unknown=False`` to raise
    :class:`UnsupportedEventError` instead. Malformed known events always raise
    :class:`MalformedEventError` or its :class:`InvalidFieldError` subtype.

    Args:
        value: A value already decoded from a WebSocket JSON frame.
        allow_unknown: Preserve unrecognized event names and their complete
            envelopes when ``True``; reject them when ``False``.

    Returns:
        A frozen dataclass representing the recognized event, or an
        :class:`UnknownStreamDockEvent` for a valid unrecognized envelope.
        Mutable JSON objects are copied before being stored in the result.

    Raises:
        MalformedEventError: If the root is not a JSON object or lacks a valid
            event name.
        InvalidFieldError: If a recognized event has a missing, invalid, or
            unsupported field. The error identifies the event and JSON path.
        UnsupportedEventError: If the event is unknown and ``allow_unknown`` is
            ``False``.
    """

    if not isinstance(value, dict):
        if not is_json_value(value):
            raise MalformedEventError("message contains a non-JSON value")
        raise MalformedEventError("expected event object")
    data = value
    raw_event = _require_string(data, "event", None)
    descriptor = EVENT_CODEC_REGISTRY.get(raw_event)
    if descriptor is None:
        if not allow_unknown:
            if not is_json_value(data):
                raise MalformedEventError("message contains a non-JSON value") from None
            raise UnsupportedEventError(raw_event) from None
        return UnknownStreamDockEvent(event=raw_event, data=_clone_event_object(data))

    event = descriptor.parser(data, raw_event)
    if not isinstance(event, descriptor.event_class):
        raise AssertionError(
            f"Parser for {raw_event!r} returned {type(event).__name__}, "
            f"expected {descriptor.event_class.__name__}"
        )
    return event

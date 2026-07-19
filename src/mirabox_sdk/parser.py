"""Strict conversion from decoded JSON values to typed MiraBox events."""

from __future__ import annotations

from copy import deepcopy
from typing import NoReturn

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
from .json_types import JsonObject, is_json_value


def _invalid(
    event_name: str | None,
    path: tuple[str | int, ...],
    reason: str,
) -> NoReturn:
    raise InvalidFieldError(reason, event_name=event_name, path=path)


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
    settings = _require_object(payload, "settings", event_name, ("payload",))
    return action, context, device, settings, _coordinates(payload, event_name), payload


def _parse_action_payload_event(
    data: JsonObject,
    event_type: StreamDockEventType,
) -> StreamDockEvent:
    event_name = event_type.value
    action, context, device, settings, coordinates, payload = _action_payload(data, event_name)

    if event_type in {StreamDockEventType.WILL_APPEAR, StreamDockEventType.WILL_DISAPPEAR}:
        values = {
            "action": action,
            "context": context,
            "device": device,
            "settings": settings,
            "coordinates": coordinates,
            "controller": _controller(payload, event_name, required=True),
            "is_in_multi_action": _require_bool(
                payload,
                "isInMultiAction",
                event_name,
                ("payload",),
            ),
            "state": _optional_int(payload, "state", event_name, ("payload",)),
        }
        if event_type is StreamDockEventType.WILL_APPEAR:
            return WillAppearEvent(**values)  # type: ignore[arg-type]
        return WillDisappearEvent(**values)  # type: ignore[arg-type]

    if event_type is StreamDockEventType.DID_RECEIVE_SETTINGS:
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

    if event_type in {
        StreamDockEventType.KEY_DOWN,
        StreamDockEventType.KEY_UP,
        StreamDockEventType.TOUCH_TAP,
    }:
        values = {
            "action": action,
            "context": context,
            "device": device,
            "settings": settings,
            "coordinates": coordinates,
            "is_in_multi_action": _require_bool(
                payload,
                "isInMultiAction",
                event_name,
                ("payload",),
            ),
            "controller": _controller(payload, event_name, required=False),
            "state": _optional_int(payload, "state", event_name, ("payload",)),
            "user_desired_state": _optional_int(
                payload,
                "userDesiredState",
                event_name,
                ("payload",),
            ),
        }
        if event_type is StreamDockEventType.KEY_DOWN:
            return KeyDownEvent(**values)  # type: ignore[arg-type]
        if event_type is StreamDockEventType.KEY_UP:
            return KeyUpEvent(**values)  # type: ignore[arg-type]
        return TouchTapEvent(**values)  # type: ignore[arg-type]

    if event_type in {StreamDockEventType.DIAL_DOWN, StreamDockEventType.DIAL_UP}:
        values = {
            "action": action,
            "context": context,
            "device": device,
            "settings": settings,
            "coordinates": coordinates,
            "controller": _controller(payload, event_name, required=True),
        }
        if event_type is StreamDockEventType.DIAL_DOWN:
            return DialDownEvent(**values)  # type: ignore[arg-type]
        return DialUpEvent(**values)  # type: ignore[arg-type]

    if event_type is StreamDockEventType.DIAL_ROTATE:
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

    if event_type is StreamDockEventType.TITLE_PARAMETERS_DID_CHANGE:
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

    raise AssertionError(f"Unhandled action payload event: {event_type}")


_ACTION_PAYLOAD_EVENTS = {
    StreamDockEventType.WILL_APPEAR,
    StreamDockEventType.WILL_DISAPPEAR,
    StreamDockEventType.DID_RECEIVE_SETTINGS,
    StreamDockEventType.KEY_DOWN,
    StreamDockEventType.KEY_UP,
    StreamDockEventType.TOUCH_TAP,
    StreamDockEventType.DIAL_DOWN,
    StreamDockEventType.DIAL_UP,
    StreamDockEventType.DIAL_ROTATE,
    StreamDockEventType.TITLE_PARAMETERS_DID_CHANGE,
}


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

    if not is_json_value(value):
        raise MalformedEventError("message contains a non-JSON value")
    if not isinstance(value, dict):
        raise MalformedEventError("expected event object")
    data = deepcopy(value)
    raw_event = _require_string(data, "event", None)
    try:
        event_type = StreamDockEventType(raw_event)
    except ValueError:
        if not allow_unknown:
            raise UnsupportedEventError(raw_event) from None
        return UnknownStreamDockEvent(event=raw_event, data=dict(data))

    if event_type in _ACTION_PAYLOAD_EVENTS:
        return _parse_action_payload_event(data, event_type)

    if event_type in {
        StreamDockEventType.PROPERTY_INSPECTOR_DID_APPEAR,
        StreamDockEventType.PROPERTY_INSPECTOR_DID_DISAPPEAR,
    }:
        action, context, device = _action_identity(data, raw_event, require_device=True)
        if device is None:  # pragma: no cover - narrowed by require_device
            raise AssertionError("required device was not parsed")
        if event_type is StreamDockEventType.PROPERTY_INSPECTOR_DID_APPEAR:
            return PropertyInspectorDidAppearEvent(
                action=action,
                context=context,
                device=device,
            )
        return PropertyInspectorDidDisappearEvent(
            action=action,
            context=context,
            device=device,
        )

    if event_type is StreamDockEventType.SEND_TO_PLUGIN:
        action, context, device = _action_identity(data, raw_event, require_device=False)
        message = _require_object(data, "payload", raw_event)
        return SendToPluginEvent(
            action=action,
            context=context,
            device=device,
            message=PropertyInspectorMessage(
                name=_optional_string(message, "event", raw_event, ("payload",)),
                value=message,
            ),
        )

    if event_type is StreamDockEventType.DID_RECEIVE_GLOBAL_SETTINGS:
        payload = _require_object(data, "payload", raw_event)
        return DidReceiveGlobalSettingsEvent(
            settings=_require_object(payload, "settings", raw_event, ("payload",))
        )

    if event_type is StreamDockEventType.DEVICE_DID_CONNECT:
        info = _device_info(data, raw_event, required=True)
        if info is None:  # pragma: no cover - narrowed by required
            raise AssertionError("required device info was not parsed")
        return DeviceDidConnectEvent(
            device=_require_string(data, "device", raw_event),
            info=info,
        )

    if event_type is StreamDockEventType.DEVICE_DID_DISCONNECT:
        return DeviceDidDisconnectEvent(
            device=_require_string(data, "device", raw_event),
            info=_device_info(data, raw_event, required=False),
        )

    if event_type in {
        StreamDockEventType.APPLICATION_DID_LAUNCH,
        StreamDockEventType.APPLICATION_DID_TERMINATE,
    }:
        payload = _require_object(data, "payload", raw_event)
        application = _require_string(payload, "application", raw_event, ("payload",))
        if event_type is StreamDockEventType.APPLICATION_DID_LAUNCH:
            return ApplicationDidLaunchEvent(application=application)
        return ApplicationDidTerminateEvent(application=application)

    if event_type is StreamDockEventType.SYSTEM_DID_WAKE_UP:
        return SystemDidWakeUpEvent()

    raise AssertionError(f"Unhandled Stream Dock event: {event_type}")

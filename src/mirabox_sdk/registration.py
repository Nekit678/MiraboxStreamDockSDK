"""Typed executable launch arguments supplied by MiraBox Stream Dock."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from .errors import InvalidPluginLaunchArgumentsError, InvalidRegistrationInfoError
from .events import DeviceSize
from .json_types import JsonObject, JsonValue, is_json_value


@dataclass(frozen=True, slots=True)
class RegistrationApplicationInfo:
    language: str
    platform: str
    platform_version: str
    version: str
    font: str | None = None


@dataclass(frozen=True, slots=True)
class RegistrationColors:
    button_mouse_over_background_color: str | None = None
    button_pressed_background_color: str | None = None
    button_pressed_border_color: str | None = None
    button_pressed_text_color: str | None = None
    highlight_color: str | None = None


@dataclass(frozen=True, slots=True)
class RegistrationDeviceInfo:
    id: str
    name: str
    type: int
    size: DeviceSize


@dataclass(frozen=True, slots=True)
class RegistrationPluginInfo:
    uuid: str
    version: str


@dataclass(frozen=True, slots=True)
class RegistrationInfo:
    application: RegistrationApplicationInfo
    colors: RegistrationColors
    device_pixel_ratio: float
    devices: tuple[RegistrationDeviceInfo, ...]
    plugin: RegistrationPluginInfo


@dataclass(frozen=True, slots=True)
class PluginLaunchArguments:
    port: int
    plugin_uuid: str
    register_event: str
    info: RegistrationInfo


def _invalid_info(path: tuple[str | int, ...], reason: str) -> NoReturn:
    raise InvalidRegistrationInfoError(reason, path=path)


def _require_object(
    data: JsonObject,
    key: str,
    path: tuple[str | int, ...] = (),
) -> JsonObject:
    field_path = (*path, key)
    if key not in data:
        _invalid_info(field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, dict):
        _invalid_info(field_path, "expected object")
    return value


def _require_array(
    data: JsonObject,
    key: str,
    path: tuple[str | int, ...] = (),
) -> list[JsonValue]:
    field_path = (*path, key)
    if key not in data:
        _invalid_info(field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, list):
        _invalid_info(field_path, "expected array")
    return value


def _require_string(
    data: JsonObject,
    key: str,
    path: tuple[str | int, ...] = (),
    *,
    nonempty: bool = False,
) -> str:
    field_path = (*path, key)
    if key not in data:
        _invalid_info(field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, str):
        _invalid_info(field_path, "expected string")
    if nonempty and not value.strip():
        _invalid_info(field_path, "expected non-empty string")
    return value


def _optional_string(
    data: JsonObject,
    key: str,
    path: tuple[str | int, ...] = (),
) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str):
        _invalid_info((*path, key), "expected string")
    return value


def _require_int(
    data: JsonObject,
    key: str,
    path: tuple[str | int, ...] = (),
) -> int:
    field_path = (*path, key)
    if key not in data:
        _invalid_info(field_path, "required field is missing")
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        _invalid_info(field_path, "expected integer")
    return value


def _require_positive_number(data: JsonObject, key: str) -> float:
    if key not in data:
        _invalid_info((key,), "required field is missing")
    value = data[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _invalid_info((key,), "expected number")
    number = float(value)
    if number <= 0:
        _invalid_info((key,), "expected positive number")
    return number


def _parse_application(data: JsonObject) -> RegistrationApplicationInfo:
    path = ("application",)
    return RegistrationApplicationInfo(
        language=_require_string(data, "language", path),
        platform=_require_string(data, "platform", path),
        platform_version=_require_string(data, "platformVersion", path),
        version=_require_string(data, "version", path),
        font=_optional_string(data, "font", path),
    )


def _parse_colors(data: JsonObject) -> RegistrationColors:
    path = ("colors",)
    return RegistrationColors(
        button_mouse_over_background_color=_optional_string(
            data, "buttonMouseOverBackgroundColor", path
        ),
        button_pressed_background_color=_optional_string(
            data, "buttonPressedBackgroundColor", path
        ),
        button_pressed_border_color=_optional_string(data, "buttonPressedBorderColor", path),
        button_pressed_text_color=_optional_string(data, "buttonPressedTextColor", path),
        highlight_color=_optional_string(data, "highlightColor", path),
    )


def _parse_devices(values: list[JsonValue]) -> tuple[RegistrationDeviceInfo, ...]:
    devices: list[RegistrationDeviceInfo] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(values):
        path = ("devices", index)
        if not isinstance(value, dict):
            _invalid_info(path, "expected object")
        size = _require_object(value, "size", path)
        columns = _require_int(size, "columns", (*path, "size"))
        rows = _require_int(size, "rows", (*path, "size"))
        if columns <= 0:
            _invalid_info((*path, "size", "columns"), "expected positive integer")
        if rows <= 0:
            _invalid_info((*path, "size", "rows"), "expected positive integer")
        device_id = _require_string(value, "id", path, nonempty=True)
        if device_id in seen_ids:
            _invalid_info((*path, "id"), "duplicate device identifier")
        seen_ids.add(device_id)
        devices.append(
            RegistrationDeviceInfo(
                id=device_id,
                name=_require_string(value, "name", path),
                type=_require_int(value, "type", path),
                size=DeviceSize(columns=columns, rows=rows),
            )
        )
    return tuple(devices)


def _parse_plugin(data: JsonObject) -> RegistrationPluginInfo:
    path = ("plugin",)
    return RegistrationPluginInfo(
        uuid=_require_string(data, "uuid", path, nonempty=True),
        version=_require_string(data, "version", path),
    )


def parse_registration_info(value: object) -> RegistrationInfo:
    """Validate and convert the decoded ``-info`` JSON argument."""

    if not is_json_value(value):
        raise InvalidRegistrationInfoError("value contains non-JSON data")
    if not isinstance(value, dict):
        raise InvalidRegistrationInfoError("expected registration info object")
    return RegistrationInfo(
        application=_parse_application(_require_object(value, "application")),
        colors=_parse_colors(_require_object(value, "colors")),
        device_pixel_ratio=_require_positive_number(value, "devicePixelRatio"),
        devices=_parse_devices(_require_array(value, "devices")),
        plugin=_parse_plugin(_require_object(value, "plugin")),
    )


def parse_plugin_launch_arguments(
    *,
    port: object,
    plugin_uuid: object,
    register_event: object,
    info: object,
) -> PluginLaunchArguments:
    """Validate all arguments with which Stream Dock launches a plugin executable."""

    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise InvalidPluginLaunchArgumentsError(
            "expected integer from 1 to 65535",
            path=("port",),
        )
    if not isinstance(plugin_uuid, str) or not plugin_uuid.strip():
        raise InvalidPluginLaunchArgumentsError(
            "expected non-empty string",
            path=("pluginUUID",),
        )
    if not isinstance(register_event, str) or not register_event.strip():
        raise InvalidPluginLaunchArgumentsError(
            "expected non-empty string",
            path=("registerEvent",),
        )
    registration_info = parse_registration_info(info)
    return PluginLaunchArguments(
        port=port,
        plugin_uuid=plugin_uuid,
        register_event=register_event,
        info=registration_info,
    )

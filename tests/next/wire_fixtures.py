"""Shared wire fixtures for the experimental boundary codec tests."""

from __future__ import annotations

from mirabox_sdk.commands import (
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
)
from mirabox_sdk.json_types import JsonObject, JsonValue


def known_event_envelopes() -> dict[str, JsonObject]:
    """Return fresh wire envelopes for every event in ``EVENT_REGISTRY``."""

    identity: JsonObject = {
        "action": "action-uuid",
        "context": "button",
        "device": "device-uuid",
    }

    def action_payload_event(event: str, **fields: JsonValue) -> JsonObject:
        payload: JsonObject = {
            "settings": {},
            "coordinates": {"column": 0, "row": 0},
            **fields,
        }
        return {
            "event": event,
            **identity,
            "payload": payload,
        }

    visibility: dict[str, JsonValue] = {
        "controller": "Keypad",
        "isInMultiAction": False,
    }
    key: dict[str, JsonValue] = {"isInMultiAction": False}
    title_parameters: JsonObject = {
        "fontFamily": "Arial",
        "fontSize": 12,
        "fontStyle": "Regular",
        "fontUnderline": False,
        "showTitle": True,
        "titleAlignment": "middle",
        "titleColor": "#ffffffff",
    }
    return {
        "willAppear": action_payload_event("willAppear", **visibility),
        "willDisappear": action_payload_event("willDisappear", **visibility),
        "didReceiveSettings": action_payload_event(
            "didReceiveSettings",
            isInMultiAction=False,
        ),
        "titleParametersDidChange": action_payload_event(
            "titleParametersDidChange",
            title="Channel",
            titleParameters=title_parameters,
        ),
        "keyDown": action_payload_event("keyDown", **key),
        "keyUp": action_payload_event("keyUp", **key),
        "touchTap": action_payload_event("touchTap", **key),
        "dialDown": action_payload_event("dialDown", controller="Encoder"),
        "dialUp": action_payload_event("dialUp", controller="Encoder"),
        "dialRotate": action_payload_event(
            "dialRotate",
            ticks=1,
            pressed=False,
        ),
        "propertyInspectorDidAppear": {
            "event": "propertyInspectorDidAppear",
            **identity,
        },
        "propertyInspectorDidDisappear": {
            "event": "propertyInspectorDidDisappear",
            **identity,
        },
        "sendToPlugin": {
            "event": "sendToPlugin",
            "action": "action-uuid",
            "context": "button",
            "payload": {"event": "refresh"},
        },
        "didReceiveGlobalSettings": {
            "event": "didReceiveGlobalSettings",
            "payload": {"settings": {}},
        },
        "deviceDidConnect": {
            "event": "deviceDidConnect",
            "device": "device-uuid",
            "deviceInfo": {
                "name": "Stream Dock",
                "type": 1,
                "size": {"columns": 5, "rows": 3},
            },
        },
        "deviceDidDisconnect": {
            "event": "deviceDidDisconnect",
            "device": "device-uuid",
        },
        "applicationDidLaunch": {
            "event": "applicationDidLaunch",
            "payload": {"application": "com.example.app"},
        },
        "applicationDidTerminate": {
            "event": "applicationDidTerminate",
            "payload": {"application": "com.example.app"},
        },
        "systemDidWakeUp": {"event": "systemDidWakeUp"},
    }


def known_command_wire_fixtures() -> tuple[tuple[StreamDockCommand, JsonObject], ...]:
    """Return typed commands paired with their exact legacy wire envelopes."""

    return (
        (
            RegisterPluginCommand("registerPlugin", "plugin-uuid"),
            {"event": "registerPlugin", "uuid": "plugin-uuid"},
        ),
        (
            SendToPropertyInspectorCommand(
                "action-uuid",
                "button",
                {"message": {"name": "refresh"}},
            ),
            {
                "event": "sendToPropertyInspector",
                "action": "action-uuid",
                "context": "button",
                "payload": {"message": {"name": "refresh"}},
            },
        ),
        (
            SetStateCommand("button", 2),
            {"event": "setState", "context": "button", "payload": {"state": 2}},
        ),
        (
            SetTitleCommand("button", "Microphone", target=1, state=2),
            {
                "event": "setTitle",
                "context": "button",
                "payload": {"title": "Microphone", "target": 1, "state": 2},
            },
        ),
        (
            SetSettingsCommand("button", {"profile": {"level": 2}}),
            {
                "event": "setSettings",
                "context": "button",
                "payload": {"profile": {"level": 2}},
            },
        ),
        (
            GetSettingsCommand("button"),
            {"event": "getSettings", "context": "button"},
        ),
        (
            SetImageCommand(
                "button",
                "data:image/png;base64,abc",
                target=1,
                state=2,
            ),
            {
                "event": "setImage",
                "context": "button",
                "payload": {
                    "image": "data:image/png;base64,abc",
                    "target": 1,
                    "state": 2,
                },
            },
        ),
        (
            ShowOkCommand("button"),
            {"event": "showOk", "context": "button"},
        ),
        (
            ShowAlertCommand("button"),
            {"event": "showAlert", "context": "button"},
        ),
        (
            OpenUrlCommand("https://example.com/settings"),
            {
                "event": "openUrl",
                "payload": {"url": "https://example.com/settings"},
            },
        ),
        (
            LogMessageCommand("Channel updated"),
            {
                "event": "logMessage",
                "payload": {"message": "Channel updated"},
            },
        ),
        (
            SetGlobalSettingsCommand("plugin-uuid", {"profile": {"level": 2}}),
            {
                "event": "setGlobalSettings",
                "context": "plugin-uuid",
                "payload": {"profile": {"level": 2}},
            },
        ),
        (
            GetGlobalSettingsCommand("plugin-uuid"),
            {"event": "getGlobalSettings", "context": "plugin-uuid"},
        ),
    )

"""Typed commands sent to MiraBox Stream Dock."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TypeVar

from .codecs import JsonCodec, encode_with_codec
from .json_types import JsonObject

PayloadT = TypeVar("PayloadT")


class StreamDockCommand(ABC):
    @abstractmethod
    def to_wire(self) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class RegisterPluginCommand(StreamDockCommand):
    event: str
    uuid: str

    def to_wire(self) -> JsonObject:
        return {"event": self.event, "uuid": self.uuid}


@dataclass(frozen=True, slots=True)
class SendToPropertyInspectorCommand(StreamDockCommand):
    action: str
    context: str
    payload: JsonObject

    @classmethod
    def from_payload(
        cls,
        action: str,
        context: str,
        payload: PayloadT,
        codec: JsonCodec[PayloadT],
    ) -> SendToPropertyInspectorCommand:
        return cls(action=action, context=context, payload=encode_with_codec(payload, codec))

    def to_wire(self) -> JsonObject:
        return {
            "event": "sendToPropertyInspector",
            "action": self.action,
            "context": self.context,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class SetStateCommand(StreamDockCommand):
    context: str
    state: int

    def to_wire(self) -> JsonObject:
        return {"event": "setState", "context": self.context, "payload": {"state": self.state}}


@dataclass(frozen=True, slots=True)
class SetTitleCommand(StreamDockCommand):
    context: str
    title: str
    target: int = 0
    state: int | None = None

    def to_wire(self) -> JsonObject:
        payload: JsonObject = {"title": self.title, "target": self.target}
        if self.state is not None:
            payload["state"] = self.state
        return {
            "event": "setTitle",
            "context": self.context,
            "payload": payload,
        }


@dataclass(frozen=True, slots=True)
class SetSettingsCommand(StreamDockCommand):
    context: str
    settings: JsonObject

    @classmethod
    def from_settings(
        cls,
        context: str,
        settings: PayloadT,
        codec: JsonCodec[PayloadT],
    ) -> SetSettingsCommand:
        return cls(context=context, settings=encode_with_codec(settings, codec))

    def to_wire(self) -> JsonObject:
        return {"event": "setSettings", "context": self.context, "payload": self.settings}


@dataclass(frozen=True, slots=True)
class GetSettingsCommand(StreamDockCommand):
    context: str

    def to_wire(self) -> JsonObject:
        return {"event": "getSettings", "context": self.context}


@dataclass(frozen=True, slots=True)
class SetImageCommand(StreamDockCommand):
    context: str
    image: str
    target: int = 0
    state: int | None = None

    def to_wire(self) -> JsonObject:
        payload: JsonObject = {"image": self.image, "target": self.target}
        if self.state is not None:
            payload["state"] = self.state
        return {
            "event": "setImage",
            "context": self.context,
            "payload": payload,
        }


@dataclass(frozen=True, slots=True)
class ShowOkCommand(StreamDockCommand):
    context: str

    def to_wire(self) -> JsonObject:
        return {"event": "showOk", "context": self.context}


@dataclass(frozen=True, slots=True)
class ShowAlertCommand(StreamDockCommand):
    context: str

    def to_wire(self) -> JsonObject:
        return {"event": "showAlert", "context": self.context}


@dataclass(frozen=True, slots=True)
class OpenUrlCommand(StreamDockCommand):
    url: str

    def to_wire(self) -> JsonObject:
        return {"event": "openUrl", "payload": {"url": self.url}}


@dataclass(frozen=True, slots=True)
class LogMessageCommand(StreamDockCommand):
    message: str

    def to_wire(self) -> JsonObject:
        return {"event": "logMessage", "payload": {"message": self.message}}


@dataclass(frozen=True, slots=True)
class SetGlobalSettingsCommand(StreamDockCommand):
    context: str
    settings: JsonObject

    @classmethod
    def from_settings(
        cls,
        context: str,
        settings: PayloadT,
        codec: JsonCodec[PayloadT],
    ) -> SetGlobalSettingsCommand:
        return cls(context=context, settings=encode_with_codec(settings, codec))

    def to_wire(self) -> JsonObject:
        return {
            "event": "setGlobalSettings",
            "context": self.context,
            "payload": self.settings,
        }


@dataclass(frozen=True, slots=True)
class GetGlobalSettingsCommand(StreamDockCommand):
    context: str

    def to_wire(self) -> JsonObject:
        return {"event": "getGlobalSettings", "context": self.context}

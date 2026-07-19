"""Typed commands sent to MiraBox Stream Dock."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TypeVar

from .codecs import JsonCodec, encode_with_codec
from .json_types import JsonObject

PayloadT = TypeVar("PayloadT")


class StreamDockCommand(ABC):
    """Abstract outbound message accepted by a Stream Dock connection.

    Concrete commands are immutable dataclasses. They intentionally contain
    protocol values without performing transport I/O, which makes them easy to
    construct, inspect, and test before passing them to
    :meth:`StreamDockSender.send`.
    """

    @abstractmethod
    def to_wire(self) -> JsonObject:
        """Serialize the command to its exact JSON-compatible wire envelope."""

        ...


@dataclass(frozen=True, slots=True)
class RegisterPluginCommand(StreamDockCommand):
    """Register the plugin immediately after the WebSocket opens.

    Attributes:
        event: Runtime-provided registration event name.
        uuid: Runtime-provided plugin UUID.
    """

    event: str
    uuid: str

    def to_wire(self) -> JsonObject:
        """Return the registration event and plugin UUID envelope."""

        return {"event": self.event, "uuid": self.uuid}


@dataclass(frozen=True, slots=True)
class SendToPropertyInspectorCommand(StreamDockCommand):
    """Deliver a plugin message to one action's Property Inspector.

    Attributes:
        action: Manifest UUID of the action type.
        context: Opaque identifier of the target action instance.
        payload: Plugin-defined JSON message body.
    """

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
        """Build a command by encoding a plugin-owned payload.

        Args:
            action: Manifest UUID of the action type.
            context: Opaque identifier of the target action instance.
            payload: Typed payload understood by the supplied codec.
            codec: Codec used to produce the wire JSON object.

        Returns:
            A command containing the validated, isolated encoded payload.

        Raises:
            JsonCodecEncodeError: If encoding fails or produces invalid JSON.
        """

        return cls(action=action, context=context, payload=encode_with_codec(payload, codec))

    def to_wire(self) -> JsonObject:
        """Return a ``sendToPropertyInspector`` wire envelope."""

        return {
            "event": "sendToPropertyInspector",
            "action": self.action,
            "context": self.context,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class SetStateCommand(StreamDockCommand):
    """Select the displayed state of an action instance.

    Attributes:
        context: Opaque identifier of the target action instance.
        state: Zero-based state index declared in the manifest.
    """

    context: str
    state: int

    def to_wire(self) -> JsonObject:
        """Return a ``setState`` wire envelope."""

        return {"event": "setState", "context": self.context, "payload": {"state": self.state}}


@dataclass(frozen=True, slots=True)
class SetTitleCommand(StreamDockCommand):
    """Change the title rendered for an action instance.

    Attributes:
        context: Opaque identifier of the target action instance.
        title: New title text; an empty string clears the title.
        target: Protocol target selector passed through to Stream Dock.
        state: Optional zero-based state to update. ``None`` omits the field.
    """

    context: str
    title: str
    target: int = 0
    state: int | None = None

    def to_wire(self) -> JsonObject:
        """Return a ``setTitle`` envelope, omitting an unspecified state."""

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
    """Persist settings belonging to one action context.

    Attributes:
        context: Opaque identifier of the target action instance.
        settings: JSON object to persist for the action.
    """

    context: str
    settings: JsonObject

    @classmethod
    def from_settings(
        cls,
        context: str,
        settings: PayloadT,
        codec: JsonCodec[PayloadT],
    ) -> SetSettingsCommand:
        """Build a settings command from a plugin-owned value.

        Args:
            context: Opaque identifier of the target action instance.
            settings: Typed settings value understood by ``codec``.
            codec: Codec used to produce the wire JSON object.

        Returns:
            A command containing validated, isolated settings.

        Raises:
            JsonCodecEncodeError: If encoding fails or produces invalid JSON.
        """

        return cls(context=context, settings=encode_with_codec(settings, codec))

    def to_wire(self) -> JsonObject:
        """Return a ``setSettings`` wire envelope."""

        return {"event": "setSettings", "context": self.context, "payload": self.settings}


@dataclass(frozen=True, slots=True)
class GetSettingsCommand(StreamDockCommand):
    """Request the persisted settings for one action context.

    Attributes:
        context: Opaque identifier of the target action instance.
    """

    context: str

    def to_wire(self) -> JsonObject:
        """Return a ``getSettings`` wire envelope."""

        return {"event": "getSettings", "context": self.context}


@dataclass(frozen=True, slots=True)
class SetImageCommand(StreamDockCommand):
    """Change the image rendered for an action instance.

    Attributes:
        context: Opaque identifier of the target action instance.
        image: Image representation accepted by Stream Dock, commonly a data
            URL. An empty string requests the manifest's default image.
        target: Protocol target selector passed through to Stream Dock.
        state: Optional zero-based state to update. ``None`` omits the field.
    """

    context: str
    image: str
    target: int = 0
    state: int | None = None

    def to_wire(self) -> JsonObject:
        """Return a ``setImage`` envelope, omitting an unspecified state."""

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
    """Show Stream Dock's temporary success indicator on an action.

    Attributes:
        context: Opaque identifier of the target action instance.
    """

    context: str

    def to_wire(self) -> JsonObject:
        """Return a ``showOk`` wire envelope."""

        return {"event": "showOk", "context": self.context}


@dataclass(frozen=True, slots=True)
class ShowAlertCommand(StreamDockCommand):
    """Show Stream Dock's temporary failure indicator on an action.

    Attributes:
        context: Opaque identifier of the target action instance.
    """

    context: str

    def to_wire(self) -> JsonObject:
        """Return a ``showAlert`` wire envelope."""

        return {"event": "showAlert", "context": self.context}


@dataclass(frozen=True, slots=True)
class OpenUrlCommand(StreamDockCommand):
    """Ask the host operating system to open a URL.

    Attributes:
        url: Absolute URL understood by the host operating system.
    """

    url: str

    def to_wire(self) -> JsonObject:
        """Return an ``openUrl`` wire envelope."""

        return {"event": "openUrl", "payload": {"url": self.url}}


@dataclass(frozen=True, slots=True)
class LogMessageCommand(StreamDockCommand):
    """Write text through Stream Dock's plugin log facility.

    Attributes:
        message: Human-readable diagnostic message.
    """

    message: str

    def to_wire(self) -> JsonObject:
        """Return a ``logMessage`` wire envelope."""

        return {"event": "logMessage", "payload": {"message": self.message}}


@dataclass(frozen=True, slots=True)
class SetGlobalSettingsCommand(StreamDockCommand):
    """Persist settings shared by every action in a plugin.

    Attributes:
        context: Plugin UUID used as the command context.
        settings: JSON object to persist globally for the plugin.
    """

    context: str
    settings: JsonObject

    @classmethod
    def from_settings(
        cls,
        context: str,
        settings: PayloadT,
        codec: JsonCodec[PayloadT],
    ) -> SetGlobalSettingsCommand:
        """Build a global-settings command from a plugin-owned value.

        Args:
            context: Plugin UUID used as the command context.
            settings: Typed global settings value understood by ``codec``.
            codec: Codec used to produce the wire JSON object.

        Returns:
            A command containing validated, isolated settings.

        Raises:
            JsonCodecEncodeError: If encoding fails or produces invalid JSON.
        """

        return cls(context=context, settings=encode_with_codec(settings, codec))

    def to_wire(self) -> JsonObject:
        """Return a ``setGlobalSettings`` wire envelope."""

        return {
            "event": "setGlobalSettings",
            "context": self.context,
            "payload": self.settings,
        }


@dataclass(frozen=True, slots=True)
class GetGlobalSettingsCommand(StreamDockCommand):
    """Request settings shared by every action in a plugin.

    Attributes:
        context: Plugin UUID used as the command context.
    """

    context: str

    def to_wire(self) -> JsonObject:
        """Return a ``getGlobalSettings`` wire envelope."""

        return {"event": "getGlobalSettings", "context": self.context}

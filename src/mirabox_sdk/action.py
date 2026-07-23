"""Reusable base class for one action instance on a Stream Dock device."""

from __future__ import annotations

from typing import Any, ClassVar, Generic, TypeVar, cast

from .codecs import JSON_OBJECT_CODEC, JsonCodec, decode_with_codec
from .commands import (
    GetSettingsCommand,
    LogMessageCommand,
    OpenUrlCommand,
    SendToPropertyInspectorCommand,
    SetImageCommand,
    SetSettingsCommand,
    SetStateCommand,
    SetTitleCommand,
    ShowAlertCommand,
    ShowOkCommand,
    StreamDockCommand,
)
from .events import (
    ApplicationDidLaunchEvent,
    ApplicationDidTerminateEvent,
    DeviceDidConnectEvent,
    DeviceDidDisconnectEvent,
    DialDownEvent,
    DialRotateEvent,
    DialUpEvent,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    KeyDownEvent,
    KeyUpEvent,
    PropertyInspectorDidAppearEvent,
    PropertyInspectorDidDisappearEvent,
    SendToPluginEvent,
    SystemDidWakeUpEvent,
    TitleParameters,
    TitleParametersDidChangeEvent,
    TouchTapEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from .json_types import JsonObject
from .protocols import StreamDockActionDependencies

SettingsT = TypeVar("SettingsT")
PayloadT = TypeVar("PayloadT")
DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)


class Action(Generic[SettingsT, DependenciesT]):
    """Base class for one action instance placed on a Stream Dock device.

    Subclass this type once for every action UUID declared in the plugin
    manifest and register the subclass with :class:`ActionRegistry`. The
    runtime creates a separate instance for every visible Stream Dock context,
    so mutable state stored on ``self`` belongs to one key, dial, or display.

    ``SettingsT`` is the plugin-owned settings type. It defaults in practice to
    :class:`JsonObject`; subclasses can assign a custom :attr:`settings_codec`
    to use dataclasses or other typed values. ``DependenciesT`` is an
    application-defined dependency container that exposes ``stream_dock``.

    Attributes:
        action: Exact action UUID from the plugin manifest.
        context: Opaque identifier for this action instance. Commands that
            modify an action are automatically scoped to this context.
        settings: Latest locally known, decoded settings for the instance.
        title: Latest title reported by ``titleParametersDidChange``. It is an
            empty string until that event is received.
        title_parameters: Latest title formatting information, or ``None``
            before Stream Dock reports it.
        dependencies: Application-owned services available to callbacks.
        settings_codec: Class-level codec used to decode and encode settings.
            The default codec validates and copies a :class:`JsonObject`.
    """

    settings_codec: ClassVar[JsonCodec[Any]] = JSON_OBJECT_CODEC

    def __init__(
        self,
        action: str,
        context: str,
        settings: SettingsT,
        dependencies: DependenciesT,
    ) -> None:
        """Initialize an action context.

        Applications normally do not instantiate actions directly;
        :class:`ActionRegistry` supplies these values after ``willAppear``.

        Args:
            action: Manifest UUID identifying the registered action class.
            context: Stream Dock context identifying this concrete instance.
            settings: Settings decoded with :attr:`settings_codec`.
            dependencies: Plugin-owned dependency container shared with action
                instances.
        """

        self.action = action
        self.context = context
        self.settings = settings
        self.title = ""
        self.title_parameters: TitleParameters | None = None
        self.dependencies = dependencies
        self._stream_dock = dependencies.stream_dock

    def _send(self, command: StreamDockCommand) -> None:
        self._stream_dock.send(command)

    def send_to_property_inspector(self, payload: JsonObject) -> None:
        """Send a JSON object to this action's Property Inspector.

        Args:
            payload: JSON-compatible message payload. Use
                :meth:`send_typed_to_property_inspector` for a plugin-owned
                Python type.
        """

        self._send(
            SendToPropertyInspectorCommand(
                action=self.action,
                context=self.context,
                payload=payload,
            )
        )

    def send_typed_to_property_inspector(
        self,
        payload: PayloadT,
        codec: JsonCodec[PayloadT],
    ) -> None:
        """Encode and send a typed payload to the Property Inspector.

        Args:
            payload: Plugin-owned value to encode.
            codec: Codec that converts ``payload`` to a JSON object.

        Raises:
            JsonCodecEncodeError: If the codec fails or returns a value that is
                not a valid JSON object.
        """

        self._send(
            SendToPropertyInspectorCommand.from_payload(
                action=self.action,
                context=self.context,
                payload=payload,
                codec=codec,
            )
        )

    def set_state(self, state: int) -> None:
        """Select the displayed state for this action context.

        Args:
            state: Zero-based state index defined by the action manifest.
        """

        self._send(SetStateCommand(self.context, state))

    def set_title(
        self,
        title: str,
        *,
        target: int = 0,
        state: int | None = None,
    ) -> None:
        """Set the title displayed for this action context.

        Args:
            title: New title text. Pass an empty string to clear it.
            target: Stream Dock protocol target selector. The value is passed
                through unchanged and defaults to ``0``.
            state: Optional zero-based state to update. ``None`` omits the
                state field and lets Stream Dock apply its default behavior.
        """

        self._send(SetTitleCommand(self.context, title, target, state))

    @classmethod
    def decode_settings(cls, settings: JsonObject) -> SettingsT:
        """Decode wire settings using the class-level settings codec.

        Args:
            settings: Validated JSON object received from Stream Dock.

        Returns:
            A decoded settings value of the action's ``SettingsT`` type.

        Raises:
            JsonCodecDecodeError: If the configured codec cannot decode the
                object.
        """

        return cast(SettingsT, decode_with_codec(settings, cls.settings_codec))

    def update_settings_from_wire(self, settings: JsonObject) -> None:
        """Replace local settings with a freshly decoded wire value.

        The runtime calls this before :meth:`on_did_receive_settings`, so the
        callback can read the new value from :attr:`settings`.

        Args:
            settings: Settings object received from Stream Dock.

        Raises:
            JsonCodecDecodeError: If :attr:`settings_codec` rejects the value.
        """

        self.settings = self.decode_settings(settings)

    def set_settings(self, settings: SettingsT) -> None:
        """Persist settings for this context and update local state.

        The local :attr:`settings` value changes only after encoding and sending
        the command succeed. Stream Dock may later echo the persisted value in
        a ``didReceiveSettings`` event.

        Args:
            settings: New settings in the action's declared ``SettingsT`` type.

        Raises:
            JsonCodecEncodeError: If :attr:`settings_codec` cannot encode the
                value as a JSON object.
            JsonCodecDecodeError: If the encoded value cannot be decoded into
                isolated local settings.
        """

        command = SetSettingsCommand.from_settings(
            context=self.context,
            settings=settings,
            codec=cast(JsonCodec[SettingsT], self.settings_codec),
        )
        next_settings = self.decode_settings(command.settings)
        self._send(command)
        self.settings = next_settings

    def get_settings(self) -> None:
        """Request the latest persisted settings for this action context.

        The response is delivered asynchronously through
        :meth:`on_did_receive_settings`.
        """

        self._send(GetSettingsCommand(self.context))

    def set_image(
        self,
        image: str,
        *,
        target: int = 0,
        state: int | None = None,
    ) -> None:
        """Set the image displayed for this action context.

        Args:
            image: Image representation accepted by the Stream Dock protocol,
                typically a data URL. Pass an empty string to request the
                manifest's default image.
            target: Stream Dock protocol target selector. The value is passed
                through unchanged and defaults to ``0``.
            state: Optional zero-based state to update. ``None`` omits the
                state field.
        """

        self._send(SetImageCommand(self.context, image, target, state))

    def show_ok(self) -> None:
        """Show Stream Dock's temporary success indicator on this action."""

        self._send(ShowOkCommand(self.context))

    def show_alert(self) -> None:
        """Show Stream Dock's temporary failure indicator on this action."""

        self._send(ShowAlertCommand(self.context))

    def open_url(self, url: str) -> None:
        """Ask Stream Dock to open a URL with the operating system.

        Args:
            url: Absolute URL understood by the host operating system.
        """

        self._send(OpenUrlCommand(url))

    def log_message(self, message: str) -> None:
        """Write a message through Stream Dock's plugin log facility.

        Args:
            message: Human-readable diagnostic text.
        """

        self._send(LogMessageCommand(message))

    def on_will_appear(self, _event: WillAppearEvent) -> None:
        """Handle creation or appearance of this action context.

        Override this no-op callback to initialize or render the action. The
        runtime has already populated :attr:`settings` before calling it.

        Args:
            _event: Appearance event containing device, coordinates, controller,
                state, and raw settings information.
        """

        pass

    def on_will_disappear(self, _event: WillDisappearEvent | None = None) -> None:
        """Release resources owned by this action context.

        The event is ``None`` when cleanup happens without a wire
        ``willDisappear`` event, for example during plugin shutdown or rollback
        after a failed :meth:`on_will_appear` call. Implementations should be
        idempotent.

        Args:
            _event: Disappearance event when one was received, otherwise
                ``None``.
        """

        pass

    def on_did_receive_settings(self, _event: DidReceiveSettingsEvent) -> None:
        """Handle settings returned or changed by Stream Dock.

        :attr:`settings` has already been decoded and replaced when this
        callback runs.

        Args:
            _event: Event containing the raw settings and action metadata.
        """

        pass

    def on_did_receive_global_settings(self, _event: DidReceiveGlobalSettingsEvent) -> None:
        """Handle the latest plugin-wide settings.

        The runtime broadcasts this event to every active action and replays
        the latest value to actions created after global settings were loaded.

        Args:
            _event: Event containing an isolated copy of global settings.
        """

        pass

    def on_title_parameters_did_change(
        self,
        _event: TitleParametersDidChangeEvent,
    ) -> None:
        """Handle title text or formatting changes made by Stream Dock.

        :attr:`title` and :attr:`title_parameters` contain the new values before
        this callback is invoked.

        Args:
            _event: Event with the title, formatting parameters, and action
                metadata.
        """

        pass

    def on_key_down(self, _event: KeyDownEvent) -> None:
        """Handle a physical or virtual key press for this context.

        Args:
            _event: Key press event including coordinates, settings, and state.
        """

        pass

    def on_key_up(self, _event: KeyUpEvent) -> None:
        """Handle release of a physical or virtual key.

        Args:
            _event: Key release event including coordinates, settings, and state.
        """

        pass

    def on_touch_tap(self, _event: TouchTapEvent) -> None:
        """Handle a tap reported by a touch-capable control.

        Args:
            _event: Tap event including coordinates, settings, and state.
        """

        pass

    def on_dial_down(self, _event: DialDownEvent) -> None:
        """Handle pressing an encoder or dial.

        Args:
            _event: Dial press event with controller and action metadata.
        """

        pass

    def on_dial_up(self, _event: DialUpEvent) -> None:
        """Handle releasing an encoder or dial.

        Args:
            _event: Dial release event with controller and action metadata.
        """

        pass

    def on_dial_rotate(self, _event: DialRotateEvent) -> None:
        """Handle rotation of an encoder or dial.

        Args:
            _event: Rotation event whose ``ticks`` sign gives direction and
                whose ``pressed`` flag reports whether the dial is held down.
        """

        pass

    def on_property_inspector_did_appear(
        self,
        _event: PropertyInspectorDidAppearEvent,
    ) -> None:
        """Handle opening of this action's Property Inspector.

        Args:
            _event: Event identifying the action context and device.
        """

        pass

    def on_property_inspector_did_disappear(
        self,
        _event: PropertyInspectorDidDisappearEvent,
    ) -> None:
        """Handle closing of this action's Property Inspector.

        Args:
            _event: Event identifying the action context and device.
        """

        pass

    def on_send_to_plugin(self, _event: SendToPluginEvent) -> None:
        """Handle a message sent by this action's Property Inspector.

        Use :meth:`SendToPluginEvent.decode_message` with a custom codec for a
        typed message body.

        Args:
            _event: Property Inspector message and routing metadata.
        """

        pass

    def on_device_did_connect(self, _event: DeviceDidConnectEvent) -> None:
        """Handle connection of any Stream Dock device.

        Args:
            _event: Connected device identifier and metadata.
        """

        pass

    def on_device_did_disconnect(self, _event: DeviceDidDisconnectEvent) -> None:
        """Handle disconnection of any Stream Dock device.

        Args:
            _event: Disconnected device identifier and optional metadata.
        """

        pass

    def on_application_did_launch(self, _event: ApplicationDidLaunchEvent) -> None:
        """Handle launch of a monitored host application.

        Args:
            _event: Event containing the application identifier.
        """

        pass

    def on_application_did_terminate(self, _event: ApplicationDidTerminateEvent) -> None:
        """Handle termination of a monitored host application.

        Args:
            _event: Event containing the application identifier.
        """

        pass

    def on_system_did_wake_up(self, _event: SystemDidWakeUpEvent) -> None:
        """Handle the host system resuming from sleep.

        Args:
            _event: System wake event. It carries no additional payload.
        """

        pass

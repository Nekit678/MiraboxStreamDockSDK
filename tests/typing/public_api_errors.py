"""Invalid consumer calls: unused-ignore errors detect a weakened or Any API."""

from dataclasses import dataclass

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    FunctionalJsonCodec,
    JsonObject,
    LogMessageCommand,
    OwnedJsonPayload,
    StreamDockApplication,
    StreamDockSender,
)
from mirabox_sdk.testing import StreamDockHarness


@dataclass(frozen=True)
class SuppliedDependencies:
    stream_dock: StreamDockSender


@dataclass(frozen=True)
class ExpectedDependencies:
    stream_dock: StreamDockSender
    label: str


registry = ActionRegistry[SuppliedDependencies]()


@registry.register("com.example.incompatible")  # type: ignore[arg-type]
class NeedsLabel(Action[JsonObject, ExpectedDependencies]):
    def read_label(self) -> str:
        return self.dependencies.label


registry.register("com.example.incompatible.direct")(NeedsLabel)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CustomSettings:
    count: int


JSON_SETTINGS_CODEC = FunctionalJsonCodec[JsonObject](lambda value: value, lambda value: value)


class IncompatibleSettingsCodec(Action[CustomSettings, SuppliedDependencies]):
    settings_codec = JSON_SETTINGS_CODEC  # type: ignore[assignment]


@registry.register("com.example.missing.codec")  # type: ignore[arg-type]
class MissingSettingsCodec(Action[CustomSettings, SuppliedDependencies]):
    pass


registry.register("com.example.wrong.codec", settings_codec=JSON_SETTINGS_CODEC)(
    MissingSettingsCodec  # type: ignore[arg-type]
)
registry.register("com.example.missing.codec.direct")(MissingSettingsCodec)  # type: ignore[arg-type]


def decode_settings(value: JsonObject) -> CustomSettings:
    count = value.get("count", 0)
    if type(count) is not int:
        raise ValueError("count must be an integer")
    return CustomSettings(count)


CUSTOM_SETTINGS_CODEC = FunctionalJsonCodec[CustomSettings](
    decode_settings, lambda value: {"count": value.count}
)


@registry.register(  # type: ignore[arg-type]
    "com.example.typed.incompatible.dependencies", settings_codec=CUSTOM_SETTINGS_CODEC
)
class TypedNeedsLabel(Action[CustomSettings, ExpectedDependencies]):
    pass


@registry.register(  # type: ignore[arg-type]
    "com.example.widened.constructor", settings_codec=JSON_SETTINGS_CODEC
)
class WidenedSettingsConstructor(Action[CustomSettings, SuppliedDependencies]):
    def __init__(
        self,
        action: str,
        context: str,
        settings: object,
        dependencies: SuppliedDependencies,
    ) -> None:
        if not isinstance(settings, CustomSettings):
            raise TypeError("expected CustomSettings")
        super().__init__(action, context, settings, dependencies)


registry.register("com.example.widened.missing.codec")(
    WidenedSettingsConstructor  # type: ignore[arg-type]
)


def reject_invalid_settings(
    action: Action[CustomSettings, SuppliedDependencies],
    dependencies: SuppliedDependencies,
) -> None:
    action.set_settings({"count": 1})  # type: ignore[arg-type]
    action.settings_codec.encode({"count": 1})  # type: ignore[arg-type]
    MissingSettingsCodec("action", "button", {}, dependencies)  # type: ignore[arg-type]


def reject_invalid_calls(
    sender: StreamDockSender,
    context: ApplicationContext,
    application: StreamDockApplication,
    harness: StreamDockHarness,
) -> None:
    sender.send("untyped command")  # type: ignore[arg-type]
    sender.send_async({"event": "logMessage"})  # type: ignore[arg-type]
    LogMessageCommand(message=123)  # type: ignore[arg-type]
    context.global_settings.set([])  # type: ignore[arg-type]
    context.global_settings.loaded = True  # type: ignore[misc]
    context.session_readiness.wait("forever")  # type: ignore[arg-type]
    application.set_global_settings("settings")  # type: ignore[arg-type]
    _ = application.metrics().missing_metric  # type: ignore[attr-defined]
    harness.send_json([])  # type: ignore[arg-type]
    harness.wait_for_events("one")  # type: ignore[arg-type]
    payload: JsonObject = OwnedJsonPayload({})
    payload["invalid"] = object()  # type: ignore[assignment]

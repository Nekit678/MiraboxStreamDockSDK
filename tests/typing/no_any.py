"""Consumer expressions checked with disallow_any_expr, including imported SDK types."""

from dataclasses import dataclass
from typing import assert_type

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    CommandFuture,
    FunctionalJsonCodec,
    JsonCodec,
    JsonObject,
    LogMessageCommand,
    OwnedJsonPayload,
    ShutdownFailure,
    ShutdownOutcome,
    StopSignal,
    StreamDockApplication,
    StreamDockSender,
    ValidatedJsonObject,
    validate_plugin,
)
from mirabox_sdk.testing import FakeStreamDockSender, StreamDockHarness

registry = ActionRegistry[ApplicationContext]()


@registry.register("com.example.strict.action")
class StrictAction(Action[JsonObject, ApplicationContext]):
    def read_context(self) -> str:
        return self.context


assert_type(registry.register("com.example.strict.alias")(StrictAction), type[StrictAction])


@dataclass(frozen=True)
class CustomSettings:
    count: int


def decode_settings(value: JsonObject) -> CustomSettings:
    count = value.get("count", 0)
    if type(count) is not int:
        raise ValueError("count must be an integer")
    return CustomSettings(count)


CUSTOM_SETTINGS_CODEC = FunctionalJsonCodec[CustomSettings](
    decode_settings, lambda value: {"count": value.count}
)


@registry.register("com.example.strict.settings", settings_codec=CUSTOM_SETTINGS_CODEC)
class StrictSettingsAction(Action[CustomSettings, ApplicationContext]):
    def read_count(self) -> int:
        return self.settings.count


assert_type(
    registry.register("com.example.strict.settings.alias", settings_codec=CUSTOM_SETTINGS_CODEC)(
        StrictSettingsAction
    ),
    type[StrictSettingsAction],
)
assert_type(StrictSettingsAction.settings_codec, JsonCodec[CustomSettings])
assert_type(StrictSettingsAction.get_settings_codec(), JsonCodec[CustomSettings])
assert_type(StrictSettingsAction.decode_settings({"count": 1}), CustomSettings)
assert_type(StrictAction.settings_codec, JsonCodec[JsonObject])
assert_type(StrictAction.get_settings_codec(), JsonCodec[JsonObject])


def check_contract(
    context: ApplicationContext,
    application: StreamDockApplication,
    action: Action[JsonObject, ApplicationContext],
    harness: StreamDockHarness,
) -> None:
    sender: StreamDockSender = FakeStreamDockSender()
    assert_type(sender.send_async(LogMessageCommand("typed command")), CommandFuture)
    assert_type(context.stream_dock.send_async(LogMessageCommand("shared sender")), CommandFuture)
    assert_type(context.global_settings.loaded, bool)
    assert_type(context.global_settings.snapshot(), JsonObject)
    assert_type(context.session_readiness.wait(0), bool)
    assert_type(context.stop_signal, StopSignal)
    assert_type(context.stop_signal.requested, bool)
    assert_type(context.stop_signal.wait(0), bool)
    outcome = application.shutdown_outcome
    assert_type(outcome, ShutdownOutcome | None)
    if outcome is not None:
        assert_type(outcome.successful, bool)
        assert_type(outcome.pending_cleanup, tuple[str, ...])
        assert_type(outcome.cleanup_failures, tuple[ShutdownFailure, ...])
        assert_type(outcome.primary_failure, BaseException | None)
    assert_type(action.settings, JsonObject)
    assert_type(action.dependencies, ApplicationContext)
    strict_action = StrictAction("com.example.strict.action", "button", {}, context)
    assert_type(strict_action, StrictAction)
    assert_type(strict_action.read_context(), str)
    typed_action = StrictSettingsAction(
        "com.example.strict.settings", "button", CustomSettings(1), context
    )
    assert_type(typed_action, StrictSettingsAction)
    assert_type(typed_action.settings, CustomSettings)
    assert_type(typed_action.settings_codec, JsonCodec[CustomSettings])
    assert_type(typed_action.read_count(), int)
    typed_action.set_settings(CustomSettings(2))
    assert_type(application.metrics().event_pump.events_received, int)
    assert_type(harness.application, StreamDockApplication)
    assert_type(harness.context, ApplicationContext)
    assert_type(harness.receive(), JsonObject)
    assert_type(harness.messages, tuple[JsonObject, ...])
    assert_type(ActionRegistry[ApplicationContext]().action_uuids, frozenset[str])
    assert_type(
        validate_plugin("plugin.sdPlugin", action_uuids={"com.example.action"}), tuple[str, ...]
    )
    snapshot = ValidatedJsonObject({"count": 1})
    assert_type(snapshot.owned_payload(), OwnedJsonPayload)
    assert_type(snapshot.isolated_copy(), JsonObject)
    assert_type(OwnedJsonPayload(snapshot).isolated_copy(), JsonObject)

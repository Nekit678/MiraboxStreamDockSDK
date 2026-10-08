"""Consumer expressions checked with disallow_any_expr, including imported SDK types."""

from typing import assert_type

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    CommandFuture,
    JsonObject,
    LogMessageCommand,
    OwnedJsonPayload,
    StreamDockApplication,
    StreamDockSender,
    ValidatedJsonObject,
    validate_plugin,
)
from mirabox_sdk.testing import FakeStreamDockSender, StreamDockHarness


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
    assert_type(action.settings, JsonObject)
    assert_type(action.dependencies, ApplicationContext)
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

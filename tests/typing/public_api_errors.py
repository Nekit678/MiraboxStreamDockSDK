"""Invalid consumer calls: unused-ignore errors detect a weakened or Any API."""

from mirabox_sdk import (
    ApplicationContext,
    JsonObject,
    LogMessageCommand,
    OwnedJsonPayload,
    StreamDockApplication,
    StreamDockSender,
)
from mirabox_sdk.testing import StreamDockHarness


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

"""Privacy contracts for experimental runtime diagnostics."""

from __future__ import annotations

import unittest

from mirabox_sdk import KeyDownEvent, UnknownStreamDockEvent
from mirabox_sdk._next.runtime.global_settings import DefaultGlobalSettingsState
from mirabox_sdk._next.runtime.router import RuntimeEventRouter
from mirabox_sdk._next.runtime.session import SessionCoordinator
from mirabox_sdk._next.transport.session import Disconnected, TransportError

from .fakes import (
    RecordingAction,
    RecordingActionFactory,
    RecordingCommandSink,
    key_down_event,
    will_appear_event,
)

_SECRET = "runtime-secret-must-not-appear"


class _SensitiveFailure(RuntimeError):
    pass


class _FailingAction(RecordingAction):
    def on_key_down(self, _event: KeyDownEvent) -> None:
        raise _SensitiveFailure(_SECRET)


class _FailingHooks:
    def on_unhandled_event(self, _event: UnknownStreamDockEvent) -> None:
        raise _SensitiveFailure(_SECRET)


class RuntimeLoggingRedactionTests(unittest.TestCase):
    def test_payloads_reasons_and_exception_messages_are_not_logged(self) -> None:
        sender = RecordingCommandSink()
        router = RuntimeEventRouter(
            RecordingActionFactory(sender, _FailingAction),
            DefaultGlobalSettingsState("plugin-uuid", sender),
            plugin_hooks=_FailingHooks(),
        )
        session = SessionCoordinator(
            sender,
            register_event="registerPlugin",
            plugin_uuid="plugin-uuid",
        )
        router.dispatch(will_appear_event())
        unknown = UnknownStreamDockEvent(
            event="futureEvent",
            data={"event": "futureEvent", "payload": {"token": _SECRET}},
        )

        with self.assertLogs("mirabox_sdk._next.runtime", level="INFO") as logs:
            router.dispatch(key_down_event())
            router.dispatch(unknown)
            session.handle(TransportError(_SensitiveFailure(_SECRET)))
            session.handle(Disconnected(status_code=1000, reason=_SECRET))

        output = "\n".join(logs.output)
        self.assertNotIn(_SECRET, output)
        self.assertNotIn("payload", output)
        self.assertIn("exception_type=_SensitiveFailure", output)
        self.assertIn("status_code=1000", output)


if __name__ == "__main__":
    unittest.main()

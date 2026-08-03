from __future__ import annotations

import unittest

from mirabox_sdk import DidReceiveGlobalSettingsEvent, SetGlobalSettingsCommand
from mirabox_sdk._next.runtime.global_settings import DefaultGlobalSettingsState

from .fakes import RecordingCommandSink


class DefaultGlobalSettingsStateTests(unittest.TestCase):
    def test_received_state_and_replay_events_are_isolated(self) -> None:
        state = DefaultGlobalSettingsState("plugin-uuid", RecordingCommandSink())

        source = state.receive({"nested": {"count": 1}})
        first = state.new_event(source)
        second = state.new_event(source)
        first.settings["nested"]["count"] = 2  # type: ignore[index]

        self.assertTrue(state.loaded)
        self.assertEqual(second, DidReceiveGlobalSettingsEvent(settings={"nested": {"count": 1}}))
        self.assertEqual(state.settings, {"nested": {"count": 1}})

    def test_local_send_commits_only_after_command_success(self) -> None:
        sender = RecordingCommandSink()
        state = DefaultGlobalSettingsState("plugin-uuid", sender)
        state.set({"count": 1})
        failure = RuntimeError("send failed")
        sender.failures[SetGlobalSettingsCommand] = failure

        with self.assertRaises(RuntimeError) as raised:
            state.update(lambda settings: settings.update(count=2))

        self.assertIs(raised.exception, failure)
        self.assertEqual(state.settings, {"count": 1})
        self.assertEqual(len(sender.commands), 2)
        self.assertEqual(sender.commands[0].context, "plugin-uuid")


if __name__ == "__main__":
    unittest.main()

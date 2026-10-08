from __future__ import annotations

import unittest

from mirabox_sdk import DidReceiveGlobalSettingsEvent, JsonObject, SetGlobalSettingsCommand
from mirabox_sdk._internal.runtime.global_settings import DefaultGlobalSettingsState

from .fakes import RecordingCommandSink


class DefaultGlobalSettingsStateTests(unittest.TestCase):
    def test_unloaded_settings_view_cannot_mutate_state(self) -> None:
        sender = RecordingCommandSink()
        state = DefaultGlobalSettingsState("plugin-uuid", sender)

        state.settings["bypassed"] = True

        self.assertFalse(state.loaded)
        self.assertEqual(state.settings, {})
        self.assertEqual(sender.commands, [])

    def test_settings_views_isolate_nested_containers_and_replay(self) -> None:
        sender = RecordingCommandSink()
        state = DefaultGlobalSettingsState("plugin-uuid", sender)
        state.receive({"nested": {"items": [{"count": 1}]}})
        first = state.settings
        second = state.settings

        nested = first["nested"]
        assert isinstance(nested, dict)
        items = nested["items"]
        assert isinstance(items, list)
        item = items[0]
        assert isinstance(item, dict)
        item["count"] = 2
        items.append({"count": 3})
        first["bypassed"] = True

        expected = {"nested": {"items": [{"count": 1}]}}
        self.assertTrue(state.loaded)
        self.assertEqual(second, expected)
        self.assertEqual(state.settings, expected)
        self.assertEqual(state.new_event().settings, expected)
        self.assertEqual(sender.commands, [])

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

    def test_callback_and_validation_failures_preserve_state(self) -> None:
        sender = RecordingCommandSink()
        state = DefaultGlobalSettingsState("plugin-uuid", sender)
        state.receive({"nested": {"count": 1}})

        def fail_update(settings: JsonObject) -> None:
            nested = settings["nested"]
            assert isinstance(nested, dict)
            nested["count"] = 2
            raise RuntimeError("update failed")

        def invalidate_update(settings: JsonObject) -> None:
            settings["invalid"] = object()  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "update failed"):
            state.update(fail_update)
        with self.assertRaises(ValueError):
            state.update(invalidate_update)
        with self.assertRaises(ValueError):
            state.set({"invalid": object()})  # type: ignore[dict-item]

        self.assertTrue(state.loaded)
        self.assertEqual(state.settings, {"nested": {"count": 1}})
        self.assertEqual(sender.commands, [])


if __name__ == "__main__":
    unittest.main()

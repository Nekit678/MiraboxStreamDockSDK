"""Integration tests for the minimal counter example."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from counter_plugin.actions.counter import ACTION_UUID, CounterAction
from counter_plugin.contracts import ActionDependencies

from mirabox_sdk import PropertyInspectorMessage, SendToPluginEvent

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]


class CounterActionTests(unittest.TestCase):
    def test_increments_and_resets_persisted_count(self) -> None:
        stream_dock = Mock()
        action = CounterAction(
            ACTION_UUID,
            "button",
            {},
            ActionDependencies(stream_dock),
        )

        action.on_will_appear(Mock())
        action.on_key_down(Mock())
        action.on_send_to_plugin(
            SendToPluginEvent(
                action=ACTION_UUID,
                context="button",
                message=PropertyInspectorMessage(
                    name="reset",
                    value={"event": "reset"},
                ),
            )
        )

        self.assertEqual(action.settings, {"count": 0})
        wires = [call.args[0].to_wire() for call in stream_dock.send.call_args_list]
        self.assertIn(
            {"event": "setSettings", "context": "button", "payload": {"count": 1}},
            wires,
        )
        self.assertEqual(wires[-1]["payload"]["title"], "0")


class CounterBundleTests(unittest.TestCase):
    def test_manifest_references_existing_files(self) -> None:
        bundle = EXAMPLE_ROOT / "com.example.counter.sdPlugin"
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["CodePath"], "CounterPlugin.exe")
        for action in manifest["Actions"]:
            self.assertTrue((bundle / action["Icon"]).is_file())
            self.assertTrue((bundle / action["PropertyInspectorPath"]).is_file())

    def test_property_inspector_client_matches_installed_sdk(self) -> None:
        from mirabox_sdk import property_inspector_client_bytes

        client = (
            EXAMPLE_ROOT / "com.example.counter.sdPlugin" / "property-inspector" / "mirabox-sdk.js"
        )

        self.assertEqual(client.read_bytes(), property_inspector_client_bytes())


if __name__ == "__main__":
    unittest.main()

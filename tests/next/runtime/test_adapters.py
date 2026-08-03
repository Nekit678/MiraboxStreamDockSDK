from __future__ import annotations

import unittest

from mirabox_sdk import ActionRegistry
from mirabox_sdk._next.runtime.adapters import LegacyActionFactoryAdapter

from .fakes import FakeDependencies, RecordingAction, RecordingCommandSink


class LegacyActionFactoryAdapterTests(unittest.TestCase):
    def test_binds_dependencies_without_exposing_them_to_runtime_route(self) -> None:
        sender = RecordingCommandSink()
        dependencies = FakeDependencies(sender)
        registry: ActionRegistry[FakeDependencies] = ActionRegistry()
        registry.register("com.example.action")(RecordingAction)
        factory = LegacyActionFactoryAdapter(registry, dependencies)

        action = factory.create("com.example.action", "button", {"count": 1})

        self.assertIsInstance(action, RecordingAction)
        assert isinstance(action, RecordingAction)
        self.assertIs(action.dependencies, dependencies)
        self.assertEqual(action.context, "button")


if __name__ == "__main__":
    unittest.main()

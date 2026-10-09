from __future__ import annotations

import unittest
from unittest.mock import patch

import mirabox_sdk.json_types as json_types
from mirabox_sdk import (
    JSON_OBJECT_CODEC,
    ActionRegistry,
    FunctionalJsonCodec,
    JsonCodecDecodeError,
    JsonObject,
    JsonObjectCodec,
)
from mirabox_sdk._internal.runtime.actions import DefaultActionContextManager
from mirabox_sdk._internal.runtime.adapters import ActionRegistryFactoryAdapter

from .fakes import FakeDependencies, RecordingAction, RecordingCommandSink, will_appear_event


class ActionRegistryFactoryAdapterTests(unittest.TestCase):
    def test_runtime_transfers_one_plain_copy_to_default_and_custom_codecs(self) -> None:
        class PassthroughCodec(JsonObjectCodec):
            def decode(self, value: JsonObject) -> JsonObject:
                return value

        for codec in (
            JSON_OBJECT_CODEC,
            FunctionalJsonCodec[JsonObject](lambda value: value, lambda value: value),
            PassthroughCodec(),
        ):
            with self.subTest(codec=type(codec).__name__):

                class TestAction(RecordingAction):
                    pass

                registry: ActionRegistry[FakeDependencies] = ActionRegistry()
                registry.register("com.example.action", settings_codec=codec)(TestAction)
                factory = ActionRegistryFactoryAdapter(
                    registry, FakeDependencies(RecordingCommandSink())
                )
                manager = DefaultActionContextManager(factory)
                event = will_appear_event(
                    action="com.example.action", settings={"nested": {"items": [1]}}
                )
                with patch(
                    "mirabox_sdk.json_types._clone_json_dict",
                    wraps=json_types._clone_json_dict,
                ) as clone:
                    action = manager.create(event)
                self.assertEqual(clone.call_count, 2)
                assert isinstance(action, TestAction)
                self.assertIs(type(action.settings), dict)
                nested = dict.__getitem__(action.settings, "nested")
                self.assertIs(type(nested), dict)
                items = dict.__getitem__(nested, "items")
                self.assertIs(type(items), list)
                list.append(items, 2)
                self.assertEqual(event.settings, {"nested": {"items": [1]}})

                # The public registry still accepts borrowed caller-owned input.
                borrowed: JsonObject = {"nested": {"items": [1]}}
                public_action = registry.create(
                    "com.example.action",
                    "public",
                    borrowed,
                    FakeDependencies(RecordingCommandSink()),
                )
                assert public_action is not None
                public_action.settings["nested"]["items"].append(2)
                self.assertEqual(borrowed, {"nested": {"items": [1]}})

    def test_runtime_preserves_action_decoder_override(self) -> None:
        seen: list[JsonObject] = []

        class CustomAction(RecordingAction):
            @classmethod
            def decode_settings(cls, settings: JsonObject) -> JsonObject:
                seen.append(settings)
                settings["custom"] = True
                return settings

        registry: ActionRegistry[FakeDependencies] = ActionRegistry()
        registry.register("com.example.action")(CustomAction)
        factory = ActionRegistryFactoryAdapter(registry, FakeDependencies(RecordingCommandSink()))
        event = will_appear_event(action="com.example.action")
        action = DefaultActionContextManager(factory).create(event)

        assert isinstance(action, CustomAction)
        self.assertEqual(len(seen), 1)
        self.assertEqual(action.settings, {"count": 1, "custom": True})
        self.assertEqual(event.settings, {"count": 1})

    def test_runtime_preserves_registry_subclass_override(self) -> None:
        seen: list[JsonObject] = []

        class CustomRegistry(ActionRegistry[FakeDependencies]):
            def create(
                self,
                action_uuid: str,
                context: str,
                settings: JsonObject,
                dependencies: FakeDependencies,
            ):
                seen.append(settings)
                settings["custom"] = True
                return super().create(action_uuid, context, settings, dependencies)

        registry = CustomRegistry()
        registry.register("com.example.action")(RecordingAction)
        factory = ActionRegistryFactoryAdapter(registry, FakeDependencies(RecordingCommandSink()))
        event = will_appear_event(action="com.example.action")
        action = DefaultActionContextManager(factory).create(event)

        assert isinstance(action, RecordingAction)
        self.assertEqual(len(seen), 1)
        self.assertEqual(action.settings, {"count": 1, "custom": True})
        self.assertEqual(event.settings, {"count": 1})

    def test_codec_mutation_and_failure_preserve_event_and_error_path(self) -> None:
        for codec_type in ("functional", "custom"):
            with self.subTest(codec_type=codec_type):

                def fail(settings: JsonObject) -> JsonObject:
                    settings.clear()
                    raise JsonCodecDecodeError("invalid count", path=("count",))

                class FailingCodec(JsonObjectCodec):
                    def decode(self, value: JsonObject) -> JsonObject:
                        return fail(value)

                codec = (
                    FunctionalJsonCodec(fail, lambda value: value)
                    if codec_type == "functional"
                    else FailingCodec()
                )

                class TestAction(RecordingAction):
                    pass

                registry: ActionRegistry[FakeDependencies] = ActionRegistry()
                registry.register("com.example.action", settings_codec=codec)(TestAction)
                factory = ActionRegistryFactoryAdapter(
                    registry, FakeDependencies(RecordingCommandSink())
                )
                manager = DefaultActionContextManager(factory)
                event = will_appear_event(action="com.example.action")
                with self.assertRaises(JsonCodecDecodeError) as raised:
                    manager.create(event)
                self.assertEqual(raised.exception.path, ("payload", "settings", "count"))
                self.assertEqual(event.settings, {"count": 1})
                self.assertEqual(manager.snapshot(), ())

    def test_binds_dependencies_without_exposing_them_to_runtime_route(self) -> None:
        sender = RecordingCommandSink()
        dependencies = FakeDependencies(sender)
        registry: ActionRegistry[FakeDependencies] = ActionRegistry()
        registry.register("com.example.action")(RecordingAction)
        factory = ActionRegistryFactoryAdapter(registry, dependencies)

        action = factory.create("com.example.action", "button", {"count": 1})

        self.assertIsInstance(action, RecordingAction)
        assert isinstance(action, RecordingAction)
        self.assertIs(action.dependencies, dependencies)
        self.assertEqual(action.context, "button")


if __name__ == "__main__":
    unittest.main()

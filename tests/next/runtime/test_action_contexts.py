from __future__ import annotations

import unittest
from threading import Event, Thread
from unittest.mock import Mock

from mirabox_sdk import JsonObject
from mirabox_sdk._next.runtime.actions import (
    DefaultActionContextManager,
    RuntimeActionIdentityError,
)
from mirabox_sdk._next.runtime.ports import ActionContextManager

from .fakes import RecordingActionFactory, will_appear_event


class ActionContextManagerTests(unittest.TestCase):
    def test_create_owns_input_and_rejects_duplicate_or_unknown_actions(self) -> None:
        factory = RecordingActionFactory(Mock())
        factory.unknown_uuids.add("com.example.unknown")
        manager = DefaultActionContextManager(factory)
        settings = {"nested": {"count": 1}}
        event = will_appear_event(settings=settings)

        action = manager.create(event)
        duplicate = manager.create(event)
        unknown = manager.create(will_appear_event(context="unknown", action="com.example.unknown"))

        self.assertIsInstance(manager, ActionContextManager)
        self.assertIs(manager.get("button"), action)
        self.assertIsNone(duplicate)
        self.assertIsNone(unknown)
        passed_settings = factory.calls[0][2]
        passed_nested = passed_settings["nested"]
        assert isinstance(passed_nested, dict)
        passed_nested["count"] = 2
        self.assertEqual(event.settings, {"nested": {"count": 1}})
        self.assertEqual(
            manager.metrics(),
            manager.metrics().__class__(
                action_instances_created=1,
                duplicate_appearances=1,
                unknown_action_uuids=1,
            ),
        )

    def test_snapshot_clear_and_identity_guard_do_not_expose_mutable_mapping(self) -> None:
        manager = DefaultActionContextManager(RecordingActionFactory(Mock()))
        first = manager.create(will_appear_event(context="first"))
        second = manager.create(will_appear_event(context="second"))
        assert first is not None
        assert second is not None
        snapshot = manager.snapshot()

        self.assertEqual(snapshot, (first, second))
        self.assertIsNone(manager.remove("first", expected=second))
        self.assertIs(manager.get("first"), first)
        self.assertEqual(manager.clear(), (first, second))
        self.assertEqual(manager.snapshot(), ())
        self.assertEqual(snapshot, (first, second))
        self.assertEqual(manager.metrics().actions_removed, 2)

    def test_factory_identity_mismatch_is_not_retained(self) -> None:
        class WrongIdentityFactory:
            def create(
                self,
                action_uuid: str,
                context: str,
                initial_settings: JsonObject,
            ):
                action = RecordingActionFactory(Mock()).create(
                    action_uuid,
                    "wrong-context",
                    initial_settings,
                )
                return action

        manager = DefaultActionContextManager(WrongIdentityFactory())

        with self.assertRaises(RuntimeActionIdentityError):
            manager.create(will_appear_event())

        self.assertEqual(manager.snapshot(), ())

    def test_concurrent_duplicate_create_constructs_only_one_instance(self) -> None:
        entered = Event()
        release = Event()
        delegate = RecordingActionFactory(Mock())

        class BlockingFactory:
            calls = 0

            def create(
                self,
                action_uuid: str,
                context: str,
                initial_settings: JsonObject,
            ):
                self.calls += 1
                entered.set()
                if not release.wait(1):
                    raise AssertionError("test did not release action factory")
                return delegate.create(
                    action_uuid,
                    context,
                    initial_settings,
                )

        factory = BlockingFactory()
        manager = DefaultActionContextManager(factory)
        event = will_appear_event()
        results: list[object] = []
        workers = [Thread(target=lambda: results.append(manager.create(event))) for _ in range(6)]

        for worker in workers:
            worker.start()
        self.assertTrue(entered.wait(1))
        release.set()
        for worker in workers:
            worker.join(1)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(factory.calls, 1)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(manager.metrics().duplicate_appearances, 5)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from typing import ClassVar
from unittest.mock import Mock

from mirabox_sdk import (
    DidReceiveGlobalSettingsEvent,
    FunctionalJsonCodec,
    GlobalSettingsStore,
    JsonCodecDecodeError,
    JsonObject,
    SystemDidWakeUpEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from mirabox_sdk._next.runtime.models import DispatchOutcome
from mirabox_sdk._next.runtime.ports import RuntimeEventDispatcher
from mirabox_sdk._next.runtime.router import RuntimeEventRouter

from .fakes import (
    FakeDependencies,
    RecordingAction,
    RecordingActionFactory,
    RecordingPluginHooks,
    did_receive_settings_event,
    key_down_event,
    title_event,
    will_appear_event,
    will_disappear_event,
)


def build_router(
    action_type: type[RecordingAction] = RecordingAction,
    *,
    hooks: RecordingPluginHooks | None = None,
) -> tuple[RuntimeEventRouter, RecordingActionFactory, GlobalSettingsStore, Mock]:
    sender = Mock()
    factory = RecordingActionFactory(sender, action_type)
    state = GlobalSettingsStore("plugin.uuid", sender)
    router = RuntimeEventRouter(factory, state, plugin_hooks=hooks)
    return router, factory, state, sender


class FailingAppearanceAction(RecordingAction):
    last_instance: ClassVar[FailingAppearanceAction | None] = None

    def __init__(
        self,
        action: str,
        context: str,
        settings: JsonObject,
        dependencies: FakeDependencies,
    ) -> None:
        super().__init__(action, context, settings, dependencies)
        type(self).last_instance = self

    def on_will_appear(self, event: WillAppearEvent) -> None:
        self.events.append(event)
        raise RuntimeError("appearance failed")


class FailingBroadcastAction(RecordingAction):
    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
        if self.context == "failing":
            raise RuntimeError("broadcast failed")
        self.events.append(event)


class MutatingGlobalSettingsAction(RecordingAction):
    def on_did_receive_global_settings(self, event: DidReceiveGlobalSettingsEvent) -> None:
        self.events.append(event)
        if self.context != "mutating":
            return
        nested = event.settings["nested"]
        assert isinstance(nested, dict)
        nested["value"] = 99


def _decode_count(value: JsonObject) -> JsonObject:
    count = value.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValueError("count must be an integer")
    return {"count": count}


class StrictSettingsAction(RecordingAction):
    settings_codec = FunctionalJsonCodec(_decode_count, lambda value: value)


class RuntimeEventDispatchTests(unittest.TestCase):
    def test_router_implements_runtime_event_dispatcher_port(self) -> None:
        router, _factory, _state, _sender = build_router()

        self.assertIsInstance(router, RuntimeEventDispatcher)

    def test_action_lifecycle_and_callbacks_apply_in_route_order(self) -> None:
        router, factory, _state, _sender = build_router()
        appear = will_appear_event()
        key = key_down_event()

        self.assertIs(router.dispatch(appear).outcome, DispatchOutcome.HANDLED)
        action = factory.instances[0]
        self.assertIs(router.contexts.get("button"), action)
        self.assertIs(router.dispatch(key).outcome, DispatchOutcome.HANDLED)
        self.assertIs(router.dispatch(will_disappear_event()).outcome, DispatchOutcome.HANDLED)

        self.assertEqual(action.events, [appear, key, will_disappear_event()])
        self.assertIsNone(router.contexts.get("button"))
        self.assertEqual(router.action_metrics().action_instances_created, 1)
        self.assertEqual(router.action_metrics().actions_removed, 1)
        self.assertEqual(router.routing_metrics().known_events_routed, 3)

    def test_disappearance_removes_context_before_callback(self) -> None:
        class RemovalObservingAction(RecordingAction):
            router: ClassVar[RuntimeEventRouter]
            removed_before_callback = False

            def on_will_disappear(self, event: WillDisappearEvent | None = None) -> None:
                type(self).removed_before_callback = self.router.contexts.get(self.context) is None
                self.events.append(event)

        router, _factory, _state, _sender = build_router(RemovalObservingAction)
        RemovalObservingAction.router = router
        router.dispatch(will_appear_event())

        router.dispatch(will_disappear_event())

        self.assertTrue(RemovalObservingAction.removed_before_callback)

    def test_appearance_failure_rolls_back_exact_instance_and_runs_cleanup(self) -> None:
        router, _factory, _state, _sender = build_router(FailingAppearanceAction)

        with self.assertLogs("mirabox_sdk._next.runtime.actions", level="ERROR"):
            result = router.dispatch(will_appear_event())

        self.assertIs(result.outcome, DispatchOutcome.CALLBACK_FAILED)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertEqual(router.contexts.snapshot(), ())
        action = FailingAppearanceAction.last_instance
        assert action is not None
        self.assertIsNone(action.events[-1])
        self.assertEqual(router.action_metrics().appearance_rollbacks, 1)

    def test_appearance_codec_failure_is_contextualized_without_partial_action(self) -> None:
        router, _factory, _state, _sender = build_router(StrictSettingsAction)

        with self.assertLogs("mirabox_sdk._next.runtime.actions", level="ERROR"):
            result = router.dispatch(will_appear_event(settings={"count": "invalid"}))

        self.assertIs(result.outcome, DispatchOutcome.CALLBACK_FAILED)
        self.assertIsInstance(result.error, JsonCodecDecodeError)
        assert isinstance(result.error, JsonCodecDecodeError)
        self.assertEqual(result.error.event_name, "willAppear")
        self.assertEqual(result.error.path, ("payload", "settings"))
        self.assertEqual(router.contexts.snapshot(), ())
        self.assertEqual(router.action_metrics().action_instances_created, 0)

    def test_duplicate_unknown_and_missing_context_are_observable_ignores(self) -> None:
        router, factory, _state, _sender = build_router()
        factory.unknown_uuids.add("com.example.unknown")

        first = router.dispatch(will_appear_event())
        duplicate = router.dispatch(will_appear_event())
        unknown = router.dispatch(
            will_appear_event(context="unknown", action="com.example.unknown")
        )
        missing = router.dispatch(key_down_event(context="missing"))

        self.assertIs(first.outcome, DispatchOutcome.HANDLED)
        self.assertTrue(
            all(
                result.outcome is DispatchOutcome.IGNORED
                for result in (duplicate, unknown, missing)
            )
        )
        metrics = router.action_metrics()
        self.assertEqual(metrics.duplicate_appearances, 1)
        self.assertEqual(metrics.unknown_action_uuids, 1)
        self.assertEqual(metrics.missing_contexts, 1)

    def test_settings_and_title_state_are_replaced_before_callbacks(self) -> None:
        router, factory, _state, _sender = build_router()
        router.dispatch(will_appear_event())
        settings = did_receive_settings_event({"count": 2})
        title = title_event()

        settings_result = router.dispatch(settings)
        title_result = router.dispatch(title)

        action = factory.instances[0]
        self.assertIs(settings_result.outcome, DispatchOutcome.HANDLED)
        self.assertIs(title_result.outcome, DispatchOutcome.HANDLED)
        self.assertEqual(action.settings_seen, [{"count": 2}])
        self.assertEqual(action.titles_seen, [(title.title, title.title_parameters)])
        self.assertEqual(router.action_metrics().settings_updates, 1)
        self.assertEqual(router.action_metrics().title_updates, 1)

    def test_settings_codec_failure_preserves_old_state_and_skips_callback(self) -> None:
        router, factory, _state, _sender = build_router(StrictSettingsAction)
        router.dispatch(will_appear_event(settings={"count": 1}))
        action = factory.instances[0]

        with self.assertLogs("mirabox_sdk._next.runtime.actions", level="ERROR"):
            result = router.dispatch(did_receive_settings_event({"count": "invalid"}))

        self.assertIs(result.outcome, DispatchOutcome.CALLBACK_FAILED)
        self.assertIsInstance(result.error, JsonCodecDecodeError)
        assert isinstance(result.error, JsonCodecDecodeError)
        self.assertEqual(result.error.event_name, "didReceiveSettings")
        self.assertEqual(result.error.path, ("payload", "settings"))
        self.assertEqual(action.settings, {"count": 1})
        self.assertEqual(action.settings_seen, [])
        self.assertEqual(router.action_metrics().settings_update_failures, 1)

    def test_broadcast_failure_does_not_block_healthy_actions(self) -> None:
        router, factory, _state, _sender = build_router(FailingBroadcastAction)
        router.dispatch(will_appear_event(context="failing"))
        router.dispatch(will_appear_event(context="healthy"))
        event = SystemDidWakeUpEvent()

        with self.assertLogs("mirabox_sdk._next.runtime.actions", level="ERROR"):
            result = router.dispatch(event)

        self.assertIs(result.outcome, DispatchOutcome.CALLBACK_FAILED)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertEqual(factory.instances[1].events[-1], event)
        metrics = router.action_metrics()
        self.assertEqual(metrics.broadcasts, 1)
        self.assertEqual(metrics.broadcast_targets, 2)
        self.assertEqual(metrics.broadcast_failures, 1)

    def test_broadcast_uses_one_snapshot_when_callback_creates_an_action(self) -> None:
        class CreatingAction(RecordingAction):
            router: ClassVar[RuntimeEventRouter]

            def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
                self.events.append(event)
                if self.context == "first":
                    self.router.dispatch(will_appear_event(context="late"))

        router, factory, _state, _sender = build_router(CreatingAction)
        CreatingAction.router = router
        router.dispatch(will_appear_event(context="first"))
        router.dispatch(will_appear_event(context="second"))
        event = SystemDidWakeUpEvent()

        router.dispatch(event)

        self.assertEqual(len(factory.instances), 3)
        self.assertEqual(factory.instances[0].events[-1], event)
        self.assertEqual(factory.instances[1].events[-1], event)
        self.assertNotIn(event, factory.instances[2].events)
        self.assertEqual(router.action_metrics().broadcast_targets, 2)

    def test_global_settings_update_broadcast_and_replay_are_isolated(self) -> None:
        router, factory, state, _sender = build_router(MutatingGlobalSettingsAction)
        router.dispatch(will_appear_event(context="mutating"))
        router.dispatch(will_appear_event(context="healthy"))
        incoming = DidReceiveGlobalSettingsEvent(settings={"nested": {"value": 1}})

        result = router.dispatch(incoming)
        router.dispatch(will_appear_event(context="late"))

        self.assertIs(result.outcome, DispatchOutcome.HANDLED)
        first_event = factory.instances[0].events[-1]
        second_event = factory.instances[1].events[-1]
        late_event = factory.instances[2].events[-1]
        assert isinstance(first_event, DidReceiveGlobalSettingsEvent)
        assert isinstance(second_event, DidReceiveGlobalSettingsEvent)
        assert isinstance(late_event, DidReceiveGlobalSettingsEvent)
        self.assertIsNot(first_event, second_event)
        self.assertEqual(first_event.settings, {"nested": {"value": 99}})
        self.assertEqual(second_event.settings, {"nested": {"value": 1}})
        self.assertEqual(late_event.settings, {"nested": {"value": 1}})
        self.assertEqual(state.settings, {"nested": {"value": 1}})
        metrics = router.action_metrics()
        self.assertEqual(metrics.global_settings_updates, 1)
        self.assertEqual(metrics.global_settings_replays, 1)

    def test_empty_global_settings_are_loaded_and_replayed(self) -> None:
        router, factory, _state, _sender = build_router()

        router.dispatch(DidReceiveGlobalSettingsEvent(settings={}))
        router.dispatch(will_appear_event())

        replay = factory.instances[0].events[-1]
        self.assertEqual(replay, DidReceiveGlobalSettingsEvent(settings={}))

    def test_unknown_event_is_delivered_once_and_hook_failure_isolated(self) -> None:
        hooks = RecordingPluginHooks()
        router, _factory, _state, _sender = build_router(hooks=hooks)
        event = UnknownStreamDockEvent(
            event="futureEvent",
            data={"event": "futureEvent", "payload": {"secret": "not logged"}},
        )

        first = router.dispatch(event)
        hooks.error = RuntimeError("hook failed")
        with self.assertLogs("mirabox_sdk._next.runtime.router", level="ERROR") as logs:
            second = router.dispatch(event)

        self.assertIs(first.outcome, DispatchOutcome.HANDLED)
        self.assertIs(second.outcome, DispatchOutcome.CALLBACK_FAILED)
        self.assertEqual(hooks.events, [event, event])
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertNotIn("hook failed", "\n".join(logs.output))
        self.assertEqual(router.routing_metrics().unknown_events_delivered, 2)

    def test_local_global_settings_failure_preserves_replay_snapshot(self) -> None:
        router, factory, state, sender = build_router()
        router.dispatch(DidReceiveGlobalSettingsEvent(settings={"theme": "light"}))
        sender.send.side_effect = RuntimeError("send failed")

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            router.global_settings.set({"theme": "dark"})

        self.assertEqual(state.settings, {"theme": "light"})
        sender.send.side_effect = None
        router.global_settings.set({"theme": "dark"})
        router.dispatch(will_appear_event())
        replay = factory.instances[0].events[-1]
        self.assertEqual(
            replay,
            DidReceiveGlobalSettingsEvent(settings={"theme": "dark"}),
        )
        self.assertEqual(router.action_metrics().global_settings_updates, 2)


if __name__ == "__main__":
    unittest.main()

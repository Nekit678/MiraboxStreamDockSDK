from __future__ import annotations

import unittest
from threading import Event, Thread
from time import monotonic
from unittest.mock import Mock, patch

import mirabox_sdk.json_types as json_types
from mirabox_sdk import (
    CommandFuture,
    DidReceiveGlobalSettingsEvent,
    FunctionalJsonCodec,
    GlobalSettingsBusyError,
    JsonObject,
    LogMessageCommand,
    OutboundCommandBusClosedError,
    OutboundCommandBusNotReadyError,
    OutboundCommandCancelledError,
    OutboundCommandDeadlineExceededError,
    OutboundCommandReentrancyError,
    OutboundQueueFullError,
    SetGlobalSettingsCommand,
)
from mirabox_sdk._internal.messaging.outbound import (
    OutboundCommandQueue,
    WriterReadyOutboundCommandSink,
)
from mirabox_sdk._internal.runtime.global_settings import (
    DefaultGlobalSettingsState,
    GlobalSettingsCoordinator,
)
from mirabox_sdk._internal.runtime.metrics import _ActionContextMetricRecorder

from .fakes import RecordingCommandSink


class DefaultGlobalSettingsStateTests(unittest.TestCase):
    def test_completion_callback_rejects_sync_writes_before_waiting_or_mutating(self) -> None:
        queue = OutboundCommandQueue(1)
        state = DefaultGlobalSettingsState("plugin", queue)
        pending = state.set_async({"count": 1})
        completion = CommandFuture()
        mutate = Mock()
        encode = Mock(return_value={"count": 2})
        codec = FunctionalJsonCodec(decoder=lambda raw: raw, encoder=encode)
        errors = []

        def observe(_completion):
            deadline = monotonic() + 0.02
            for write in (
                lambda: state.set({"count": 2}, deadline=deadline),
                lambda: state.update(mutate, deadline=deadline),
                lambda: state.set_typed({"count": 2}, codec, deadline=deadline),
            ):
                try:
                    write()
                except Exception as exc:
                    errors.append(exc)

        try:
            completion.add_done_callback(observe)
            completion._finish()
            self.assertEqual(len(errors), 3)
            self.assertTrue(
                all(isinstance(error, OutboundCommandReentrancyError) for error in errors)
            )
            mutate.assert_not_called()
            encode.assert_not_called()
            self.assertEqual(state.settings, {})
            self.assertEqual(queue.metrics().enqueued, 1)
            queue.receive().completion._finish()
            pending.result(timeout=0)
            self.assertEqual(state.settings, {"count": 1})
        finally:
            queue.shutdown(timeout=0)

    def test_async_deadline_and_cancel_rollback_before_public_completion(self) -> None:
        codec = FunctionalJsonCodec[int](
            decoder=lambda raw: int(raw["count"]), encoder=lambda count: {"count": count}
        )
        for method in ("set_async", "update_async", "set_typed_async"):
            for cancel in (False, True):
                with self.subTest(method=method, cancel=cancel):
                    queue = OutboundCommandQueue(1)
                    state = DefaultGlobalSettingsState("plugin", queue)
                    state.receive({"count": 1})
                    facade = GlobalSettingsCoordinator(state)
                    deadline = monotonic() + (10 if cancel else 0.03)
                    if method == "set_async":
                        completion = facade.set_async({"count": 2}, deadline=deadline)
                    elif method == "update_async":
                        completion = facade.update_async(
                            lambda draft: draft.update(count=2), deadline=deadline
                        )
                    else:
                        completion = facade.set_typed_async(2, codec, deadline=deadline)
                    observations = []
                    observer_finished = Event()

                    def observe(
                        result, observations=observations, facade=facade, done=observer_finished
                    ):
                        observations.append(facade.snapshot())
                        done.set()

                    completion.add_done_callback(observe)
                    if cancel:
                        self.assertTrue(completion.cancel())
                        self.assertTrue(completion.cancelled())
                    with self.assertRaises(
                        OutboundCommandCancelledError
                        if cancel
                        else OutboundCommandDeadlineExceededError
                    ):
                        completion.result(timeout=1)
                    self.assertTrue(observer_finished.wait(1))
                    self.assertEqual(observations, [{"count": 1}])
                    next_write = facade.set_async({"count": 3})
                    queue.receive().completion._finish()
                    next_write.result(timeout=0)
                    self.assertEqual(facade.snapshot(), {"count": 3})
                    queue.shutdown(timeout=0)

    def test_sync_settings_deadline_does_not_cancel_an_existing_transaction(self) -> None:
        queue = OutboundCommandQueue(1)
        state = DefaultGlobalSettingsState("plugin", queue)
        state.receive({"count": 1})
        facade = GlobalSettingsCoordinator(state)
        pending = facade.set_async({"count": 2})
        mutation = Mock()
        with self.assertRaises(OutboundCommandDeadlineExceededError):
            facade.update(mutation, deadline=monotonic() + 0.02)
        mutation.assert_not_called()
        self.assertFalse(pending.done())
        pending.cancel()
        with self.assertRaises(OutboundCommandDeadlineExceededError):
            facade.set({"count": 3}, deadline=monotonic() + 0.02)
        self.assertEqual(facade.snapshot(), {"count": 1})
        queue.shutdown(timeout=0)

    def test_snapshot_and_replay_clone_each_container_once(self) -> None:
        state = DefaultGlobalSettingsState("plugin", RecordingCommandSink())
        source = state.receive({"nested": {"items": [1, 2]}})
        facade = GlobalSettingsCoordinator(state)

        for read in (
            facade.snapshot,
            facade.new_replay_event,
            lambda: state.new_event(source),
        ):
            with self.subTest(read=read):
                with patch(
                    "mirabox_sdk.json_types._clone_json_dict",
                    wraps=json_types._clone_json_dict,
                ) as clone:
                    result = read()
                self.assertEqual(clone.call_count, 2)
                settings = (
                    result.settings if isinstance(result, DidReceiveGlobalSettingsEvent) else result
                )
                self.assertEqual(settings, {"nested": {"items": [1, 2]}})

    def test_public_snapshot_and_replay_use_isolated_native_containers(self) -> None:
        state = DefaultGlobalSettingsState("plugin", RecordingCommandSink())
        facade = GlobalSettingsCoordinator(state)
        facade.snapshot()["bypassed"] = True
        self.assertFalse(facade.loaded)
        self.assertIsNone(facade.new_replay_event())
        original: JsonObject = {"nested": {"items": [{"count": 1}]}}
        state.receive(original)
        first = facade.snapshot()
        second = facade.snapshot()
        replay = facade.new_replay_event()
        assert replay is not None

        for settings in (original, first, replay.settings):
            self.assertIs(type(settings), dict)
            nested = dict.__getitem__(settings, "nested")
            self.assertIs(type(nested), dict)
            items = dict.__getitem__(nested, "items")
            self.assertIs(type(items), list)
            item = list.__getitem__(items, 0)
            self.assertIs(type(item), dict)
            dict.__setitem__(item, "count", 2)
            list.append(items, {"count": 3})

        expected = {"nested": {"items": [{"count": 1}]}}
        self.assertEqual(second, expected)
        self.assertEqual(facade.snapshot(), expected)
        self.assertEqual(state.new_event().settings, expected)

    def test_replay_keeps_explicit_source_after_committed_state_changes(self) -> None:
        state = DefaultGlobalSettingsState("plugin", RecordingCommandSink())
        source = state.receive({"nested": {"items": [1]}})
        state.set({"nested": {"items": [2]}})

        self.assertEqual(state.new_event(source).settings, {"nested": {"items": [1]}})
        self.assertEqual(state.new_event().settings, {"nested": {"items": [2]}})

    def test_readers_see_committed_state_while_a_send_is_blocked(self) -> None:
        sender = RecordingCommandSink()
        state = DefaultGlobalSettingsState("plugin-uuid", sender)
        state.receive({"count": 1})
        sending = Event()
        release = Event()
        read = Event()
        snapshots: list[object] = []

        def block_send(_command: object) -> None:
            sending.set()
            if not release.wait(2):
                raise TimeoutError("test sender was not released")

        def read_state() -> None:
            snapshots.extend((state.settings, state.loaded, state.new_event().settings))
            read.set()

        sender.on_send = block_send
        writer = Thread(target=lambda: state.update(lambda draft: draft.update(count=2)))
        reader = Thread(target=read_state)
        writer.start()
        try:
            self.assertTrue(sending.wait(1))
            with self.assertRaises(GlobalSettingsBusyError):
                state.set_async({"count": 3})
            reader.start()
            self.assertTrue(read.wait(0.2), "snapshot readers waited for transport I/O")
            self.assertEqual(snapshots, [{"count": 1}, True, {"count": 1}])
        finally:
            release.set()
            writer.join(1)
            if reader.ident is not None:
                reader.join(1)
        self.assertFalse(writer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(state.settings, {"count": 2})

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
        self.assertIs(type(first.settings), dict)
        nested = dict.__getitem__(first.settings, "nested")
        self.assertIs(type(nested), dict)
        dict.__setitem__(nested, "count", 2)

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

    def test_async_writes_commit_before_completion_and_preserve_failures(self) -> None:
        codec = FunctionalJsonCodec[int](lambda raw: int(raw["count"]), lambda n: {"count": n})
        for method in ("set_async", "update_async", "set_typed_async"):
            for failure in (None, RuntimeError("send failed"), TimeoutError("transport timeout")):
                with self.subTest(method=method, failure=failure):
                    queue = OutboundCommandQueue(4)
                    state = DefaultGlobalSettingsState("plugin", queue)
                    state.receive({"count": 1})
                    facade = GlobalSettingsCoordinator(
                        state, metrics=_ActionContextMetricRecorder()
                    )
                    if method == "set_async":
                        future = facade.set_async({"count": 2})
                    elif method == "update_async":
                        future = facade.update_async(lambda draft: draft.update(count=2))
                    else:
                        future = facade.set_typed_async(2, codec)
                    observations: list[object] = []
                    future.add_done_callback(
                        lambda _result, facade=facade, observations=observations: (
                            observations.append(facade.snapshot())
                        )
                    )
                    self.assertFalse(future.done())
                    self.assertEqual(facade.snapshot(), {"count": 1})
                    self.assertEqual(facade.metrics().global_settings_updates, 0)
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0)
                    submission = queue.receive(timeout=0)
                    self.assertEqual(submission.command.settings, {"count": 2})
                    submission.completion._finish(error=failure)
                    expected = {"count": 2 if failure is None else 1}
                    self.assertTrue(future.done())
                    self.assertIs(future.exception(timeout=0), failure)
                    self.assertEqual(facade.snapshot(), expected)
                    self.assertEqual(observations, [expected])
                    self.assertEqual(facade.metrics().global_settings_updates, int(failure is None))
                    if failure is not None:
                        with self.assertRaises(type(failure)) as raised:
                            future.result(timeout=0)
                        self.assertIs(raised.exception, failure)
                    # Both success and failure free the single transaction slot.
                    facade.set_async({"count": 3})
                    queue.receive(timeout=0).completion._finish()
                    self.assertEqual(facade.snapshot(), {"count": 3})

    def test_busy_async_write_rejects_before_mutation_or_encoding(self) -> None:
        queue = OutboundCommandQueue(4)
        state = DefaultGlobalSettingsState("plugin", queue)
        first = state.set_async({"count": 1})
        update = Mock()
        encode = Mock(return_value={"count": 2})
        codec = FunctionalJsonCodec[int](lambda _raw: 0, encode)
        with self.assertRaises(GlobalSettingsBusyError):
            state.update_async(update)
        with self.assertRaises(GlobalSettingsBusyError):
            state.set_async({"count": 2})
        with self.assertRaises(GlobalSettingsBusyError):
            state.set_typed_async(2, codec)
        update.assert_not_called()
        encode.assert_not_called()
        self.assertFalse(state.loaded)
        self.assertEqual(state.settings, {})
        self.assertEqual(queue.metrics().enqueued, 1)
        queue.receive(timeout=0).completion._finish()
        first.result(timeout=0)
        self.assertTrue(state.loaded)
        self.assertEqual(state.settings, {"count": 1})

    def test_reentrant_async_write_is_busy_during_mutation(self) -> None:
        queue = OutboundCommandQueue(4)
        state = DefaultGlobalSettingsState("plugin", queue)
        state.receive({"count": 1})
        nested_update = Mock()

        def update(_draft: JsonObject) -> None:
            state.update_async(nested_update)

        with self.assertRaises(GlobalSettingsBusyError):
            state.update_async(update)
        nested_update.assert_not_called()
        self.assertEqual(queue.metrics().enqueued, 0)
        self.assertEqual(state.settings, {"count": 1})
        state.set_async({"count": 2})
        queue.receive(timeout=0).completion._finish()
        self.assertEqual(state.settings, {"count": 2})

    def test_reentrant_synchronous_write_preserves_existing_order(self) -> None:
        sender = RecordingCommandSink()
        state = DefaultGlobalSettingsState("plugin", sender)
        state.receive({"count": 1})

        def update(draft: JsonObject) -> None:
            state.set({"count": 2})
            draft["count"] = 3

        state.update(update)
        self.assertEqual(state.settings, {"count": 3})
        self.assertEqual(
            [command.settings for command in sender.commands], [{"count": 2}, {"count": 3}]
        )

    def test_async_draft_and_inputs_are_isolated_before_acceptance(self) -> None:
        queue = OutboundCommandQueue(4)
        state = DefaultGlobalSettingsState("plugin", queue)
        raw: JsonObject = {"nested": {"items": [1]}}
        future = state.set_async(raw)
        nested = raw["nested"]
        assert isinstance(nested, dict)
        nested["items"] = [2]
        queue.receive(timeout=0).completion._finish()
        future.result(timeout=0)
        self.assertEqual(state.settings, {"nested": {"items": [1]}})

    def test_async_preparation_and_acceptance_errors_leave_state_and_slot_intact(self) -> None:
        queue = OutboundCommandQueue(1)
        state = DefaultGlobalSettingsState("plugin", queue)
        update = Mock(side_effect=RuntimeError("update failed"))
        with self.assertRaisesRegex(RuntimeError, "update failed"):
            state.update_async(update)
        with self.assertRaises(ValueError):
            state.set_async({"invalid": object()})  # type: ignore[dict-item]
        with self.assertRaises(TypeError):
            state.update_async(None)  # type: ignore[arg-type]
        queue.send_async(LogMessageCommand("fill queue"))
        with self.assertRaises(OutboundQueueFullError):
            state.set_async({"count": 1})
        self.assertFalse(state.loaded)
        self.assertEqual(state.settings, {})
        queue.receive(timeout=0).completion._finish()
        state.set_async({"count": 2})
        queue.receive(timeout=0).completion._finish()
        self.assertEqual(state.settings, {"count": 2})

        not_ready = DefaultGlobalSettingsState("plugin", WriterReadyOutboundCommandSink(queue))
        for _ in range(2):
            with self.assertRaises(OutboundCommandBusNotReadyError):
                not_ready.set_async({"count": 1})
        self.assertFalse(not_ready.loaded)
        self.assertEqual(queue.metrics().current_depth, 0)

    def test_shutdown_reports_async_failure_without_committing(self) -> None:
        queue = OutboundCommandQueue(1)
        state = DefaultGlobalSettingsState("plugin", queue)
        future = state.set_async({"count": 1})
        self.assertFalse(queue.shutdown(timeout=0))
        with self.assertRaises(OutboundCommandBusClosedError):
            future.result(timeout=0)
        self.assertFalse(state.loaded)
        self.assertEqual(state.settings, {})
        with self.assertRaises(OutboundCommandBusClosedError):
            state.set_async({"count": 2})

    def test_sync_update_uses_latest_committed_state_after_async_success_or_rollback(self) -> None:
        for failure in (None, RuntimeError("send failed")):
            with self.subTest(failure=failure):
                queue = OutboundCommandQueue(4)
                state = DefaultGlobalSettingsState("plugin", queue)
                state.receive({"count": 1})
                state.update_async(lambda draft: draft.update(count=2))
                first = queue.receive(timeout=0)
                entered = Event()
                errors: list[Exception] = []

                def update(
                    state: DefaultGlobalSettingsState,
                    entered: Event,
                    errors: list[Exception],
                ) -> None:
                    entered.set()
                    try:
                        state.update(lambda draft: draft.update(count=int(draft["count"]) + 1))
                    except Exception as exc:
                        errors.append(exc)

                writer = Thread(target=update, args=(state, entered, errors))
                writer.start()
                try:
                    self.assertTrue(entered.wait(1))
                    with self.assertRaises(TimeoutError):
                        queue.receive(timeout=0.02)
                    first.completion._finish(error=failure)
                    second = queue.receive(timeout=1)
                    expected = {"count": 3 if failure is None else 2}
                    self.assertEqual(second.command.settings, expected)
                    second.completion._finish()
                    writer.join(1)
                    self.assertFalse(writer.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(state.settings, expected)
                finally:
                    queue.shutdown(timeout=0)
                    writer.join(1)

    def test_incoming_receive_is_serialized_after_async_transaction(self) -> None:
        queue = OutboundCommandQueue(4)
        state = DefaultGlobalSettingsState("plugin", queue)
        state.receive({"count": 1})
        future = state.set_async({"count": 2})
        receiving = Event()
        received = Event()

        def receive() -> None:
            receiving.set()
            state.receive({"count": 3})
            received.set()

        receiver = Thread(target=receive)
        receiver.start()
        try:
            self.assertTrue(receiving.wait(1))
            self.assertFalse(received.wait(0.02))
            self.assertEqual(state.settings, {"count": 1})
            queue.receive(timeout=0).completion._finish()
            future.result(timeout=0)
            self.assertTrue(received.wait(1))
            receiver.join(1)
            self.assertFalse(receiver.is_alive())
            self.assertEqual(state.settings, {"count": 3})
        finally:
            queue.shutdown(timeout=0)
            receiver.join(1)

    def test_immediate_async_completion_and_observer_can_submit_the_next_write(self) -> None:
        state = DefaultGlobalSettingsState("plugin", RecordingCommandSink())
        future = state.set_async({"count": 1})
        self.assertTrue(future.done())
        next_futures = []
        future.add_done_callback(lambda _result: next_futures.append(state.set_async({"count": 2})))
        next_futures[0].result(timeout=0)
        self.assertEqual(state.settings, {"count": 2})

    def test_completion_observer_can_submit_another_deferred_transaction(self) -> None:
        queue = OutboundCommandQueue(4)
        state = DefaultGlobalSettingsState("plugin", queue)
        future = state.set_async({"count": 1})
        next_futures = []
        future.add_done_callback(lambda _result: next_futures.append(state.set_async({"count": 2})))
        queue.receive(timeout=0).completion._finish()
        future.result(timeout=0)
        self.assertEqual(state.settings, {"count": 1})
        self.assertEqual(len(next_futures), 1)
        self.assertFalse(next_futures[0].done())
        queue.receive(timeout=0).completion._finish()
        next_futures[0].result(timeout=0)
        self.assertEqual(state.settings, {"count": 2})


if __name__ == "__main__":
    unittest.main()

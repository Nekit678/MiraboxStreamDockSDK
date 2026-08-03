from __future__ import annotations

import unittest
from collections.abc import Callable
from threading import Event, Lock, Thread, current_thread

from mirabox_sdk import (
    ActionRegistry,
    PluginLaunchArguments,
    RegisterPluginCommand,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    StreamDockEvent,
)
from mirabox_sdk._next.runtime.composition import (
    ComposedStreamDockRuntime,
    StreamDockRuntimeLifecycleError,
    create_stream_dock_runtime,
)
from mirabox_sdk._next.runtime.config import RuntimeDispatcherConfig
from mirabox_sdk._next.runtime.keyed_scheduler import KeyedSerialHandlerScheduler
from mirabox_sdk._next.runtime.metrics import (
    ActionContextMetrics,
    HandlerSchedulerMetrics,
    RuntimeEventPumpMetrics,
    RuntimeRouterMetrics,
    SessionCoordinatorMetrics,
)
from mirabox_sdk._next.runtime.models import (
    DispatchOutcome,
    DispatchResult,
    RuntimeLifecycleState,
    RuntimeSchedulerKind,
)
from mirabox_sdk._next.runtime.ports import RuntimeLifecycle
from mirabox_sdk._next.transport.session import Connected, Disconnected

from .fakes import (
    FakeDependencies,
    FakeInboundEventSource,
    FakeSessionEventSource,
    RecordingAction,
    RecordingActionFactory,
    RecordingCommandSink,
    will_appear_event,
)


def _launch_arguments() -> PluginLaunchArguments:
    return PluginLaunchArguments(
        port=12345,
        plugin_uuid="plugin-uuid",
        register_event="registerPlugin",
        info=RegistrationInfo(
            application=RegistrationApplicationInfo(
                language="en",
                platform="windows",
                platform_version="11",
                version="2.10",
            ),
            colors=RegistrationColors(),
            device_pixel_ratio=1.0,
            devices=(),
            plugin=RegistrationPluginInfo(uuid="plugin-uuid", version="0.1.0"),
        ),
    )


class _FakeBoundary:
    def __init__(
        self,
        *,
        events: FakeInboundEventSource | None = None,
        session_events: FakeSessionEventSource | None = None,
        block_run_until_close: bool = False,
        history: list[str] | None = None,
    ) -> None:
        self.events = events or FakeInboundEventSource()
        self.commands = RecordingCommandSink()
        self.session_events = session_events or FakeSessionEventSource()
        self.metrics_snapshot = object()
        self.history = history if history is not None else []
        self.run_started = Event()
        self.close_started = Event()
        self._run_released = Event()
        self._block_run_until_close = block_run_until_close
        self._lock = Lock()
        self.close_calls = 0
        self.close_threads: list[str] = []
        self.run_error: Exception | None = None
        self.close_error: Exception | None = None

    def run_forever(self) -> None:
        self.history.append("boundary.run")
        self.run_started.set()
        if self._block_run_until_close:
            self._run_released.wait(2)
        if self.run_error is not None:
            raise self.run_error

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
            self.close_threads.append(current_thread().name)
        self.history.append("boundary.close")
        self.close_started.set()
        self.events.close()
        self.session_events.close()
        self._run_released.set()
        if self.close_error is not None:
            raise self.close_error

    def metrics(self) -> object:
        return self.metrics_snapshot


class _FakeScheduler:
    def __init__(self, history: list[str]) -> None:
        self.history = history
        self.start_error: Exception | None = None
        self.drain_result = True
        self.stop_error: Exception | None = None

    def start(self) -> None:
        self.history.append("scheduler.start")
        if self.start_error is not None:
            raise self.start_error

    def submit(self, _event: StreamDockEvent) -> _ImmediateCompletion:
        return _ImmediateCompletion()

    def stop_accepting(self) -> None:
        self.history.append("scheduler.stop_accepting")

    def drain(self, *, timeout: float | None = None) -> bool:
        self.history.append("scheduler.drain")
        return self.drain_result

    def stop(self, *, timeout: float | None = None) -> bool:
        self.history.append("scheduler.stop")
        if self.stop_error is not None:
            raise self.stop_error
        return self.drain_result

    def metrics(self) -> HandlerSchedulerMetrics:
        return HandlerSchedulerMetrics()


class _ImmediateCompletion:
    def done(self) -> bool:
        return True

    def result(self, timeout: float | None = None) -> DispatchResult:
        return DispatchResult(DispatchOutcome.HANDLED)

    def add_done_callback(self, callback: Callable[[object], None]) -> None:
        callback(self)


class _FakePump:
    def __init__(self, history: list[str], name: str, *, session: bool = False) -> None:
        self.history = history
        self.name = name
        self.session = session
        self.failure: Exception | None = None
        self.start_error: Exception | None = None
        self.drain_result = True
        self.stop_error: Exception | None = None

    def start(self) -> None:
        self.history.append(f"{self.name}.start")
        if self.start_error is not None:
            raise self.start_error

    def request_stop(self) -> None:
        self.history.append(f"{self.name}.request_stop")

    def drain(self, *, timeout: float | None = None) -> bool:
        self.history.append(f"{self.name}.drain")
        return self.drain_result

    def stop(self, *, timeout: float | None = None) -> bool:
        self.history.append(f"{self.name}.stop")
        if self.stop_error is not None:
            raise self.stop_error
        return self.drain_result

    def is_worker_thread(self) -> bool:
        return False

    def metrics(self) -> SessionCoordinatorMetrics | RuntimeEventPumpMetrics:
        if self.session:
            return SessionCoordinatorMetrics()
        return RuntimeEventPumpMetrics()


class _FakeContexts:
    def __init__(self, history: list[str]) -> None:
        self.history = history
        self.actions: tuple[RecordingAction, ...] = ()

    def create(self, _event: object) -> None:
        return None

    def get(self, _context: str) -> None:
        return None

    def remove(self, _context: str, *, expected: object | None = None) -> None:
        return None

    def snapshot(self) -> tuple[RecordingAction, ...]:
        return self.actions

    def clear(self) -> tuple[RecordingAction, ...]:
        self.history.append("actions.clear")
        actions = self.actions
        self.actions = ()
        return actions


class _FakeRouter:
    def __init__(self, history: list[str]) -> None:
        self.contexts = _FakeContexts(history)

    def dispatch(self, _event: StreamDockEvent) -> DispatchResult:
        return DispatchResult(DispatchOutcome.HANDLED)

    def routing_metrics(self) -> RuntimeRouterMetrics:
        return RuntimeRouterMetrics()

    def action_metrics(self) -> ActionContextMetrics:
        return ActionContextMetrics()


def _composed_fakes() -> tuple[
    ComposedStreamDockRuntime,
    _FakeBoundary,
    _FakeScheduler,
    _FakePump,
    _FakePump,
    list[str],
]:
    history: list[str] = []
    boundary = _FakeBoundary(history=history)
    scheduler = _FakeScheduler(history)
    event_pump = _FakePump(history, "events")
    session_pump = _FakePump(history, "session", session=True)
    runtime = ComposedStreamDockRuntime(
        boundary=boundary,
        scheduler=scheduler,
        event_pump=event_pump,
        session_pump=session_pump,
        router=_FakeRouter(history),
        config=RuntimeDispatcherConfig(
            runtime_drain_timeout=0,
            worker_stop_timeout=0,
        ),
    )
    return runtime, boundary, scheduler, event_pump, session_pump, history


class ComposedRuntimeLifecycleTests(unittest.TestCase):
    def test_owns_startup_drain_and_cleanup_order(self) -> None:
        runtime, _, _, _, _, history = _composed_fakes()

        runtime.run_forever()

        self.assertEqual(
            history,
            [
                "scheduler.start",
                "events.start",
                "session.start",
                "boundary.run",
                "boundary.close",
                "events.drain",
                "scheduler.stop_accepting",
                "session.drain",
                "session.stop",
                "scheduler.drain",
                "scheduler.stop",
                "events.stop",
                "actions.clear",
            ],
        )
        self.assertIs(runtime.state, RuntimeLifecycleState.STOPPED)

    def test_startup_failure_preserves_primary_and_cleans_started_stages(self) -> None:
        cases = ("scheduler", "events", "session", "boundary")
        for stage in cases:
            with self.subTest(stage=stage):
                runtime, boundary, scheduler, event_pump, session_pump, history = _composed_fakes()
                failure = RuntimeError(f"{stage} failed")
                if stage == "scheduler":
                    scheduler.start_error = failure
                elif stage == "events":
                    event_pump.start_error = failure
                elif stage == "session":
                    session_pump.start_error = failure
                else:
                    boundary.run_error = failure

                with self.assertRaises(RuntimeError) as raised:
                    runtime.run_forever()

                self.assertIs(raised.exception, failure)
                self.assertIs(runtime.failure, failure)
                self.assertIs(runtime.state, RuntimeLifecycleState.FAILED)
                self.assertIn("boundary.close", history)
                self.assertIn("scheduler.stop", history)
                if stage in ("session", "boundary"):
                    self.assertIn("events.stop", history)
                if stage == "boundary":
                    self.assertIn("session.stop", history)

    def test_cleanup_failure_does_not_replace_primary_failure(self) -> None:
        runtime, boundary, scheduler, _, _, _ = _composed_fakes()
        primary = RuntimeError("primary")
        boundary.run_error = primary
        boundary.close_error = RuntimeError("close cleanup")
        scheduler.stop_error = RuntimeError("scheduler cleanup")

        with self.assertLogs("mirabox_sdk._next.runtime.composition", level="ERROR") as logs:
            with self.assertRaises(RuntimeError) as raised:
                runtime.run_forever()

        self.assertIs(raised.exception, primary)
        self.assertIs(runtime.failure, primary)
        output = "\n".join(logs.output)
        self.assertNotIn("close cleanup", output)
        self.assertNotIn("scheduler cleanup", output)

    def test_shutdown_timeout_still_finishes_other_cleanup_stages(self) -> None:
        runtime, _, scheduler, event_pump, session_pump, history = _composed_fakes()
        event_pump.drain_result = False
        session_pump.drain_result = False
        scheduler.drain_result = False

        with self.assertLogs("mirabox_sdk._next.runtime.composition", level="WARNING"):
            runtime.run_forever()

        self.assertIs(runtime.state, RuntimeLifecycleState.STOPPED)
        self.assertIn("events.request_stop", history)
        self.assertIn("session.stop", history)
        self.assertIn("scheduler.stop", history)
        self.assertIn("actions.clear", history)

    def test_close_before_run_is_idempotent_and_prevents_start(self) -> None:
        runtime, boundary, _, _, _, _ = _composed_fakes()

        runtime.close()
        runtime.close()

        self.assertEqual(boundary.close_calls, 1)
        self.assertIs(runtime.state, RuntimeLifecycleState.STOPPED)
        with self.assertRaises(StreamDockRuntimeLifecycleError):
            runtime.run_forever()

    def test_concurrent_close_invokes_boundary_once_and_waits_for_runtime(self) -> None:
        boundary = _FakeBoundary(block_run_until_close=True)
        factory = RecordingActionFactory(boundary.commands)
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=factory,
            config=RuntimeDispatcherConfig(
                event_poll_interval=0.005,
                session_poll_interval=0.005,
            ),
        )
        run_error: list[Exception] = []
        runner = Thread(
            target=lambda: _capture_error(runtime.run_forever, run_error),
            name="runtime-runner",
        )
        runner.start()
        self.assertTrue(boundary.run_started.wait(1))
        closers = [Thread(target=runtime.close) for _ in range(8)]

        for closer in closers:
            closer.start()
        for closer in closers:
            closer.join(1)
        runner.join(1)

        self.assertTrue(all(not closer.is_alive() for closer in closers))
        self.assertFalse(runner.is_alive())
        self.assertEqual(run_error, [])
        self.assertEqual(boundary.close_calls, 1)
        self.assertIs(runtime.state, RuntimeLifecycleState.STOPPED)


class RuntimeFactoryIntegrationTests(unittest.TestCase):
    def test_factory_selects_bounded_keyed_scheduler_from_config(self) -> None:
        boundary = _FakeBoundary()
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=RecordingActionFactory(boundary.commands),
            config=RuntimeDispatcherConfig(
                scheduler_kind=RuntimeSchedulerKind.KEYED_SERIAL,
                worker_count=3,
                scheduler_pending_limit=7,
            ),
        )

        self.assertIsInstance(runtime._scheduler, KeyedSerialHandlerScheduler)
        self.assertEqual(runtime.metrics().scheduler.current_pending, 0)

    def test_factory_injects_custom_scheduler_without_starting_components(self) -> None:
        boundary = _FakeBoundary()
        factory = RecordingActionFactory(boundary.commands)
        history: list[str] = []
        scheduler = _FakeScheduler(history)
        dispatchers: list[object] = []

        def scheduler_factory(dispatcher: object) -> _FakeScheduler:
            dispatchers.append(dispatcher)
            return scheduler

        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=factory,
            scheduler_factory=scheduler_factory,
        )

        self.assertIsInstance(runtime, ComposedStreamDockRuntime)
        self.assertEqual(len(dispatchers), 1)
        self.assertEqual(history, [])
        self.assertFalse(boundary.run_started.is_set())

    def test_full_fake_boundary_lifecycle_routes_and_aggregates_metrics(self) -> None:
        event = will_appear_event()
        events = FakeInboundEventSource((event,))
        events.close()
        session_events = FakeSessionEventSource((Connected(), Disconnected(1000, None)))
        session_events.close()
        boundary = _FakeBoundary(events=events, session_events=session_events)
        factory = RecordingActionFactory(boundary.commands)
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=factory,
            config=RuntimeDispatcherConfig(
                event_poll_interval=0.005,
                session_poll_interval=0.005,
            ),
        )

        runtime.run_forever()

        self.assertIsInstance(runtime, RuntimeLifecycle)
        self.assertEqual(events.acknowledged, [event])
        self.assertEqual(len(factory.instances), 1)
        self.assertEqual(factory.instances[0].events, [event, None])
        snapshot = runtime.metrics()
        self.assertEqual(snapshot.session.initialization_succeeded, 1)
        self.assertEqual(snapshot.session.disconnected, 1)
        self.assertEqual(snapshot.event_pump.events_acknowledged, 1)
        self.assertEqual(snapshot.routing.known_events_routed, 1)
        self.assertIs(snapshot.boundary, boundary.metrics_snapshot)
        for hidden_capability in ("events", "commands", "session_events", "boundary"):
            self.assertFalse(hasattr(runtime, hidden_capability))

    def test_action_registry_is_bound_to_application_dependencies(self) -> None:
        registry: ActionRegistry[FakeDependencies] = ActionRegistry()
        registry.register("com.example.runtime.action")(RecordingAction)
        events = FakeInboundEventSource((will_appear_event(),))
        events.close()
        session_events = FakeSessionEventSource((Connected(),))
        session_events.close()
        boundary = _FakeBoundary(events=events, session_events=session_events)
        dependencies = FakeDependencies(boundary.commands)
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=registry,
            action_dependencies=dependencies,
            config=RuntimeDispatcherConfig(
                event_poll_interval=0.005,
                session_poll_interval=0.005,
            ),
        )

        runtime.run_forever()

        self.assertEqual(events.acknowledged, events.received)
        self.assertEqual(runtime.metrics().actions.action_instances_created, 1)

    def test_factory_rejects_unbound_or_incompatible_action_dependencies(self) -> None:
        boundary = _FakeBoundary()
        registry: ActionRegistry[FakeDependencies] = ActionRegistry()

        with self.assertRaisesRegex(TypeError, "action_dependencies are required"):
            create_stream_dock_runtime(
                _launch_arguments(),
                boundary=boundary,
                action_factory=registry,
            )
        with self.assertRaisesRegex(TypeError, "four-argument action registry"):
            create_stream_dock_runtime(
                _launch_arguments(),
                boundary=boundary,
                action_factory=RecordingActionFactory(boundary.commands),
                action_dependencies=FakeDependencies(boundary.commands),
            )

    def test_close_from_action_callback_is_deferred_without_deadlock(self) -> None:
        returned = Event()
        runtime_holder: dict[str, ComposedStreamDockRuntime] = {}

        class ClosingAction(RecordingAction):
            def on_will_appear(self, event: object) -> None:
                self.events.append(event)
                runtime_holder["runtime"].close()
                returned.set()

        boundary = _FakeBoundary(
            events=FakeInboundEventSource((will_appear_event(),)),
            session_events=FakeSessionEventSource((Connected(),)),
            block_run_until_close=True,
        )
        factory = RecordingActionFactory(boundary.commands, ClosingAction)
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=factory,
            config=RuntimeDispatcherConfig(
                event_poll_interval=0.005,
                session_poll_interval=0.005,
            ),
        )
        runtime_holder["runtime"] = runtime
        run_error: list[Exception] = []
        runner = Thread(target=lambda: _capture_error(runtime.run_forever, run_error))
        runner.start()

        self.assertTrue(returned.wait(1))
        runner.join(1)

        self.assertFalse(runner.is_alive())
        self.assertEqual(run_error, [])
        self.assertEqual(boundary.close_calls, 1)
        self.assertEqual(boundary.close_threads, ["mirabox-next-runtime-close"])
        self.assertEqual(len(boundary.events.acknowledged), 1)

    def test_close_from_keyed_scheduler_callback_is_deferred_without_deadlock(self) -> None:
        returned = Event()
        runtime_holder: dict[str, ComposedStreamDockRuntime] = {}

        class ClosingAction(RecordingAction):
            def on_will_appear(self, event: object) -> None:
                self.events.append(event)
                runtime_holder["runtime"].close()
                returned.set()

        boundary = _FakeBoundary(
            events=FakeInboundEventSource((will_appear_event(),)),
            session_events=FakeSessionEventSource((Connected(),)),
            block_run_until_close=True,
        )
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=RecordingActionFactory(boundary.commands, ClosingAction),
            config=RuntimeDispatcherConfig(
                event_poll_interval=0.005,
                session_poll_interval=0.005,
                scheduler_kind=RuntimeSchedulerKind.KEYED_SERIAL,
                worker_count=2,
                scheduler_pending_limit=4,
            ),
        )
        runtime_holder["runtime"] = runtime
        run_error: list[Exception] = []
        runner = Thread(target=lambda: _capture_error(runtime.run_forever, run_error))
        runner.start()

        self.assertTrue(returned.wait(1))
        runner.join(1)

        self.assertFalse(runner.is_alive())
        self.assertEqual(run_error, [])
        self.assertEqual(boundary.close_calls, 1)
        self.assertEqual(boundary.close_threads, ["mirabox-next-runtime-close"])
        self.assertEqual(len(boundary.events.acknowledged), 1)

    def test_fatal_session_initialization_closes_boundary_and_is_rethrown(self) -> None:
        failure = RuntimeError("registration failed")
        boundary = _FakeBoundary(
            session_events=FakeSessionEventSource((Connected(),)),
            block_run_until_close=True,
        )
        boundary.commands.failures[RegisterPluginCommand] = failure
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=RecordingActionFactory(boundary.commands),
            config=RuntimeDispatcherConfig(
                event_poll_interval=0.005,
                session_poll_interval=0.005,
            ),
        )
        run_error: list[Exception] = []
        runner = Thread(target=lambda: _capture_error(runtime.run_forever, run_error))

        with self.assertLogs("mirabox_sdk._next.runtime", level="ERROR"):
            runner.start()
            runner.join(1)

        self.assertFalse(runner.is_alive())
        self.assertEqual(run_error, [failure])
        self.assertIs(runtime.failure, failure)
        self.assertIs(runtime.state, RuntimeLifecycleState.FAILED)
        self.assertEqual(boundary.close_calls, 1)

    def test_active_callback_timeout_is_observable_without_forced_interruption(self) -> None:
        callback_started = Event()
        release_callback = Event()

        class BlockingAction(RecordingAction):
            def on_will_appear(self, event: object) -> None:
                callback_started.set()
                release_callback.wait(1)
                self.events.append(event)

        event = will_appear_event()
        boundary = _FakeBoundary(
            events=FakeInboundEventSource((event,)),
            session_events=FakeSessionEventSource((Connected(),)),
            block_run_until_close=True,
        )
        runtime = create_stream_dock_runtime(
            _launch_arguments(),
            boundary=boundary,
            action_factory=RecordingActionFactory(boundary.commands, BlockingAction),
            config=RuntimeDispatcherConfig(
                event_poll_interval=0.005,
                session_poll_interval=0.005,
                runtime_drain_timeout=0,
                worker_stop_timeout=0,
                callback_timeout=0,
            ),
        )
        run_error: list[Exception] = []
        runner = Thread(target=lambda: _capture_error(runtime.run_forever, run_error))
        runner.start()
        self.assertTrue(callback_started.wait(1))

        with self.assertLogs("mirabox_sdk._next.runtime", level="WARNING"):
            runtime.close()

        runner.join(1)
        self.assertFalse(runner.is_alive())
        self.assertEqual(run_error, [])
        self.assertEqual(runtime.metrics().scheduler.callback_timeouts, 1)
        self.assertEqual(boundary.events.acknowledged, [])

        release_callback.set()
        self.assertTrue(boundary.events.wait_for_acknowledged(1, timeout=1))
        self.assertEqual(boundary.events.acknowledged, [event])


def _capture_error(action: Callable[[], None], errors: list[Exception]) -> None:
    try:
        action()
    except Exception as exc:
        errors.append(exc)


if __name__ == "__main__":
    unittest.main()

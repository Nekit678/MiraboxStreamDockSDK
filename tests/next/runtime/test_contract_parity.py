"""Behavioral contracts shared by the legacy and experimental runtimes."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import dataclass

from mirabox_sdk import (
    Action,
    ActionRegistry,
    CommandFuture,
    Controller,
    Coordinates,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    GetGlobalSettingsCommand,
    JsonObject,
    KeyDownEvent,
    PluginLaunchArguments,
    RegisterPluginCommand,
    RegistrationApplicationInfo,
    RegistrationColors,
    RegistrationInfo,
    RegistrationPluginInfo,
    SetTitleCommand,
    StreamDockCommand,
    StreamDockEvent,
    StreamDockListener,
    StreamDockPlugin,
    StreamDockSender,
    SystemDidWakeUpEvent,
    TitleAlignment,
    TitleParameters,
    TitleParametersDidChangeEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from mirabox_sdk._next.runtime.adapters import LegacyActionFactoryAdapter
from mirabox_sdk._next.runtime.global_settings import DefaultGlobalSettingsState
from mirabox_sdk._next.runtime.router import RuntimeEventRouter
from mirabox_sdk._next.runtime.session import SessionCoordinator
from mirabox_sdk._next.transport.session import Connected

_ACTION_UUID = "com.example.runtime.parity"
_FAILURE_SECRET = "runtime-parity-secret-must-not-appear"


@dataclass(frozen=True, slots=True)
class _Dependencies:
    stream_dock: StreamDockSender


class _RecordingConnection:
    """Minimal connection/sender accepted by both runtime compositions."""

    def __init__(self) -> None:
        self.commands: list[StreamDockCommand] = []
        self.listener: StreamDockListener | None = None
        self.close_calls = 0

    def set_listener(self, listener: StreamDockListener) -> None:
        self.listener = listener

    def run_forever(self) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1

    def send(self, command: StreamDockCommand) -> None:
        self.commands.append(command)

    def send_async(self, command: StreamDockCommand) -> CommandFuture:
        self.send(command)
        completion = CommandFuture()
        completion._finish()
        return completion


class _ParityAction(Action[JsonObject, _Dependencies]):
    instances: dict[str, _ParityAction] = {}

    def __init__(
        self,
        action: str,
        context: str,
        settings: JsonObject,
        dependencies: _Dependencies,
    ) -> None:
        super().__init__(action, context, settings, dependencies)
        self.observations: list[tuple[object, ...]] = []
        type(self).instances[context] = self

    def on_will_appear(self, _event: WillAppearEvent) -> None:
        self.observations.append(("will_appear", deepcopy(self.settings)))
        if self.context == "appearance-fails":
            raise RuntimeError(_FAILURE_SECRET)

    def on_key_down(self, _event: KeyDownEvent) -> None:
        self.observations.append(("key_down", deepcopy(self.settings)))
        if self.context == "callback-fails":
            raise RuntimeError(_FAILURE_SECRET)
        self.set_title("handled")

    def on_did_receive_settings(self, _event: DidReceiveSettingsEvent) -> None:
        self.observations.append(("settings", deepcopy(self.settings)))

    def on_title_parameters_did_change(
        self,
        _event: TitleParametersDidChangeEvent,
    ) -> None:
        self.observations.append(("title", self.title, self.title_parameters))

    def on_did_receive_global_settings(self, event: DidReceiveGlobalSettingsEvent) -> None:
        nested = event.settings["nested"]
        assert isinstance(nested, dict)
        if self.context == "mutating":
            nested["value"] = 99
        self.observations.append(("global_settings", deepcopy(event.settings)))

    def on_system_did_wake_up(self, _event: SystemDidWakeUpEvent) -> None:
        self.observations.append(("system_wake",))
        if self.context == "broadcast-fails":
            raise RuntimeError(_FAILURE_SECRET)

    def on_will_disappear(self, event: WillDisappearEvent | None = None) -> None:
        self.observations.append(("will_disappear", event is None))


class _PluginHooks:
    def __init__(self) -> None:
        self.events: list[UnknownStreamDockEvent] = []

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        self.events.append(event)


class _LegacyPlugin(StreamDockPlugin[_Dependencies]):
    def __init__(
        self,
        launch_arguments: PluginLaunchArguments,
        *,
        stream_dock: _RecordingConnection,
        action_registry: ActionRegistry[_Dependencies],
        action_dependencies: _Dependencies,
        hooks: _PluginHooks,
    ) -> None:
        self._hooks = hooks
        super().__init__(
            launch_arguments,
            stream_dock=stream_dock,
            action_registry=action_registry,
            action_dependencies=action_dependencies,
        )

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        self._hooks.on_unhandled_event(event)


class _RuntimeHarness:
    name: str
    error_logger: str
    broadcast_error_logger: str

    def connect(self) -> None:
        raise NotImplementedError

    def dispatch(self, event: StreamDockEvent) -> None:
        raise NotImplementedError

    def active_contexts(self) -> set[str]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def commands(self) -> list[StreamDockCommand]:
        raise NotImplementedError

    @property
    def hooks(self) -> _PluginHooks:
        raise NotImplementedError

    def action(self, context: str) -> _ParityAction:
        return _ParityAction.instances[context]


class _LegacyHarness(_RuntimeHarness):
    name = "legacy"
    error_logger = "mirabox_sdk.plugin"
    broadcast_error_logger = "mirabox_sdk.stores"

    def __init__(self) -> None:
        _ParityAction.instances = {}
        self._connection = _RecordingConnection()
        self._hooks = _PluginHooks()
        registry: ActionRegistry[_Dependencies] = ActionRegistry()
        registry.register(_ACTION_UUID)(_ParityAction)
        self._runtime = _LegacyPlugin(
            _launch_arguments(),
            stream_dock=self._connection,
            action_registry=registry,
            action_dependencies=_Dependencies(self._connection),
            hooks=self._hooks,
        )

    def connect(self) -> None:
        self._runtime.on_stream_dock_connected()

    def dispatch(self, event: StreamDockEvent) -> None:
        self._runtime.on_stream_dock_event(event)

    def active_contexts(self) -> set[str]:
        return set(self._runtime.actions)

    def close(self) -> None:
        self._runtime.stop()

    @property
    def commands(self) -> list[StreamDockCommand]:
        return self._connection.commands

    @property
    def hooks(self) -> _PluginHooks:
        return self._hooks


class _ExperimentalHarness(_RuntimeHarness):
    name = "experimental"
    error_logger = "mirabox_sdk._next.runtime.actions"
    broadcast_error_logger = "mirabox_sdk._next.runtime.actions"

    def __init__(self) -> None:
        _ParityAction.instances = {}
        self._sender = _RecordingConnection()
        self._hooks = _PluginHooks()
        registry: ActionRegistry[_Dependencies] = ActionRegistry()
        registry.register(_ACTION_UUID)(_ParityAction)
        dependencies = _Dependencies(self._sender)
        state = DefaultGlobalSettingsState("plugin-uuid", self._sender)
        self._router = RuntimeEventRouter(
            LegacyActionFactoryAdapter(registry, dependencies),
            state,
            plugin_hooks=self._hooks,
        )
        self._session = SessionCoordinator(
            self._sender,
            register_event="registerPlugin",
            plugin_uuid="plugin-uuid",
        )

    def connect(self) -> None:
        self._session.handle(Connected())

    def dispatch(self, event: StreamDockEvent) -> None:
        self._router.dispatch(event)

    def active_contexts(self) -> set[str]:
        return {action.context for action in self._router.contexts.snapshot()}

    def close(self) -> None:
        for action in self._router.contexts.clear():
            action.on_will_disappear()

    @property
    def commands(self) -> list[StreamDockCommand]:
        return self._sender.commands

    @property
    def hooks(self) -> _PluginHooks:
        return self._hooks


class RuntimeBehavioralContractTests(unittest.TestCase):
    implementations = (_LegacyHarness, _ExperimentalHarness)

    def test_initialization_precedes_callbacks_and_callback_commands_are_sent(self) -> None:
        for harness_type in self.implementations:
            with self.subTest(runtime=harness_type.name):
                harness = harness_type()
                harness.connect()
                harness.dispatch(_will_appear())
                harness.dispatch(_key_down())

                self.assertEqual(
                    [type(command) for command in harness.commands],
                    [RegisterPluginCommand, GetGlobalSettingsCommand, SetTitleCommand],
                )
                self.assertEqual(
                    [item[0] for item in harness.action("button").observations],
                    ["will_appear", "key_down"],
                )
                harness.close()

    def test_action_lifecycle_and_state_before_callback_match(self) -> None:
        settings = {"count": 2}
        title = _title_changed()
        for harness_type in self.implementations:
            with self.subTest(runtime=harness_type.name):
                harness = harness_type()
                harness.dispatch(_will_appear())
                action = harness.action("button")

                harness.dispatch(_did_receive_settings(settings))
                harness.dispatch(title)
                harness.dispatch(_key_down(context="missing"))
                harness.dispatch(_will_disappear())

                self.assertEqual(
                    action.observations[1:],
                    [
                        ("settings", {"count": 2}),
                        ("title", title.title, title.title_parameters),
                        ("will_disappear", False),
                    ],
                )
                self.assertEqual(harness.active_contexts(), set())
                harness.close()

    def test_appearance_and_callback_failures_are_isolated(self) -> None:
        for harness_type in self.implementations:
            with self.subTest(runtime=harness_type.name):
                harness = harness_type()
                with self.assertLogs(harness.error_logger, level="ERROR"):
                    harness.dispatch(_will_appear(context="appearance-fails"))
                failed_appearance = harness.action("appearance-fails")
                self.assertEqual(harness.active_contexts(), set())
                self.assertEqual(failed_appearance.observations[-1], ("will_disappear", True))

                harness.dispatch(_will_appear(context="callback-fails"))
                with self.assertLogs(harness.error_logger, level="ERROR"):
                    harness.dispatch(_key_down(context="callback-fails"))
                harness.dispatch(_did_receive_settings({"count": 3}, context="callback-fails"))

                callback_action = harness.action("callback-fails")
                self.assertEqual(callback_action.observations[-1], ("settings", {"count": 3}))
                harness.close()

    def test_broadcast_failure_does_not_block_other_contexts(self) -> None:
        for harness_type in self.implementations:
            with self.subTest(runtime=harness_type.name):
                harness = harness_type()
                harness.dispatch(_will_appear(context="broadcast-fails"))
                harness.dispatch(_will_appear(context="healthy"))

                with self.assertLogs(harness.broadcast_error_logger, level="ERROR"):
                    harness.dispatch(SystemDidWakeUpEvent())

                self.assertIn(("system_wake",), harness.action("broadcast-fails").observations)
                self.assertIn(("system_wake",), harness.action("healthy").observations)
                harness.close()

    def test_callback_failure_logs_redact_exception_messages(self) -> None:
        for harness_type in self.implementations:
            with self.subTest(runtime=harness_type.name):
                harness = harness_type()
                harness.dispatch(_will_appear(context="callback-fails"))

                with self.assertLogs(harness.error_logger, level="ERROR") as logs:
                    harness.dispatch(_key_down(context="callback-fails"))

                output = "\n".join(logs.output)
                self.assertNotIn(_FAILURE_SECRET, output)
                self.assertNotIn("payload", output)
                self.assertIn("exception_type=RuntimeError", output)
                harness.close()

    def test_global_settings_broadcast_and_late_replay_are_isolated(self) -> None:
        for harness_type in self.implementations:
            with self.subTest(runtime=harness_type.name):
                harness = harness_type()
                harness.dispatch(_will_appear(context="mutating"))
                harness.dispatch(_will_appear(context="healthy"))
                harness.dispatch(DidReceiveGlobalSettingsEvent(settings={"nested": {"value": 1}}))
                harness.dispatch(_will_appear(context="late"))

                self.assertEqual(
                    harness.action("mutating").observations[-1],
                    ("global_settings", {"nested": {"value": 99}}),
                )
                self.assertEqual(
                    harness.action("healthy").observations[-1],
                    ("global_settings", {"nested": {"value": 1}}),
                )
                self.assertEqual(
                    harness.action("late").observations[-1],
                    ("global_settings", {"nested": {"value": 1}}),
                )
                harness.close()

    def test_unknown_event_and_shutdown_cleanup_are_delivered_once(self) -> None:
        event = UnknownStreamDockEvent(
            event="futureEvent",
            data={"event": "futureEvent", "payload": {"secret": "not-logged"}},
        )
        for harness_type in self.implementations:
            with self.subTest(runtime=harness_type.name):
                harness = harness_type()
                harness.dispatch(_will_appear())
                action = harness.action("button")

                harness.dispatch(event)
                harness.close()
                harness.close()

                self.assertEqual(harness.hooks.events, [event])
                self.assertEqual(action.observations[-1], ("will_disappear", True))
                self.assertEqual(
                    sum(item[0] == "will_disappear" for item in action.observations),
                    1,
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
                version="2.10.179.426",
            ),
            colors=RegistrationColors(),
            device_pixel_ratio=1.0,
            devices=(),
            plugin=RegistrationPluginInfo(uuid="plugin-uuid", version="0.1.0"),
        ),
    )


def _will_appear(*, context: str = "button") -> WillAppearEvent:
    return WillAppearEvent(
        action=_ACTION_UUID,
        context=context,
        device="device-uuid",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def _will_disappear(*, context: str = "button") -> WillDisappearEvent:
    return WillDisappearEvent(
        action=_ACTION_UUID,
        context=context,
        device="device-uuid",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def _key_down(*, context: str = "button") -> KeyDownEvent:
    return KeyDownEvent(
        action=_ACTION_UUID,
        context=context,
        device="device-uuid",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def _did_receive_settings(
    settings: JsonObject,
    *,
    context: str = "button",
) -> DidReceiveSettingsEvent:
    return DidReceiveSettingsEvent(
        action=_ACTION_UUID,
        context=context,
        device="device-uuid",
        settings=settings,
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def _title_changed(*, context: str = "button") -> TitleParametersDidChangeEvent:
    return TitleParametersDidChangeEvent(
        action=_ACTION_UUID,
        context=context,
        device="device-uuid",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        title="Updated",
        title_parameters=TitleParameters(
            font_family="Arial",
            font_size=14,
            font_style="Bold",
            font_underline=False,
            show_title=True,
            alignment=TitleAlignment.BOTTOM,
            color="#ffffffff",
        ),
        controller=Controller.KEYPAD,
    )


if __name__ == "__main__":
    unittest.main()

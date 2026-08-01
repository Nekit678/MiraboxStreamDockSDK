from __future__ import annotations

import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from mirabox_sdk import Action, StreamDockEvent, StreamDockEventType, UnknownStreamDockEvent
from mirabox_sdk._next.runtime.routes import (
    RUNTIME_EVENT_REGISTRY,
    DispatchOrdering,
    RuntimeEventRegistry,
    RuntimeEventRegistryError,
    RuntimeEventRoute,
    RuntimeEventRouteMismatchError,
    RuntimeEventScope,
    RuntimeTransition,
)
from mirabox_sdk.events import ActionEvent
from mirabox_sdk.parser import EVENT_CODEC_REGISTRY

PROJECT_ROOT = Path(__file__).parents[3]


class _WrongKeyDownEvent(StreamDockEvent):
    event = StreamDockEventType.KEY_DOWN


class _DuplicateKeyDownEvent(StreamDockEvent):
    event = StreamDockEventType.KEY_DOWN


class RuntimeEventRouteTests(unittest.TestCase):
    def test_registry_covers_every_known_event_with_runtime_only_policy(self) -> None:
        expected = {
            "willAppear": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_will_appear",
                RuntimeTransition.CREATE_ACTION,
            ),
            "willDisappear": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_will_disappear",
                RuntimeTransition.REMOVE_ACTION,
            ),
            "didReceiveSettings": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_did_receive_settings",
                RuntimeTransition.UPDATE_ACTION_SETTINGS,
            ),
            "titleParametersDidChange": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_title_parameters_did_change",
                RuntimeTransition.UPDATE_ACTION_TITLE,
            ),
            "keyDown": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_key_down",
                RuntimeTransition.NONE,
            ),
            "keyUp": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_key_up",
                RuntimeTransition.NONE,
            ),
            "touchTap": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_touch_tap",
                RuntimeTransition.NONE,
            ),
            "dialDown": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_dial_down",
                RuntimeTransition.NONE,
            ),
            "dialUp": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_dial_up",
                RuntimeTransition.NONE,
            ),
            "dialRotate": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_dial_rotate",
                RuntimeTransition.NONE,
            ),
            "propertyInspectorDidAppear": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_property_inspector_did_appear",
                RuntimeTransition.NONE,
            ),
            "propertyInspectorDidDisappear": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_property_inspector_did_disappear",
                RuntimeTransition.NONE,
            ),
            "sendToPlugin": (
                RuntimeEventScope.ACTION,
                DispatchOrdering.CONTEXT,
                "on_send_to_plugin",
                RuntimeTransition.NONE,
            ),
            "didReceiveGlobalSettings": (
                RuntimeEventScope.BROADCAST,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_did_receive_global_settings",
                RuntimeTransition.UPDATE_GLOBAL_SETTINGS,
            ),
            "deviceDidConnect": (
                RuntimeEventScope.BROADCAST,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_device_did_connect",
                RuntimeTransition.NONE,
            ),
            "deviceDidDisconnect": (
                RuntimeEventScope.BROADCAST,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_device_did_disconnect",
                RuntimeTransition.NONE,
            ),
            "applicationDidLaunch": (
                RuntimeEventScope.BROADCAST,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_application_did_launch",
                RuntimeTransition.NONE,
            ),
            "applicationDidTerminate": (
                RuntimeEventScope.BROADCAST,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_application_did_terminate",
                RuntimeTransition.NONE,
            ),
            "systemDidWakeUp": (
                RuntimeEventScope.BROADCAST,
                DispatchOrdering.GLOBAL_BARRIER,
                "on_system_did_wake_up",
                RuntimeTransition.NONE,
            ),
        }

        self.assertEqual(set(expected), {event.value for event in StreamDockEventType})
        self.assertEqual(len(RUNTIME_EVENT_REGISTRY), len(expected))
        for route in RUNTIME_EVENT_REGISTRY.routes:
            with self.subTest(event=route.wire_name):
                self.assertEqual(
                    (route.scope, route.ordering, route.callback, route.transition),
                    expected[route.wire_name],
                )
                self.assertIs(RUNTIME_EVENT_REGISTRY[route.event_class], route)
                self.assertIs(RUNTIME_EVENT_REGISTRY.get_by_wire_name(route.wire_name), route)
                self.assertTrue(callable(getattr(Action, route.callback)))
                self.assertEqual(
                    route.scope is RuntimeEventScope.ACTION,
                    issubclass(route.event_class, ActionEvent),
                )

    def test_route_and_registry_are_immutable(self) -> None:
        route = RUNTIME_EVENT_REGISTRY.routes[0]

        with self.assertRaises(FrozenInstanceError):
            route.callback = "replacement"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            RUNTIME_EVENT_REGISTRY._routes = ()  # type: ignore[misc]
        with self.assertRaises(TypeError):
            RUNTIME_EVENT_REGISTRY[route.event_class] = route  # type: ignore[index]

    def test_registry_rejects_duplicate_class_and_wire_name(self) -> None:
        routes = RUNTIME_EVENT_REGISTRY.routes
        with self.assertRaisesRegex(RuntimeEventRegistryError, "duplicate runtime route"):
            RuntimeEventRegistry((*routes, routes[0]))

        duplicate_wire = RuntimeEventRoute(
            _DuplicateKeyDownEvent,
            RuntimeEventScope.BROADCAST,
            DispatchOrdering.CONTEXT,
            "on_key_down",
        )
        with self.assertRaisesRegex(RuntimeEventRegistryError, "duplicate runtime route"):
            RuntimeEventRegistry((*routes, duplicate_wire))

    def test_registry_rejects_missing_route(self) -> None:
        missing_wire_name = RUNTIME_EVENT_REGISTRY.routes[-1].wire_name

        with self.assertRaisesRegex(
            RuntimeEventRegistryError,
            rf"missing=\['{missing_wire_name}'\]",
        ):
            RuntimeEventRegistry(RUNTIME_EVENT_REGISTRY.routes[:-1])

    def test_registry_validates_action_scope_and_callback_existence(self) -> None:
        action_route = next(
            route
            for route in RUNTIME_EVENT_REGISTRY.routes
            if route.scope is RuntimeEventScope.ACTION
        )
        invalid_scope = replace(action_route, scope=RuntimeEventScope.BROADCAST)
        routes = tuple(
            invalid_scope if route is action_route else route
            for route in RUNTIME_EVENT_REGISTRY.routes
        )
        with self.assertRaisesRegex(RuntimeEventRegistryError, "invalid scope"):
            RuntimeEventRegistry(routes)

        missing_callback = replace(action_route, callback="on_missing_callback")
        routes = tuple(
            missing_callback if route is action_route else route
            for route in RUNTIME_EVENT_REGISTRY.routes
        )
        with self.assertRaisesRegex(RuntimeEventRegistryError, "missing Action callback"):
            RuntimeEventRegistry(routes)

    def test_unknown_event_bypasses_registry_and_wrong_known_dto_is_rejected(self) -> None:
        unknown = UnknownStreamDockEvent(event="futureEvent", data={"event": "futureEvent"})

        self.assertIsNone(RUNTIME_EVENT_REGISTRY.route_for(unknown))
        with self.assertRaises(RuntimeEventRouteMismatchError) as raised:
            RUNTIME_EVENT_REGISTRY.route_for(_WrongKeyDownEvent())
        self.assertEqual(raised.exception.wire_name, StreamDockEventType.KEY_DOWN.value)

    def test_parser_registry_contains_only_codec_metadata(self) -> None:
        self.assertEqual(
            set(EVENT_CODEC_REGISTRY),
            {event_type.value for event_type in StreamDockEventType},
        )
        for codec in EVENT_CODEC_REGISTRY.values():
            self.assertFalse(hasattr(codec, "scope"))
            self.assertFalse(hasattr(codec, "callback"))
            self.assertFalse(hasattr(codec, "runtime_handler"))
        with self.assertRaises(TypeError):
            EVENT_CODEC_REGISTRY["futureEvent"] = next(  # type: ignore[index]
                iter(EVENT_CODEC_REGISTRY.values())
            )

    def test_importing_parser_does_not_load_runtime_routing_policy(self) -> None:
        script = (
            "import sys\n"
            "import mirabox_sdk.parser\n"
            "if 'mirabox_sdk._next.runtime.routes' in sys.modules:\n"
            "    raise AssertionError('parser imported runtime routes')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

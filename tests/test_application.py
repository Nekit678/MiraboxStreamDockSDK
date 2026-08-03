"""Tests for the stable Stream Dock runtime application API."""

from __future__ import annotations

import sys
import unittest
from concurrent.futures import InvalidStateError
from threading import enumerate as enumerate_threads

import mirabox_sdk
import mirabox_sdk.runtime as runtime
from mirabox_sdk import (
    CommandFuture,
    OutboundCommandBusClosedError,
    OutboundQueueFullError,
    RuntimeDispatcherConfig,
    RuntimeSchedulerKind,
)
from mirabox_sdk._next.messaging.models import CommandFuture as BoundaryCommandFuture
from mirabox_sdk._next.messaging.outbound import (
    OutboundCommandQueueClosedError,
)
from mirabox_sdk._next.messaging.outbound import (
    OutboundQueueFullError as BoundaryQueueFullError,
)


class StableRuntimeApiTests(unittest.TestCase):
    def test_runtime_package_exports_only_stable_application_capabilities(self) -> None:
        expected = {
            "ActionContextMetrics",
            "ActionFactory",
            "HandlerSchedulerMetrics",
            "InboundOverflowPolicy",
            "PluginHooks",
            "RuntimeDispatcherConfig",
            "RuntimeEventPumpMetrics",
            "RuntimeLifecycle",
            "RuntimeRouterMetrics",
            "RuntimeSchedulerKind",
            "SessionCoordinatorMetrics",
            "StreamDockApplication",
            "StreamDockBoundaryMetrics",
            "StreamDockQueueConfig",
            "StreamDockRuntime",
            "StreamDockRuntimeLifecycleError",
            "StreamDockRuntimeMetrics",
            "StreamDockSender",
            "StreamDockShutdownConfig",
            "create_stream_dock_application",
        }

        self.assertEqual(set(runtime.__all__), expected)
        self.assertTrue(all(hasattr(runtime, name) for name in expected))
        self.assertTrue(expected.issubset(mirabox_sdk.__all__))

    def test_legacy_and_experimental_runtime_surfaces_are_not_public(self) -> None:
        removed = {
            "EVENT_REGISTRY",
            "StreamDockConnection",
            "StreamDockListener",
            "StreamDockPlugin",
            "WebSocketStreamDockConnection",
            "create_experimental_stream_dock_application",
            "create_experimental_stream_dock_connection",
        }

        self.assertFalse(removed.intersection(mirabox_sdk.__all__))
        self.assertTrue(all(not hasattr(mirabox_sdk, name) for name in removed))
        self.assertNotIn("mirabox_sdk.experimental", sys.modules)

    def test_importing_runtime_does_not_start_workers(self) -> None:
        before = {thread.ident for thread in enumerate_threads()}

        __import__("mirabox_sdk.runtime")

        self.assertEqual({thread.ident for thread in enumerate_threads()}, before)

    def test_keyed_serial_scheduler_is_the_bounded_default(self) -> None:
        config = RuntimeDispatcherConfig()

        self.assertIs(config.scheduler_kind, RuntimeSchedulerKind.KEYED_SERIAL)
        self.assertEqual(config.worker_count, 4)
        self.assertEqual(config.scheduler_pending_limit, 64)


class CanonicalCommandFutureTests(unittest.TestCase):
    def test_boundary_and_action_helpers_share_one_completion_type(self) -> None:
        self.assertIs(BoundaryCommandFuture, CommandFuture)

        completion = BoundaryCommandFuture()
        shared = completion._share()
        completion._finish()

        self.assertIsInstance(shared, CommandFuture)
        self.assertTrue(shared.wait(timeout=0))
        self.assertIsNone(shared.result(timeout=0))
        with self.assertRaises(InvalidStateError):
            completion._finish()

    def test_boundary_submission_errors_are_the_public_errors(self) -> None:
        self.assertIs(BoundaryQueueFullError, OutboundQueueFullError)
        self.assertIs(OutboundCommandQueueClosedError, OutboundCommandBusClosedError)


if __name__ == "__main__":
    unittest.main()

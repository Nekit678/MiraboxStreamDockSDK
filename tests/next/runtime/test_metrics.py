from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from mirabox_sdk._next.runtime.metrics import (
    ActionContextMetrics,
    HandlerSchedulerMetrics,
    RuntimeEventPumpMetrics,
    RuntimeRouterMetrics,
    SessionCoordinatorMetrics,
)


class RuntimeMetricModelTests(unittest.TestCase):
    def test_component_snapshots_are_zero_initialized_and_frozen(self) -> None:
        snapshots = (
            SessionCoordinatorMetrics(),
            RuntimeEventPumpMetrics(),
            HandlerSchedulerMetrics(),
            RuntimeRouterMetrics(),
            ActionContextMetrics(),
        )

        for snapshot in snapshots:
            with self.subTest(snapshot=type(snapshot).__name__):
                self.assertFalse(hasattr(snapshot, "__dict__"))
                first_field = next(iter(snapshot.__dataclass_fields__))
                with self.assertRaises(FrozenInstanceError):
                    setattr(snapshot, first_field, 1)

    def test_snapshot_values_are_retained_without_shared_mutable_state(self) -> None:
        first = RuntimeEventPumpMetrics(events_received=3, current_owned=1, peak_owned=2)
        second = RuntimeEventPumpMetrics()

        self.assertEqual(first.events_received, 3)
        self.assertEqual(first.current_owned, 1)
        self.assertEqual(first.peak_owned, 2)
        self.assertEqual(second.events_received, 0)


if __name__ == "__main__":
    unittest.main()

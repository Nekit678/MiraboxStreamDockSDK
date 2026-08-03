from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from mirabox_sdk._next.runtime.config import RuntimeDispatcherConfig
from mirabox_sdk._next.runtime.models import RuntimeSchedulerKind


class RuntimeDispatcherConfigTests(unittest.TestCase):
    def test_defaults_describe_bounded_keyed_serial_dispatch(self) -> None:
        config = RuntimeDispatcherConfig()

        self.assertEqual(config.session_poll_interval, 0.05)
        self.assertEqual(config.event_poll_interval, 0.05)
        self.assertIs(config.scheduler_kind, RuntimeSchedulerKind.KEYED_SERIAL)
        self.assertEqual(config.worker_count, 4)
        self.assertEqual(config.scheduler_pending_limit, 64)
        self.assertEqual(config.runtime_drain_timeout, 5.0)
        self.assertEqual(config.worker_stop_timeout, 5.0)
        self.assertIsNone(config.callback_timeout)

        with self.assertRaises(FrozenInstanceError):
            config.worker_count = 2  # type: ignore[misc]

    def test_intervals_require_positive_finite_numbers(self) -> None:
        for field_name in ("session_poll_interval", "event_poll_interval"):
            for invalid in (0, -1, True, float("inf"), float("nan"), "0.1"):
                with (
                    self.subTest(field_name=field_name, invalid=invalid),
                    self.assertRaisesRegex(
                        ValueError,
                        f"^{field_name} must be a positive finite number$",
                    ),
                ):
                    replace(RuntimeDispatcherConfig(), **{field_name: invalid})

    def test_capacity_values_require_positive_non_boolean_integers(self) -> None:
        for field_name in ("worker_count", "scheduler_pending_limit"):
            for invalid in (0, -1, True, 1.5, "1"):
                with (
                    self.subTest(field_name=field_name, invalid=invalid),
                    self.assertRaisesRegex(
                        ValueError,
                        f"^{field_name} must be a positive integer$",
                    ),
                ):
                    replace(RuntimeDispatcherConfig(), **{field_name: invalid})

    def test_timeouts_accept_none_or_non_negative_finite_numbers(self) -> None:
        field_names = (
            "runtime_drain_timeout",
            "worker_stop_timeout",
            "callback_timeout",
        )
        for field_name in field_names:
            self.assertIsNone(
                getattr(replace(RuntimeDispatcherConfig(), **{field_name: None}), field_name)
            )
            self.assertEqual(
                getattr(replace(RuntimeDispatcherConfig(), **{field_name: 0}), field_name),
                0,
            )
            for invalid in (-1, True, float("inf"), float("nan"), "1"):
                with (
                    self.subTest(field_name=field_name, invalid=invalid),
                    self.assertRaisesRegex(
                        ValueError,
                        f"^{field_name} must be a non-negative finite number or None$",
                    ),
                ):
                    replace(RuntimeDispatcherConfig(), **{field_name: invalid})

    def test_scheduler_kind_and_sequential_worker_invariant_are_validated(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "^scheduler_kind must be a RuntimeSchedulerKind$",
        ):
            RuntimeDispatcherConfig(scheduler_kind="sequential")  # type: ignore[arg-type]

        with self.assertRaisesRegex(
            ValueError,
            "^sequential scheduler requires worker_count == 1$",
        ):
            RuntimeDispatcherConfig(
                scheduler_kind=RuntimeSchedulerKind.SEQUENTIAL,
                worker_count=2,
            )

        config = RuntimeDispatcherConfig(
            scheduler_kind=RuntimeSchedulerKind.KEYED_SERIAL,
            worker_count=2,
            scheduler_pending_limit=8,
        )
        self.assertEqual(config.worker_count, 2)


if __name__ == "__main__":
    unittest.main()

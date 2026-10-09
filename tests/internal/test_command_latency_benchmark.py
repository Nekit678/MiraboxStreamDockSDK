"""Exercise slow-sender measurements without performance thresholds."""

from __future__ import annotations

import unittest

from scripts.benchmark_command_latency import measure_command_latency


class CommandLatencyBenchmarkTests(unittest.TestCase):
    def test_command_modes_report_all_completions_and_worker_occupancy(self) -> None:
        for mode in ("sync_commands", "async_commands"):
            with self.subTest(mode=mode):
                result = measure_command_latency(
                    mode, batches=2, worker_count=2, sender_delay=0.001
                )
                self.assertEqual(result.command_count, 4)
                self.assertEqual(result.completed_commands, 4)
                self.assertEqual(
                    result.occupied_workers_before_input, 2 if mode.startswith("sync") else 0
                )
                self.assertGreater(result.command_p99_ms, 0)
                self.assertGreaterEqual(result.command_p99_ms, result.command_p95_ms)
                self.assertGreater(result.input_start_p99_ms, 0)
                self.assertIsNone(result.snapshot_p95_ms)
                self.assertIsNone(result.committed_count)

    def test_settings_modes_report_commit_and_isolated_snapshot_latency(self) -> None:
        for mode in ("sync_settings", "async_settings"):
            with self.subTest(mode=mode):
                result = measure_command_latency(
                    mode, batches=2, worker_count=2, sender_delay=0.001
                )
                self.assertEqual(result.command_count, 2)
                self.assertEqual(result.completed_commands, 2)
                self.assertEqual(result.committed_count, 1)
                self.assertIsNotNone(result.snapshot_p95_ms)
                if mode.startswith("async"):
                    self.assertEqual(result.occupied_workers_before_input, 0)


if __name__ == "__main__":
    unittest.main()

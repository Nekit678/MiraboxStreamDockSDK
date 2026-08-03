"""Regression tests for the runtime scheduler benchmark harness."""

from __future__ import annotations

import unittest

from scripts import benchmark_runtime_scheduler


class RuntimeSchedulerBenchmarkTests(unittest.TestCase):
    def test_scheduler_matrix_reports_bounded_sequential_and_keyed_measurements(self) -> None:
        measurements = benchmark_runtime_scheduler.benchmark_scheduler_matrix(
            event_count=32,
            repeats=1,
            callback_delay=0.0001,
            worker_count=4,
            pending_limit=8,
        )

        self.assertEqual(len(measurements), 8)
        for measurement in measurements:
            with self.subTest(
                scenario=measurement.scenario,
                scheduler=measurement.scheduler,
            ):
                self.assertGreater(measurement.throughput_per_second, 0)
                self.assertGreaterEqual(measurement.callback_start_p50_ms, 0)
                self.assertGreaterEqual(measurement.callback_start_p95_ms, 0)
                self.assertGreaterEqual(measurement.callback_start_p99_ms, 0)
                if measurement.scheduler == "sequential":
                    self.assertEqual(measurement.peak_pending, 0)
                else:
                    self.assertLessEqual(measurement.peak_pending, 8)

    def test_larger_prefetch_limit_reduces_boundary_coalescing_opportunity(self) -> None:
        small = benchmark_runtime_scheduler.measure_prefetch_coalescing(
            pending_limit=1,
            rotation_count=24,
        )
        large = benchmark_runtime_scheduler.measure_prefetch_coalescing(
            pending_limit=8,
            rotation_count=24,
        )

        self.assertEqual(small.peak_scheduler_pending, 1)
        self.assertEqual(large.peak_scheduler_pending, 8)
        self.assertGreater(small.coalesced_rotations, large.coalesced_rotations)
        self.assertGreater(small.coalescing_ratio, large.coalescing_ratio)
        self.assertGreaterEqual(small.scheduler_backpressure, 1)
        self.assertGreaterEqual(large.scheduler_backpressure, 1)

    def test_percentile_interpolates_ordered_samples(self) -> None:
        self.assertEqual(benchmark_runtime_scheduler._percentile((0, 10), 50), 5)
        self.assertEqual(benchmark_runtime_scheduler._percentile((30, 10, 20), 95), 29)


if __name__ == "__main__":
    unittest.main()

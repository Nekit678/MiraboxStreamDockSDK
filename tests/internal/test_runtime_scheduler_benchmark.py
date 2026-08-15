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

    def test_scheduler_gate_reports_throughput_and_boundedness_violations(self) -> None:
        measurements = []
        for scenario in benchmark_runtime_scheduler.SCHEDULER_PERFORMANCE_BUDGETS:
            measurements.extend(
                (
                    _scheduler_measurement(scenario, "sequential", throughput=100, peak_pending=0),
                    _scheduler_measurement(
                        scenario,
                        "keyed_serial",
                        throughput=50,
                        peak_pending=9,
                        callback_start_p95_ms=31,
                    ),
                )
            )

        comparisons = benchmark_runtime_scheduler.evaluate_scheduler_performance(
            measurements,
            pending_limit=8,
        )

        self.assertEqual(len(comparisons), 4)
        self.assertTrue(all(not comparison.within_budget for comparison in comparisons))
        self.assertTrue(all(comparison.violations for comparison in comparisons))
        self.assertEqual(comparisons[0].keyed_to_sequential_throughput_ratio, 0.5)
        self.assertIn("callback-start p95", " ".join(comparisons[0].violations))

    def test_every_canonical_measurement_has_an_explicit_performance_budget(self) -> None:
        self.assertEqual(
            set(benchmark_runtime_scheduler.SCHEDULER_PERFORMANCE_BUDGETS),
            {"single_context", "contexts_4", "contexts_16", "contexts_64"},
        )
        self.assertEqual(
            set(benchmark_runtime_scheduler.COALESCING_PERFORMANCE_BUDGETS),
            {1, 4, 16, 64},
        )
        for budget in benchmark_runtime_scheduler.SCHEDULER_PERFORMANCE_BUDGETS.values():
            with self.subTest(budget=budget):
                self.assertGreater(budget.min_throughput_ratio, 0)
                self.assertGreater(budget.max_callback_start_p95_ms, 0)
                self.assertTrue(budget.reason)
        for budget in benchmark_runtime_scheduler.COALESCING_PERFORMANCE_BUDGETS.values():
            with self.subTest(budget=budget):
                self.assertGreaterEqual(budget.min_coalescing_ratio, 0)
                self.assertLessEqual(budget.min_coalescing_ratio, 1)
                self.assertTrue(budget.reason)

    def test_coalescing_gate_requires_the_complete_canonical_matrix(self) -> None:
        with self.assertRaisesRegex(ValueError, "pending limit 4"):
            benchmark_runtime_scheduler.evaluate_coalescing_performance(
                (_coalescing_measurement(1, ratio=1),)
            )


def _scheduler_measurement(
    scenario: str,
    scheduler: str,
    *,
    throughput: float,
    peak_pending: float,
    callback_start_p95_ms: float = 0,
) -> benchmark_runtime_scheduler.SchedulerBenchmarkMeasurement:
    return benchmark_runtime_scheduler.SchedulerBenchmarkMeasurement(
        scenario=scenario,
        scheduler=scheduler,
        event_count=100,
        duration_seconds=1,
        throughput_per_second=throughput,
        callback_start_p50_ms=0,
        callback_start_p95_ms=callback_start_p95_ms,
        callback_start_p99_ms=0,
        peak_pending=peak_pending,
        peak_active_callbacks=1,
        admission_backpressure=0,
    )


def _coalescing_measurement(
    pending_limit: int,
    *,
    ratio: float,
) -> benchmark_runtime_scheduler.CoalescingMeasurement:
    return benchmark_runtime_scheduler.CoalescingMeasurement(
        pending_limit=pending_limit,
        submitted_rotations=100,
        coalesced_rotations=int(ratio * 100),
        coalescing_ratio=ratio,
        dispatched_events=1,
        peak_boundary_depth=1,
        peak_scheduler_pending=pending_limit,
        scheduler_backpressure=1,
    )


if __name__ == "__main__":
    unittest.main()

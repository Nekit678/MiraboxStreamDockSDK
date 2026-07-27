"""Regression tests for the boundary benchmark guardrails."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import benchmark_boundary


class BoundaryBenchmarkTests(unittest.TestCase):
    def test_concurrent_scenario_propagates_producer_failures(self) -> None:
        harness = _FailingHarness()
        with (
            patch.object(benchmark_boundary, "_new_harness", return_value=harness),
            self.assertRaisesRegex(ExceptionGroup, "producers failed") as caught,
        ):
            benchmark_boundary._concurrent_set_title("legacy", 4, 0)

        self.assertEqual(len(caught.exception.exceptions), 4)
        self.assertTrue(harness.closed)

    def test_comparison_reports_every_exceeded_budget(self) -> None:
        legacy = _measurement(latency=100, throughput=10, peak_bytes=100, peak_depth=2)
        next_boundary = _measurement(
            latency=151,
            throughput=8,
            peak_bytes=201,
            peak_depth=4,
        )
        budget = benchmark_boundary.BenchmarkBudget(1.5, 2.0, "test budget")

        comparison = benchmark_boundary._compare_measurements(
            legacy,
            next_boundary,
            budget,
        )

        self.assertFalse(comparison.within_budget)
        self.assertEqual(comparison.latency_ratio, 1.51)
        self.assertEqual(comparison.throughput_ratio, 0.8)
        self.assertEqual(comparison.peak_traced_bytes_ratio, 2.01)
        self.assertEqual(comparison.peak_queue_depth_ratio, 2)
        self.assertEqual(comparison.max_latency_ratio, 1.5)
        self.assertEqual(comparison.max_peak_traced_bytes_ratio, 2)
        self.assertEqual(len(comparison.violations), 2)

    def test_every_scenario_has_an_explicit_same_run_budget(self) -> None:
        self.assertEqual(
            set(benchmark_boundary.SCENARIOS),
            set(benchmark_boundary.BENCHMARK_BUDGETS),
        )
        for name, budget in benchmark_boundary.BENCHMARK_BUDGETS.items():
            with self.subTest(scenario=name):
                self.assertGreaterEqual(budget.max_latency_ratio, 1)
                self.assertGreaterEqual(budget.max_peak_traced_bytes_ratio, 1)
                self.assertTrue(budget.reason)


class _FailingHarness:
    def __init__(self) -> None:
        self.closed = False

    def send_async(self, _command: object) -> None:
        raise RuntimeError("producer failed")

    def peak_outbound_depth(self) -> int:
        return 0

    def sent_count(self) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


def _measurement(
    *,
    latency: float,
    throughput: float,
    peak_bytes: float,
    peak_depth: float,
) -> benchmark_boundary.BenchmarkMeasurement:
    return benchmark_boundary.BenchmarkMeasurement(
        latency_ms=latency,
        throughput_per_second=throughput,
        net_allocation_blocks=0,
        net_allocated_bytes=0,
        peak_traced_bytes=peak_bytes,
        peak_queue_depth=peak_depth,
    )


if __name__ == "__main__":
    unittest.main()

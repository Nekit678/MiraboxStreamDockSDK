"""Check mixed workload ordering and bounded memory measurement behavior."""

from __future__ import annotations

import json
import tracemalloc
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts import benchmark_runtime_workload
from scripts.benchmark_runtime_scheduler import _key_down
from scripts.benchmark_runtime_workload import (
    _MixedDispatcher,
    measure_memory_soak,
    measure_mixed_load,
)


class RuntimeWorkloadBenchmarkTests(unittest.TestCase):
    def test_cli_preserves_all_measurements_when_ordering_checks_fail(self) -> None:
        original_dispatch = _MixedDispatcher.dispatch

        def invalid_dispatch(dispatcher, event):
            result = original_dispatch(dispatcher, event)
            with dispatcher.lock:
                dispatcher.ordering_violations += 1
            return result

        output, diagnostics = StringIO(), StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "benchmark_runtime_workload",
                    "--batches",
                    "1",
                    "--events-per-segment",
                    "8",
                ],
            ),
            patch.object(_MixedDispatcher, "dispatch", invalid_dispatch),
            redirect_stdout(output),
            redirect_stderr(diagnostics),
        ):
            self.assertEqual(benchmark_runtime_workload.main(), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["invariants_passed"])
        self.assertEqual(len(result["measurements"]), 4)
        self.assertTrue(all(value["event_count"] == 37 for value in result["measurements"]))
        self.assertTrue(all(not value["passed"] for value in result["measurements"]))
        self.assertIn("ordering, completion or queue bounds failed", diagnostics.getvalue())

    def test_mixed_events_preserve_fifo_and_global_barriers_under_backpressure(self) -> None:
        for kind in ("sequential", "keyed_serial"):
            for pending_limit in (1, 4, 64):
                with self.subTest(kind=kind, pending_limit=pending_limit):
                    result = measure_mixed_load(
                        kind,
                        batches=2,
                        events_per_segment=20,
                        callback_delay=0.0001,
                        pending_limit=pending_limit,
                        source_limit=8,
                        payload_sizes=(64, 4096),
                    )
                    self.assertTrue(result.passed)
                    self.assertEqual(result.event_count, 170)
                    self.assertEqual(result.barriers_processed, 10)
                    self.assertEqual(result.ordering_violations, 0)
                    self.assertGreater(result.throughput_per_second, 0)
                    self.assertIsNotNone(result.callback_start_p99_ms)
                    self.assertEqual(result.source["acknowledged"], 170)
                    self.assertEqual(result.source["current_bytes"], 0)
                    self.assertLessEqual(result.source["peak_depth"], 8)
                    self.assertLessEqual(result.scheduler_metrics["peak_pending"], pending_limit)
                    self.assertEqual(result.scheduler_metrics["current_pending_bytes"], 0)

    def test_ordering_observer_detects_reordering_and_crossing_a_barrier(self) -> None:
        first, barrier, last = (_key_down("hot") for _ in range(3))
        for early in (barrier, last):
            with self.subTest(early=early):
                dispatcher = _MixedDispatcher(0, record_latency=False)
                dispatcher.mark_submitted(first, is_barrier=False)
                dispatcher.mark_submitted(barrier, is_barrier=True)
                dispatcher.mark_submitted(last, is_barrier=False)
                dispatcher.dispatch(early)
                self.assertGreater(dispatcher.ordering_violations, 0)

        dispatcher = _MixedDispatcher(0, record_latency=False)
        dispatcher.mark_submitted(first, is_barrier=False)
        dispatcher.mark_submitted(last, is_barrier=False)
        dispatcher.dispatch(last)
        self.assertGreater(dispatcher.ordering_violations, 0)

    def test_soak_reports_drained_samples_without_retaining_latency_history(self) -> None:
        for kind in ("sequential", "keyed_serial"):
            with self.subTest(kind=kind):
                result = measure_memory_soak(
                    kind,
                    duration_seconds=0.03,
                    sample_interval=0.005,
                    events_per_segment=8,
                    source_limit=8,
                    pending_limit=4,
                    payload_sizes=(64, 4096),
                )
                self.assertTrue(result.workload.passed)
                self.assertIsNone(result.workload.callback_start_p95_ms)
                self.assertIsNone(result.workload.callback_start_p99_ms)
                self.assertGreaterEqual(len(result.samples), 2)
                first, last = result.samples[0], result.samples[-1]
                self.assertEqual(first.acknowledged_events, 37)
                self.assertEqual(result.warmup_events, 37)
                self.assertGreater(last.acknowledged_events, first.acknowledged_events)
                self.assertGreaterEqual(last.elapsed_seconds, 0.03)
                self.assertEqual(last.acknowledged_events, result.workload.event_count)
                self.assertGreaterEqual(last.traced_peak_bytes, last.traced_retained_bytes)
                self.assertEqual(
                    result.traced_growth_bytes,
                    last.traced_retained_bytes - first.traced_retained_bytes,
                )
                self.assertTrue(all(sample.source_retained_bytes == 0 for sample in result.samples))
                self.assertTrue(
                    all(sample.scheduler_pending_bytes == 0 for sample in result.samples)
                )
                self.assertFalse(tracemalloc.is_tracing())

    def test_soak_rejects_invalid_durations_and_intervals(self) -> None:
        for field in ("duration_seconds", "sample_interval"):
            for value in (0, -1, float("nan"), float("inf"), True):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, "positive finite number"),
                ):
                    measure_memory_soak("keyed_serial", **{field: value})

    def test_soak_stops_tracing_after_a_workload_failure(self) -> None:
        with (
            patch.object(
                benchmark_runtime_workload._Workload,
                "batch",
                side_effect=RuntimeError("batch failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "batch failed"),
        ):
            measure_memory_soak("keyed_serial", duration_seconds=0.03)
        self.assertFalse(tracemalloc.is_tracing())


if __name__ == "__main__":
    unittest.main()

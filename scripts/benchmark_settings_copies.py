"""Measure settings latency, transient memory and cloning work for PERF-05.

Timing, tracemalloc and profiling run separately. The action measurement covers
parsing a decoded envelope and constructing a fresh context manager/action;
it excludes JSON text decoding, transport and callbacks. No timing gate applies.
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import json
import platform
import statistics
import tracemalloc
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter_ns

from mirabox_sdk import Action, ActionRegistry, JsonObject, StreamDockSender, WillAppearEvent
from mirabox_sdk._internal.runtime.actions import DefaultActionContextManager
from mirabox_sdk._internal.runtime.adapters import ActionRegistryFactoryAdapter
from mirabox_sdk._internal.runtime.global_settings import (
    DefaultGlobalSettingsState,
    GlobalSettingsCoordinator,
)
from mirabox_sdk.parser import parse_stream_dock_event
from mirabox_sdk.testing import FakeStreamDockSender


@dataclass(frozen=True, slots=True)
class SettingsCopyMeasurement:
    shape: str
    operation: str
    samples: int
    median_ms: float
    retained_bytes: int
    peak_bytes: int
    dict_clone_calls: int
    value_clone_calls: int


@dataclass(frozen=True, slots=True)
class _Dependencies:
    stream_dock: StreamDockSender


class _Action(Action[JsonObject, _Dependencies]):
    pass


def _settings_shapes() -> dict[str, JsonObject]:
    nested: JsonObject = {"items": [{"count": index} for index in range(100)]}
    for _ in range(20):
        nested = {"child": nested}
    return {
        "small": {"count": 1, "enabled": True},
        "wide": {"items": list(range(10_000))},
        "nested": nested,
    }


def _measure(
    shape: str, name: str, operation: Callable[[], object], samples: int
) -> SettingsCopyMeasurement:
    operation()
    durations: list[float] = []
    for _ in range(samples):
        started = perf_counter_ns()
        operation()
        durations.append((perf_counter_ns() - started) / 1_000_000)

    gc.collect()
    tracemalloc.start()
    try:
        result = operation()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    del result

    profiler = cProfile.Profile()
    profiler.runcall(operation)
    clone_calls = {"_clone_json_dict": 0, "_clone_json_value": 0}
    for entry in profiler.getstats():
        code = entry.code
        if not isinstance(code, str) and Path(code.co_filename).name == "json_types.py":
            if code.co_name in clone_calls:
                clone_calls[code.co_name] += entry.callcount
    return SettingsCopyMeasurement(
        shape,
        name,
        samples,
        statistics.median(durations),
        retained,
        peak,
        clone_calls["_clone_json_dict"],
        clone_calls["_clone_json_value"],
    )


def measure_settings_copies(*, samples: int = 31) -> list[SettingsCopyMeasurement]:
    """Return independent measurements for small, wide and nested settings."""

    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    sender = FakeStreamDockSender()
    registry: ActionRegistry[_Dependencies] = ActionRegistry()
    registry.register("benchmark.action")(_Action)
    factory = ActionRegistryFactoryAdapter(registry, _Dependencies(sender))
    measurements: list[SettingsCopyMeasurement] = []
    for shape, settings in _settings_shapes().items():
        state = DefaultGlobalSettingsState("benchmark.plugin", sender)
        state.receive(settings)
        facade = GlobalSettingsCoordinator(state)
        message: JsonObject = {
            "event": "willAppear",
            "action": "benchmark.action",
            "context": "benchmark.context",
            "device": "benchmark.device",
            "payload": {
                "settings": settings,
                "coordinates": {"column": 0, "row": 0},
                "controller": "Keypad",
                "isInMultiAction": False,
            },
        }

        def parse_and_create(message: JsonObject = message) -> object:
            event = parse_stream_dock_event(message)
            assert isinstance(event, WillAppearEvent)
            return DefaultActionContextManager(factory).create(event)

        for name, operation in (
            ("backend_settings", lambda state=state: state.settings),
            ("global_snapshot", facade.snapshot),
            ("global_replay", facade.new_replay_event),
            ("parse_action_creation", parse_and_create),
        ):
            measurements.append(_measure(shape, name, operation, samples))
    return measurements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=31)
    args = parser.parse_args()
    if args.samples <= 0:
        parser.error("--samples must be a positive integer")
    print(
        json.dumps(
            {
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                },
                "measurements": [
                    asdict(result) for result in measure_settings_copies(samples=args.samples)
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

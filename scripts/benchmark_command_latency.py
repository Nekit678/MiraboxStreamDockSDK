"""Measure PERF-04 through real queues, writer and callback workers.

The sender is an in-process transport substitute with a controlled per-frame
delay. Results exclude sockets, device acknowledgements and external I/O.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
from dataclasses import asdict, dataclass
from math import isfinite
from threading import Condition, Event, Thread
from time import perf_counter, sleep

from mirabox_sdk import CommandFuture, JsonObject, SetTitleCommand, StreamDockEvent
from mirabox_sdk._internal.messaging.inbound import InboundEventQueue
from mirabox_sdk._internal.messaging.outbound import OutboundCommandQueue
from mirabox_sdk._internal.messaging.writer import CommandWriter
from mirabox_sdk._internal.protocol.encoder import JsonStreamDockCommandEncoder
from mirabox_sdk._internal.runtime.global_settings import (
    DefaultGlobalSettingsState,
    GlobalSettingsCoordinator,
)
from mirabox_sdk._internal.runtime.keyed_scheduler import KeyedSerialHandlerScheduler
from mirabox_sdk._internal.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._internal.runtime.pumps import RuntimeEventPump
from mirabox_sdk._internal.transport.queues import RawOutboundQueue, TransportQueueClosedError
from scripts.benchmark_runtime_scheduler import _key_down, _percentile


@dataclass(frozen=True, slots=True)
class CommandLatencyMeasurement:
    mode: str
    batches: int
    command_count: int
    worker_count: int
    sender_delay_ms: float
    command_p95_ms: float
    command_p99_ms: float
    occupied_workers_before_input: float
    input_start_p95_ms: float
    input_start_p99_ms: float
    snapshot_p95_ms: float | None
    completed_commands: int
    committed_count: int | None


class _DelayedSender:
    def __init__(self, queue: RawOutboundQueue, delay: float) -> None:
        self._queue = queue
        self._delay = delay
        self.started = Event()
        self.release = Event()
        self.completed_at: dict[str, float] = {}
        self.errors: list[Exception] = []
        self.thread = Thread(target=self._run, name="benchmark-slow-sender", daemon=True)

    def _run(self) -> None:
        try:
            while True:
                try:
                    frame = self._queue.receive()
                except TransportQueueClosedError:
                    return
                self.started.set()
                if not self.release.wait(5):
                    error = TimeoutError("benchmark sender was not released")
                    frame.receipt._finish(error=error)
                    raise error
                sleep(self._delay)
                frame.receipt._finish()
                self.completed_at[json.loads(frame.payload)["context"]] = perf_counter()
        except Exception as exc:
            self.errors.append(exc)


class _Dispatcher:
    def __init__(self, mode: str, commands: OutboundCommandQueue) -> None:
        self.mode = mode
        self.commands = commands
        self.settings = GlobalSettingsCoordinator(DefaultGlobalSettingsState("plugin", commands))
        self.condition = Condition()
        self.entered = 0
        self.command_started_at: dict[str, float] = {}
        self.futures: list[CommandFuture] = []
        self.snapshot_latencies: list[float] = []
        self.input_started = Event()
        self.input_started_at = 0.0

    def dispatch(self, event: StreamDockEvent) -> DispatchResult:
        context = getattr(event, "context", "")
        if context == "input":
            self.input_started_at = perf_counter()
            self.input_started.set()
            return DispatchResult(DispatchOutcome.HANDLED)
        started = perf_counter()
        with self.condition:
            self.entered += 1
            self.condition.notify_all()
        if context.startswith("reader"):
            self.settings.snapshot()
            with self.condition:
                self.snapshot_latencies.append((perf_counter() - started) * 1000)
        elif self.mode.endswith("settings"):
            self.command_started_at["plugin"] = started

            def increment(draft: JsonObject) -> None:
                count = draft.get("count", 0)
                assert isinstance(count, int)
                draft["count"] = count + 1

            if self.mode.startswith("async"):
                self.futures.append(self.settings.update_async(increment))
            else:
                self.settings.update(increment)
        else:
            self.command_started_at[context] = started
            future = self.commands.send_async(SetTitleCommand(context, context))
            with self.condition:
                self.futures.append(future)
            if self.mode.startswith("sync"):
                future.result()
        return DispatchResult(DispatchOutcome.HANDLED)


def measure_command_latency(
    mode: str,
    *,
    batches: int = 8,
    worker_count: int = 4,
    sender_delay: float = 0.02,
) -> CommandLatencyMeasurement:
    """Probe input while a batch occupies the sender and callback workers.

    Command modes submit one display command per worker. Settings modes submit
    one transaction plus snapshot readers on the remaining workers. The first
    send is gated until all callbacks have entered. Command latency includes
    construction, queue acceptance, encoding, the setup gate and sender delay.
    """

    if mode not in ("sync_commands", "async_commands", "sync_settings", "async_settings"):
        raise ValueError("unknown command latency mode")
    for name, value in (("batches", batches), ("worker_count", worker_count)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if (
        isinstance(sender_delay, bool)
        or not isinstance(sender_delay, (int, float))
        or not isfinite(sender_delay)
        or sender_delay < 0
    ):
        raise ValueError("sender_delay must be a non-negative finite number")
    latencies: list[float] = []
    inputs: list[float] = []
    snapshots: list[float] = []
    occupied: list[int] = []
    completed = 0
    committed: int | None = None
    for _ in range(batches):
        source = InboundEventQueue(worker_count + 1)
        commands = OutboundCommandQueue(worker_count + 1)
        raw = RawOutboundQueue(worker_count + 1)
        writer = CommandWriter(commands, JsonStreamDockCommandEncoder(), raw)
        sender = _DelayedSender(raw, sender_delay)
        dispatcher = _Dispatcher(mode, commands)
        scheduler = KeyedSerialHandlerScheduler(
            dispatcher, worker_count=worker_count, pending_limit=worker_count + 1
        )
        pump = RuntimeEventPump(source, scheduler, poll_interval=0.001)
        writer.start()
        sender.thread.start()
        scheduler.start()
        pump.start()
        try:
            source.submit(_key_down("command-0"))
            if not sender.started.wait(2):
                raise TimeoutError("benchmark command did not reach the sender")
            for index in range(1, worker_count):
                prefix = "reader" if mode.endswith("settings") else "command"
                source.submit(_key_down(f"{prefix}-{index}"))
            with dispatcher.condition:
                if not dispatcher.condition.wait_for(
                    lambda dispatcher=dispatcher: dispatcher.entered == worker_count, timeout=2
                ):
                    raise TimeoutError("benchmark callbacks did not start")
            if mode.startswith("async") and not source.drain(timeout=2):
                raise TimeoutError("async callbacks did not return before transport completion")
            occupied.append(scheduler.metrics().current_active_callbacks)
            input_submitted = perf_counter()
            source.submit(_key_down("input"))
            source.stop_accepting()
            if mode.startswith("async") and not dispatcher.input_started.wait(2):
                raise TimeoutError("async benchmark input did not start while sender was blocked")
            sender.release.set()
            if not pump.drain(timeout=5) or not scheduler.drain(timeout=5):
                raise TimeoutError("benchmark callbacks did not drain")
            for future in dispatcher.futures:
                future.result(timeout=5)
            if not writer.drain(timeout=5):
                raise TimeoutError("benchmark commands did not complete")
            if scheduler.metrics().callback_failures or sender.errors:
                raise RuntimeError("benchmark callback or sender failed")
            # Observe sender timestamps only after joining its thread below.
            raw.stop_accepting()
            sender.thread.join(2)
            if sender.thread.is_alive():
                raise TimeoutError("benchmark sender did not stop")
            latencies.extend(
                (sender.completed_at[context] - started) * 1000
                for context, started in dispatcher.command_started_at.items()
            )
            inputs.append((dispatcher.input_started_at - input_submitted) * 1000)
            snapshots.extend(dispatcher.snapshot_latencies)
            completed += writer.metrics().completed
            if mode.endswith("settings"):
                committed = int(dispatcher.settings.snapshot()["count"])
        finally:
            sender.release.set()
            source.stop_accepting()
            pump.stop(timeout=5)
            scheduler.stop(timeout=5)
            commands.stop_accepting()
            writer.stop(timeout=5)
            raw.stop_accepting()
            sender.thread.join(5)

    return CommandLatencyMeasurement(
        mode=mode,
        batches=batches,
        command_count=len(latencies),
        worker_count=worker_count,
        sender_delay_ms=sender_delay * 1000,
        command_p95_ms=_percentile(latencies, 95),
        command_p99_ms=_percentile(latencies, 99),
        occupied_workers_before_input=statistics.mean(occupied),
        input_start_p95_ms=_percentile(inputs, 95),
        input_start_p99_ms=_percentile(inputs, 99),
        snapshot_p95_ms=_percentile(snapshots, 95) if snapshots else None,
        completed_commands=completed,
        committed_count=committed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sender-delay-ms", type=float, default=20)
    parser.add_argument("--include-async-settings", action="store_true")
    args = parser.parse_args()
    modes = ["sync_commands", "async_commands", "sync_settings"]
    if args.include_async_settings:
        modes.append("async_settings")
    print(
        json.dumps(
            {
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                },
                "measurements": [
                    asdict(
                        measure_command_latency(
                            mode,
                            batches=args.batches,
                            worker_count=args.workers,
                            sender_delay=args.sender_delay_ms / 1000,
                        )
                    )
                    for mode in modes
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from mirabox_sdk import RegisterPluginCommand, StreamDockEvent
from mirabox_sdk._next.messaging.models import CommandFuture
from mirabox_sdk._next.runtime.metrics import HandlerSchedulerMetrics
from mirabox_sdk._next.runtime.models import DispatchOutcome, DispatchResult
from mirabox_sdk._next.runtime.ports import (
    ActionContextManager,
    ActionFactory,
    DispatchCompletion,
    HandlerScheduler,
    PluginHooks,
    RuntimeAction,
    RuntimeLifecycle,
    StreamDockSender,
)

PROJECT_ROOT = Path(__file__).parents[3]


class _Action:
    def __init__(self, action: str = "com.example.action", context: str = "context") -> None:
        self.action = action
        self.context = context


class _Factory:
    def create(
        self,
        action_uuid: str,
        context: str,
        initial_settings: dict[str, object],
    ) -> _Action | None:
        return _Action(action_uuid, context)


class _Hooks:
    def on_unhandled_event(self, event: object) -> None:
        pass


class _Sender:
    def send(self, command: object) -> None:
        self.send_async(command).result()

    def send_async(self, command: object) -> CommandFuture:
        completion = CommandFuture()
        completion._finish()
        return completion


class _Completion:
    def done(self) -> bool:
        return True

    def result(self, timeout: float | None = None) -> DispatchResult:
        return DispatchResult(DispatchOutcome.HANDLED)

    def add_done_callback(self, callback: object) -> None:
        callback(self)  # type: ignore[operator]


class _Scheduler:
    def start(self) -> None:
        pass

    def submit(self, event: object) -> _Completion:
        return _Completion()

    def stop_accepting(self) -> None:
        pass

    def drain(self, *, timeout: float | None = None) -> bool:
        return True

    def stop(self, *, timeout: float | None = None) -> bool:
        return True

    def metrics(self) -> HandlerSchedulerMetrics:
        return HandlerSchedulerMetrics()


class _Contexts:
    def __init__(self) -> None:
        self.action = _Action()

    def create(self, event: object) -> _Action | None:
        return self.action

    def get(self, context: str) -> _Action | None:
        return self.action

    def remove(self, context: str) -> _Action | None:
        return self.action

    def snapshot(self) -> tuple[_Action, ...]:
        return (self.action,)

    def clear(self) -> tuple[_Action, ...]:
        return (self.action,)


class _Lifecycle:
    def run_forever(self) -> None:
        pass

    def close(self) -> None:
        pass

    def metrics(self) -> object:
        return object()


class RuntimePortContractTests(unittest.TestCase):
    def test_ports_accept_structural_implementations(self) -> None:
        contracts_and_values = (
            (RuntimeAction, _Action()),
            (ActionFactory, _Factory()),
            (PluginHooks, _Hooks()),
            (StreamDockSender, _Sender()),
            (DispatchCompletion, _Completion()),
            (HandlerScheduler, _Scheduler()),
            (ActionContextManager, _Contexts()),
            (RuntimeLifecycle, _Lifecycle()),
        )

        for contract, value in contracts_and_values:
            with self.subTest(contract=contract.__name__):
                self.assertIsInstance(value, contract)

        command = RegisterPluginCommand(event="registerPlugin", uuid="plugin.uuid")
        self.assertIsNone(_Sender().send_async(command).result(timeout=0))
        result = _Scheduler().submit(StreamDockEvent()).result(timeout=0)
        self.assertIs(result.outcome, DispatchOutcome.HANDLED)

    def test_runtime_modules_import_without_threads_or_concrete_boundary_components(self) -> None:
        module_names = (
            "mirabox_sdk._next.runtime",
            "mirabox_sdk._next.runtime.config",
            "mirabox_sdk._next.runtime.metrics",
            "mirabox_sdk._next.runtime.models",
            "mirabox_sdk._next.runtime.ports",
        )
        script = (
            "import importlib\n"
            "import sys\n"
            "from unittest.mock import patch\n"
            f"modules = {module_names!r}\n"
            "with patch('threading.Thread.start', side_effect=AssertionError("
            "'import started a thread')):\n"
            "    for module in modules:\n"
            "        importlib.import_module(module)\n"
            "for forbidden in (\n"
            "    'mirabox_sdk._next.protocol.decoder',\n"
            "    'mirabox_sdk._next.protocol.encoder',\n"
            "    'mirabox_sdk._next.transport.queues',\n"
            "    'mirabox_sdk._next.transport.websocket',\n"
            "):\n"
            "    if forbidden in sys.modules:\n"
            "        raise AssertionError(f'imported concrete implementation: {forbidden}')\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

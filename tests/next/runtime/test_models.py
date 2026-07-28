from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from mirabox_sdk._next.runtime.models import (
    DispatchOutcome,
    DispatchResult,
    InvalidRuntimeStateTransitionError,
    InvalidSessionStateTransitionError,
    RuntimeLifecycleState,
    SessionState,
    transition_runtime_state,
    transition_session_state,
)


class RuntimeStateModelTests(unittest.TestCase):
    def test_runtime_lifecycle_accepts_only_documented_transitions(self) -> None:
        transitions = (
            (RuntimeLifecycleState.NEW, RuntimeLifecycleState.STARTING),
            (RuntimeLifecycleState.NEW, RuntimeLifecycleState.STOPPED),
            (RuntimeLifecycleState.STARTING, RuntimeLifecycleState.RUNNING),
            (RuntimeLifecycleState.STARTING, RuntimeLifecycleState.STOPPING),
            (RuntimeLifecycleState.STARTING, RuntimeLifecycleState.FAILED),
            (RuntimeLifecycleState.RUNNING, RuntimeLifecycleState.STOPPING),
            (RuntimeLifecycleState.RUNNING, RuntimeLifecycleState.FAILED),
            (RuntimeLifecycleState.STOPPING, RuntimeLifecycleState.STOPPED),
            (RuntimeLifecycleState.STOPPING, RuntimeLifecycleState.FAILED),
        )

        for current, target in transitions:
            with self.subTest(current=current, target=target):
                self.assertIs(transition_runtime_state(current, target), target)

        for current, target in (
            (RuntimeLifecycleState.NEW, RuntimeLifecycleState.RUNNING),
            (RuntimeLifecycleState.RUNNING, RuntimeLifecycleState.STOPPED),
            (RuntimeLifecycleState.STOPPED, RuntimeLifecycleState.STARTING),
            (RuntimeLifecycleState.FAILED, RuntimeLifecycleState.STOPPING),
        ):
            with (
                self.subTest(current=current, target=target),
                self.assertRaises(InvalidRuntimeStateTransitionError) as raised,
            ):
                transition_runtime_state(current, target)
            self.assertIs(raised.exception.current, current)
            self.assertIs(raised.exception.target, target)

    def test_session_accepts_only_single_run_initialization_transitions(self) -> None:
        transitions = (
            (SessionState.WAITING_CONNECTED, SessionState.INITIALIZING),
            (SessionState.INITIALIZING, SessionState.READY),
            (SessionState.INITIALIZING, SessionState.FAILED),
            (SessionState.READY, SessionState.DISCONNECTED),
        )

        for current, target in transitions:
            with self.subTest(current=current, target=target):
                self.assertIs(transition_session_state(current, target), target)

        for current, target in (
            (SessionState.WAITING_CONNECTED, SessionState.DISCONNECTED),
            (SessionState.READY, SessionState.INITIALIZING),
            (SessionState.DISCONNECTED, SessionState.READY),
            (SessionState.FAILED, SessionState.INITIALIZING),
        ):
            with (
                self.subTest(current=current, target=target),
                self.assertRaises(InvalidSessionStateTransitionError),
            ):
                transition_session_state(current, target)

    def test_terminal_state_flags_are_explicit(self) -> None:
        self.assertTrue(RuntimeLifecycleState.STOPPED.terminal)
        self.assertTrue(RuntimeLifecycleState.FAILED.terminal)
        self.assertFalse(RuntimeLifecycleState.RUNNING.terminal)
        self.assertTrue(SessionState.DISCONNECTED.terminal)
        self.assertTrue(SessionState.FAILED.terminal)
        self.assertFalse(SessionState.READY.terminal)

    def test_transition_helpers_reject_values_outside_their_state_type(self) -> None:
        with self.assertRaisesRegex(TypeError, "^current must be a RuntimeLifecycleState$"):
            transition_runtime_state("new", RuntimeLifecycleState.STARTING)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "^target must be a SessionState$"):
            transition_session_state(SessionState.READY, "disconnected")  # type: ignore[arg-type]

    def test_dispatch_result_is_frozen_and_retains_original_failure(self) -> None:
        failure = RuntimeError("callback failed")
        result = DispatchResult(DispatchOutcome.CALLBACK_FAILED, failure)

        self.assertIs(result.error, failure)
        with self.assertRaises(FrozenInstanceError):
            result.outcome = DispatchOutcome.HANDLED  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

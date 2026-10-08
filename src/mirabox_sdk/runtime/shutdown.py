"""Public shutdown diagnostics and cooperative cancellation."""

from .._internal.lifecycle import ShutdownFailure, ShutdownOutcome, StopSignal

__all__ = ["ShutdownFailure", "ShutdownOutcome", "StopSignal"]

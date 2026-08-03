"""Public configuration for the Stream Dock application stack."""

from .._next.boundary.config import BoundaryQueueConfig, BoundaryShutdownConfig
from .._next.runtime.config import RuntimeDispatcherConfig
from .._next.runtime.models import RuntimeSchedulerKind

StreamDockQueueConfig = BoundaryQueueConfig
StreamDockShutdownConfig = BoundaryShutdownConfig

__all__ = [
    "RuntimeDispatcherConfig",
    "RuntimeSchedulerKind",
    "StreamDockQueueConfig",
    "StreamDockShutdownConfig",
]

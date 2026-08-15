"""Public configuration for the Stream Dock application stack."""

from .._internal.boundary.config import BoundaryQueueConfig, BoundaryShutdownConfig
from .._internal.runtime.config import RuntimeDispatcherConfig
from .._internal.runtime.models import RuntimeSchedulerKind

StreamDockQueueConfig = BoundaryQueueConfig
StreamDockShutdownConfig = BoundaryShutdownConfig

__all__ = [
    "RuntimeDispatcherConfig",
    "RuntimeSchedulerKind",
    "StreamDockQueueConfig",
    "StreamDockShutdownConfig",
]

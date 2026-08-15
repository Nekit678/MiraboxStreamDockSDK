"""Stable Stream Dock runtime and application composition API.

Importing this package does not start threads or connect to Stream Dock.
``create_stream_dock_application()`` returns an unstarted application consumed
by ``run_plugin_cli()``.
"""

from .._next.messaging.inbound import InboundOverflowPolicy
from .._next.runtime.composition import (
    StreamDockRuntime,
    StreamDockRuntimeLifecycleError,
)
from .application import StreamDockApplication, create_stream_dock_application
from .config import (
    RuntimeDispatcherConfig,
    RuntimeSchedulerKind,
    StreamDockQueueConfig,
    StreamDockShutdownConfig,
)
from .metrics import (
    ActionContextMetrics,
    HandlerSchedulerMetrics,
    RuntimeEventPumpMetrics,
    RuntimeRouterMetrics,
    SessionCoordinatorMetrics,
    StreamDockBoundaryMetrics,
    StreamDockRuntimeMetrics,
)
from .ports import (
    ActionFactory,
    ApplicationContext,
    ApplicationService,
    ApplicationServiceFactory,
    GlobalSettings,
    PluginHooks,
    RuntimeLifecycle,
    SessionReadiness,
    StreamDockSender,
)

__all__ = [
    "ActionContextMetrics",
    "ActionFactory",
    "ApplicationContext",
    "ApplicationService",
    "ApplicationServiceFactory",
    "GlobalSettings",
    "HandlerSchedulerMetrics",
    "InboundOverflowPolicy",
    "PluginHooks",
    "RuntimeDispatcherConfig",
    "RuntimeEventPumpMetrics",
    "RuntimeLifecycle",
    "RuntimeRouterMetrics",
    "RuntimeSchedulerKind",
    "SessionReadiness",
    "SessionCoordinatorMetrics",
    "StreamDockApplication",
    "StreamDockBoundaryMetrics",
    "StreamDockQueueConfig",
    "StreamDockRuntime",
    "StreamDockRuntimeLifecycleError",
    "StreamDockRuntimeMetrics",
    "StreamDockSender",
    "StreamDockShutdownConfig",
    "create_stream_dock_application",
]

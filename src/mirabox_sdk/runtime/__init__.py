"""Stable Stream Dock runtime and application composition API.

Importing this package does not start threads or connect to Stream Dock.
``create_stream_dock_application()`` returns an unstarted application consumed
by ``run_plugin_cli()``.
"""

from .._internal.messaging.inbound import InboundOverflowPolicy
from .application import StreamDockApplication, create_stream_dock_application
from .config import (
    RuntimeDispatcherConfig,
    RuntimeSchedulerKind,
    StreamDockQueueConfig,
    StreamDockShutdownConfig,
)
from .metrics import (
    ActionContextMetrics,
    CommandWriterMetrics,
    EventReaderMetrics,
    HandlerSchedulerMetrics,
    InboundEventQueueMetrics,
    OutboundCommandQueueMetrics,
    RuntimeEventPumpMetrics,
    RuntimeRouterMetrics,
    SessionCoordinatorMetrics,
    StreamDockBoundaryMetrics,
    StreamDockRuntimeMetrics,
    TransportQueueMetrics,
    WebSocketConnectorMetrics,
)
from .ports import (
    ApplicationContext,
    ApplicationRuntime,
    ApplicationService,
    ApplicationServiceFactory,
    DependencyAwareActionRegistry,
    GlobalSettings,
    Plugin,
    PluginHooks,
    RuntimeLifecycle,
    SessionReadiness,
    StreamDockSender,
)

__all__ = [
    "ActionContextMetrics",
    "ApplicationContext",
    "ApplicationRuntime",
    "ApplicationService",
    "ApplicationServiceFactory",
    "CommandWriterMetrics",
    "DependencyAwareActionRegistry",
    "EventReaderMetrics",
    "GlobalSettings",
    "HandlerSchedulerMetrics",
    "InboundEventQueueMetrics",
    "InboundOverflowPolicy",
    "OutboundCommandQueueMetrics",
    "Plugin",
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
    "StreamDockRuntimeMetrics",
    "StreamDockSender",
    "StreamDockShutdownConfig",
    "TransportQueueMetrics",
    "WebSocketConnectorMetrics",
    "create_stream_dock_application",
]

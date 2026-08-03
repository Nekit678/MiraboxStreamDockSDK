"""Pure contracts and models for the experimental runtime dispatcher.

Importing this package does not construct a runtime, start worker threads, or
connect to a Stream Dock boundary. The synchronous dispatcher components only
apply typed state transitions and application callbacks.
"""

from .actions import (
    ActionEventDispatcher,
    BroadcastDispatcher,
    DefaultActionContextManager,
    RuntimeActionIdentityError,
    RuntimeEventDispatchError,
)
from .global_settings import GlobalSettingsCoordinator, GlobalSettingsState
from .ports import (
    RuntimeEventDispatcher,
    RuntimeEventPumpWorker,
    SessionEventCoordinator,
    SessionEventPumpWorker,
    SessionReadiness,
)
from .pumps import (
    RuntimeEventPump,
    RuntimeEventPumpLifecycleError,
    SessionEventPump,
    SessionEventPumpLifecycleError,
)
from .router import NullPluginHooks, RuntimeEventRouter
from .routes import (
    RUNTIME_EVENT_REGISTRY,
    DispatchOrdering,
    RuntimeEventRegistry,
    RuntimeEventRegistryError,
    RuntimeEventRoute,
    RuntimeEventRouteMismatchError,
    RuntimeEventScope,
    RuntimeTransition,
)
from .scheduler import HandlerSchedulerLifecycleError, SequentialHandlerScheduler
from .session import SessionCoordinator, SessionReadinessGate, SessionReadinessStateError

__all__ = [
    "ActionEventDispatcher",
    "BroadcastDispatcher",
    "DefaultActionContextManager",
    "DispatchOrdering",
    "GlobalSettingsCoordinator",
    "GlobalSettingsState",
    "HandlerSchedulerLifecycleError",
    "NullPluginHooks",
    "RUNTIME_EVENT_REGISTRY",
    "RuntimeActionIdentityError",
    "RuntimeEventDispatchError",
    "RuntimeEventDispatcher",
    "RuntimeEventRegistry",
    "RuntimeEventRouter",
    "RuntimeEventRegistryError",
    "RuntimeEventPump",
    "RuntimeEventPumpLifecycleError",
    "RuntimeEventPumpWorker",
    "RuntimeEventRoute",
    "RuntimeEventRouteMismatchError",
    "RuntimeEventScope",
    "RuntimeTransition",
    "SessionCoordinator",
    "SessionEventCoordinator",
    "SessionEventPump",
    "SessionEventPumpLifecycleError",
    "SessionEventPumpWorker",
    "SessionReadiness",
    "SessionReadinessGate",
    "SessionReadinessStateError",
    "SequentialHandlerScheduler",
]

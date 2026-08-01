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

__all__ = [
    "ActionEventDispatcher",
    "BroadcastDispatcher",
    "DefaultActionContextManager",
    "DispatchOrdering",
    "GlobalSettingsCoordinator",
    "GlobalSettingsState",
    "NullPluginHooks",
    "RUNTIME_EVENT_REGISTRY",
    "RuntimeActionIdentityError",
    "RuntimeEventDispatchError",
    "RuntimeEventRegistry",
    "RuntimeEventRouter",
    "RuntimeEventRegistryError",
    "RuntimeEventRoute",
    "RuntimeEventRouteMismatchError",
    "RuntimeEventScope",
    "RuntimeTransition",
]

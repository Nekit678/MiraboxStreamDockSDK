"""Pure contracts and models for the experimental runtime dispatcher.

Importing this package does not construct a runtime, start worker threads, or
connect to a Stream Dock boundary. Concrete dispatcher components are added in
later migration stages.
"""

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
    "DispatchOrdering",
    "RUNTIME_EVENT_REGISTRY",
    "RuntimeEventRegistry",
    "RuntimeEventRegistryError",
    "RuntimeEventRoute",
    "RuntimeEventRouteMismatchError",
    "RuntimeEventScope",
    "RuntimeTransition",
]

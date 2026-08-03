"""Application composition adapters for the runtime dispatcher."""

from .action_registry import ActionRegistryFactoryAdapter, DependencyAwareActionRegistry

__all__ = [
    "ActionRegistryFactoryAdapter",
    "DependencyAwareActionRegistry",
]

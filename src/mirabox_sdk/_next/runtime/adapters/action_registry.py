"""Bind dependency-aware action registries to the runtime factory port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ....json_types import JsonObject
from ....protocols import StreamDockActionDependencies
from ..ports import ActionFactory, RuntimeActionCallbacks


@runtime_checkable
class DependencyAwareActionRegistry(Protocol):
    """Structural view of a registry whose actions share dependencies."""

    def create(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
        dependencies: StreamDockActionDependencies,
    ) -> RuntimeActionCallbacks | None: ...


class ActionRegistryFactoryAdapter(ActionFactory):
    """Bind application dependencies without exposing them to event routing."""

    __slots__ = ("_dependencies", "_registry")

    def __init__(
        self,
        registry: DependencyAwareActionRegistry,
        dependencies: StreamDockActionDependencies,
    ) -> None:
        if not isinstance(registry, DependencyAwareActionRegistry):
            raise TypeError("registry must implement DependencyAwareActionRegistry")
        if not hasattr(dependencies, "stream_dock"):
            raise TypeError("dependencies must provide stream_dock")
        self._registry = registry
        self._dependencies = dependencies

    def create(
        self,
        action_uuid: str,
        context: str,
        initial_settings: JsonObject,
    ) -> RuntimeActionCallbacks | None:
        """Create one action through the bound dependency-aware registry."""

        return self._registry.create(  # type: ignore[attr-defined,no-any-return]
            action_uuid,
            context,
            initial_settings,
            self._dependencies,
        )

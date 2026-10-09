"""Bind dependency-aware action registries to the runtime factory port."""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar, runtime_checkable

from ....action_registry import ActionRegistry
from ....json_types import JsonObject
from ....protocols import StreamDockActionDependencies
from ..ports import ActionFactory, RuntimeActionCallbacks

DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)
RegistryDependenciesT = TypeVar(
    "RegistryDependenciesT", bound=StreamDockActionDependencies, contravariant=True
)


@runtime_checkable
class DependencyAwareActionRegistry(Protocol[RegistryDependenciesT]):
    """Structural view of a registry whose actions share dependencies."""

    def create(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
        dependencies: RegistryDependenciesT,
    ) -> RuntimeActionCallbacks | None: ...


class ActionRegistryFactoryAdapter(ActionFactory, Generic[DependenciesT]):
    """Bind application dependencies without exposing them to event routing."""

    __slots__ = ("_dependencies", "_registry")

    def __init__(
        self,
        registry: DependencyAwareActionRegistry[DependenciesT],
        dependencies: DependenciesT,
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
        """Consume one isolated settings snapshot through the bound registry."""

        if (
            type(self._registry) is ActionRegistry
            and getattr(self._registry.create, "__func__", None) is ActionRegistry.create
        ):
            return self._registry._create_from_owned_settings(
                action_uuid, context, initial_settings, self._dependencies
            )
        return self._registry.create(
            action_uuid,
            context,
            initial_settings,
            self._dependencies,
        )

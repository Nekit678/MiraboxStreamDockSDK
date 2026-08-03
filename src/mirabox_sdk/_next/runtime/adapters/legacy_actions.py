"""Compatibility adapter for the current dependency-aware action registry."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ....json_types import JsonObject
from ....protocols import StreamDockActionDependencies
from ..ports import ActionFactory, RuntimeActionCallbacks


@runtime_checkable
class LegacyActionRegistry(Protocol):
    """Structural view of the current dependency-aware registry."""

    def create(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
        dependencies: StreamDockActionDependencies,
    ) -> RuntimeActionCallbacks | None: ...


class LegacyActionFactoryAdapter(ActionFactory):
    """Bind application dependencies to the current four-argument registry.

    The runtime-owned :class:`ActionFactory` port deliberately accepts only
    event-derived values. The public registry still needs the application
    dependency container as a fourth argument, so composition binds it once
    through this adapter rather than exposing it to routing code.
    """

    __slots__ = ("_dependencies", "_registry")

    def __init__(
        self,
        registry: LegacyActionRegistry,
        dependencies: StreamDockActionDependencies,
    ) -> None:
        if not isinstance(registry, LegacyActionRegistry):
            raise TypeError("registry must implement LegacyActionRegistry")
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
        """Create one current :class:`Action` through the bound registry."""

        return self._registry.create(  # type: ignore[attr-defined,no-any-return]
            action_uuid,
            context,
            initial_settings,
            self._dependencies,
        )


_LegacyActionFactoryAdapter = LegacyActionFactoryAdapter

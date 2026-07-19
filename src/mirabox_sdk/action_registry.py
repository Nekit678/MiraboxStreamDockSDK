"""Per-plugin registry of Stream Dock action classes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from .action import Action
from .json_types import JsonObject
from .protocols import StreamDockActionDependencies

DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)
ActionType = type[Action[Any, Any]]
ActionTypeT = TypeVar("ActionTypeT", bound=Action[Any, Any])


class ActionRegistry(Generic[DependenciesT]):
    """Own action registrations without leaking them between plugin applications."""

    def __init__(self) -> None:
        self._action_types: dict[str, ActionType] = {}

    def register(self, action_uuid: str) -> Callable[[type[ActionTypeT]], type[ActionTypeT]]:
        """Register an Action class for an exact manifest UUID."""

        if not action_uuid.strip():
            raise ValueError("Action UUID must not be empty")

        def decorator(action_type: type[ActionTypeT]) -> type[ActionTypeT]:
            if action_uuid in self._action_types:
                raise ValueError(f"Action is already registered: {action_uuid}")
            if not issubclass(action_type, Action):
                raise TypeError("Registered action must inherit from Action")
            self._action_types[action_uuid] = action_type
            return action_type

        return decorator

    def create(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
        dependencies: DependenciesT,
    ) -> Action[Any, DependenciesT] | None:
        action_type = self._action_types.get(action_uuid)
        if action_type is None:
            return None
        return action_type(
            action_uuid,
            context,
            action_type.decode_settings(settings),
            dependencies,
        )

    @property
    def action_uuids(self) -> frozenset[str]:
        return frozenset(self._action_types)

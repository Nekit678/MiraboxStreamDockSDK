"""Per-plugin registry of Stream Dock action classes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, Never, Protocol, TypeVar, cast

from .action import Action
from .json_types import JsonObject
from .json_types import JsonValue as JsonValue  # Resolve recursive JSON type hints.
from .protocols import StreamDockActionDependencies

DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)
DependenciesT_co = TypeVar("DependenciesT_co", bound=StreamDockActionDependencies, covariant=True)
ActionTypeT = TypeVar("ActionTypeT", bound=Action[Any, Any])


class ActionRegistration(Protocol[DependenciesT_co]):
    """Class decorator that checks dependencies and preserves the action subclass."""

    def __call__(
        self,
        action_type: Callable[[str, str, Never, DependenciesT_co], ActionTypeT],
        /,
    ) -> type[ActionTypeT]:
        """Register an Action whose constructor accepts the registry dependencies.

        ``Never`` leaves the settings parameter unconstrained: the registered
        action's codec supplies its own settings type when creating instances.
        """

        ...


class ActionRegistry(Generic[DependenciesT]):
    """Map manifest action UUIDs to the classes that implement them.

    A registry belongs to one plugin application. Keeping registrations on an
    instance prevents tests and multiple plugin runtimes in the same process
    from leaking action classes into each other.

    ``DependenciesT`` is the dependency-container type accepted by every
    action registered in this instance.
    """

    def __init__(self) -> None:
        """Create an empty action registry."""

        self._action_types: dict[str, type[Action[Any, DependenciesT]]] = {}

    def register(self, action_uuid: str) -> ActionRegistration[DependenciesT]:
        """Return a decorator that registers an action class.

        Args:
            action_uuid: Exact, non-empty UUID declared for the action in
                ``manifest.json``.

        Returns:
            A class decorator that checks constructor dependency compatibility
            and returns the original action class with its concrete type.

        Raises:
            ValueError: If ``action_uuid`` is empty or has already been
                registered in this registry.
            TypeError: If the decorated class does not inherit from
                :class:`Action`.

        Example:
            ``@registry.register("com.example.counter.increment")`` associates
            the decorated ``Action`` subclass with that manifest UUID.
        """

        if not action_uuid.strip():
            raise ValueError("Action UUID must not be empty")

        def decorator(
            action_type: Callable[[str, str, Never, DependenciesT], ActionTypeT],
        ) -> type[ActionTypeT]:
            if action_uuid in self._action_types:
                raise ValueError(f"Action is already registered: {action_uuid}")
            if not isinstance(action_type, type) or not issubclass(action_type, Action):
                raise TypeError("Registered action must inherit from Action")
            self._action_types[action_uuid] = action_type
            # Constructor typing checks dependencies; the runtime check above
            # establishes that this callable is the original Action class.
            return cast(type[ActionTypeT], action_type)

        return decorator

    def create(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
        dependencies: DependenciesT,
    ) -> Action[Any, DependenciesT] | None:
        """Create the action instance registered for a Stream Dock context.

        Args:
            action_uuid: Exact action UUID received in ``willAppear``.
            context: Opaque identifier for the concrete action instance.
            settings: Raw settings received with the appearance event.
            dependencies: Application dependency container passed to the
                action constructor.

        Returns:
            A new action with decoded settings, or ``None`` when the UUID is not
            registered.

        Raises:
            JsonCodecDecodeError: If the registered action's settings codec
                rejects ``settings``.
        """

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
        """Return an immutable snapshot of all registered action UUIDs."""

        return frozenset(self._action_types)

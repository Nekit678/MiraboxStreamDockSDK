"""Per-plugin registry of Stream Dock action classes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, Protocol, TypeVar, cast, overload

from .action import Action
from .codecs import JsonCodec, _decode_isolated_with_codec
from .json_types import JsonObject
from .json_types import JsonValue as JsonValue  # Resolve recursive JSON type hints.
from .protocols import StreamDockActionDependencies

DependenciesT = TypeVar("DependenciesT", bound=StreamDockActionDependencies)
DependenciesT_co = TypeVar("DependenciesT_co", bound=StreamDockActionDependencies, covariant=True)
ActionTypeT = TypeVar("ActionTypeT", bound=Action[Any, Any])
SettingsT = TypeVar("SettingsT")
ClassDependenciesT_contra = TypeVar(
    "ClassDependenciesT_contra", bound=StreamDockActionDependencies, contravariant=True
)
ClassActionT_co = TypeVar("ClassActionT_co", bound=Action[Any, Any], covariant=True)


class ActionClass(Protocol[SettingsT, ClassDependenciesT_contra, ClassActionT_co]):
    """Structural class contract tying a settings codec to an action constructor."""

    def get_settings_codec(self) -> JsonCodec[SettingsT]:
        """Return the codec for the action's declared settings type."""

        ...

    def __call__(
        self,
        action: str,
        context: str,
        settings: SettingsT,
        dependencies: ClassDependenciesT_contra,
        /,
    ) -> ClassActionT_co:
        """Construct the concrete action with decoded settings and dependencies."""

        ...


class ActionRegistration(Protocol[DependenciesT_co]):
    """Decorator for JSON settings that preserves the action subclass and dependencies."""

    def __call__(
        self,
        action_type: ActionClass[JsonObject, DependenciesT_co, ActionTypeT],
        /,
    ) -> type[ActionTypeT]:
        """Register an Action accepting JSON settings and the registry dependencies."""

        ...


class TypedActionRegistration(Protocol[SettingsT, DependenciesT_co]):
    """Decorator binding a codec's settings type while preserving the action subclass."""

    def __call__(
        self,
        action_type: ActionClass[SettingsT, DependenciesT_co, ActionTypeT],
        /,
    ) -> type[ActionTypeT]:
        """Register an Action accepting the codec's settings and registry dependencies."""

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

    @overload
    def register(self, action_uuid: str) -> ActionRegistration[DependenciesT]: ...

    @overload
    def register(
        self,
        action_uuid: str,
        *,
        settings_codec: JsonCodec[SettingsT],
    ) -> TypedActionRegistration[SettingsT, DependenciesT]: ...

    def register(
        self,
        action_uuid: str,
        *,
        settings_codec: JsonCodec[SettingsT] | None = None,
    ) -> Callable[[ActionClass[Any, DependenciesT, ActionTypeT]], type[ActionTypeT]]:
        """Return a decorator that registers an action class.

        Args:
            action_uuid: Exact, non-empty UUID declared for the action in
                ``manifest.json``.
            settings_codec: Codec matching the action's declared settings
                type. Required for settings other than :class:`JsonObject`;
                registration assigns it to the original action class.

        Returns:
            A decorator that checks settings and dependency compatibility and
            returns the original action class with its concrete type.

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
            action_type: ActionClass[Any, DependenciesT, ActionTypeT],
        ) -> type[ActionTypeT]:
            if action_uuid in self._action_types:
                raise ValueError(f"Action is already registered: {action_uuid}")
            if not isinstance(action_type, type) or not issubclass(action_type, Action):
                raise TypeError("Registered action must inherit from Action")
            if settings_codec is not None:
                # Replace the descriptor; direct assignment is typed as the descriptor itself.
                setattr(action_type, "settings_codec", settings_codec)  # noqa: B010
            self._action_types[action_uuid] = action_type
            # Codec and constructor typing check settings and dependencies; the check above
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

    def _create_from_owned_settings(
        self,
        action_uuid: str,
        context: str,
        settings: JsonObject,
        dependencies: DependenciesT,
    ) -> Action[Any, DependenciesT] | None:
        """Consume the runtime factory's validated, isolated settings snapshot."""

        action_type = self._action_types.get(action_uuid)
        if action_type is None:
            return None
        # Preserve plugin overrides; only the default decoder's copy is redundant.
        if getattr(action_type.decode_settings, "__func__", None) is not getattr(
            Action.decode_settings, "__func__", None
        ):
            return self.create(action_uuid, context, settings, dependencies)
        return action_type(
            action_uuid,
            context,
            _decode_isolated_with_codec(settings, action_type.settings_codec),
            dependencies,
        )

    @property
    def action_uuids(self) -> frozenset[str]:
        """Return an immutable snapshot of all registered action UUIDs."""

        return frozenset(self._action_types)

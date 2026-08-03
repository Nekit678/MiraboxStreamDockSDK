"""Stable application composition for the Stream Dock runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from .._next.boundary.config import BoundaryQueueConfig, BoundaryShutdownConfig
from .._next.boundary.ports import WebSocketConnectorFactory
from .._next.messaging.inbound import InboundOverflowPolicy
from .._next.runtime.adapters import DependencyAwareActionRegistry
from .._next.runtime.composition import (
    HandlerSchedulerFactory,
    StreamDockRuntime,
    create_stream_dock_runtime,
)
from .._next.runtime.config import RuntimeDispatcherConfig
from .._next.runtime.metrics import StreamDockRuntimeMetrics
from .._next.runtime.ports import ActionFactory, PluginHooks, RuntimeLifecycle
from ..codecs import JsonCodec
from ..json_types import JsonObject
from ..protocols import StreamDockActionDependencies, StreamDockSender
from ..registration import PluginLaunchArguments

_DEFAULT_QUEUE_LIMIT = 1024
_DEFAULT_SESSION_QUEUE_LIMIT = 16

GlobalSettingsT = TypeVar("GlobalSettingsT")


class StreamDockApplication:
    """Executable application facade over the typed boundary and dispatcher."""

    __slots__ = ("_runtime",)

    def __init__(self, runtime: StreamDockRuntime) -> None:
        if not isinstance(runtime, RuntimeLifecycle):
            raise TypeError("runtime must implement RuntimeLifecycle")
        self._runtime = runtime

    @property
    def runtime(self) -> StreamDockRuntime:
        """Return the runtime facade for state and diagnostic inspection."""

        return self._runtime

    def run(self) -> None:
        """Run the application until disconnect, close, or fatal failure."""

        self._runtime.run_forever()

    def stop(self) -> None:
        """Idempotently request graceful shutdown."""

        self._runtime.close()

    def metrics(self) -> StreamDockRuntimeMetrics:
        """Return an immutable aggregate runtime and boundary snapshot."""

        return self._runtime.metrics()

    @property
    def global_settings(self) -> JsonObject:
        """Return an isolated snapshot of current plugin-wide settings."""

        return self._runtime.global_settings

    def update_global_settings(self, update: Callable[[JsonObject], None]) -> None:
        """Persist and commit one rollback-safe global-settings transaction."""

        self._runtime.update_global_settings(update)

    def set_global_settings(self, settings: JsonObject) -> None:
        """Persist raw plugin-wide settings and update replay state."""

        self._runtime.set_global_settings(settings)

    def set_typed_global_settings(
        self,
        settings: GlobalSettingsT,
        codec: JsonCodec[GlobalSettingsT],
    ) -> None:
        """Persist typed plugin-wide settings and update replay state."""

        self._runtime.set_typed_global_settings(settings, codec)


def create_stream_dock_application(
    launch_arguments: PluginLaunchArguments,
    *,
    action_factory: ActionFactory | DependencyAwareActionRegistry,
    action_dependencies_factory: (
        Callable[[StreamDockSender], StreamDockActionDependencies] | None
    ) = None,
    plugin_hooks: PluginHooks | None = None,
    queue_config: BoundaryQueueConfig | None = None,
    shutdown_config: BoundaryShutdownConfig | None = None,
    runtime_config: RuntimeDispatcherConfig | None = None,
    scheduler_factory: HandlerSchedulerFactory | None = None,
    inbound_overflow_policy: InboundOverflowPolicy = InboundOverflowPolicy.DROP_NEWEST,
    coalesce_dial_rotations: bool = False,
    coalesce_commands: bool = False,
    connector_factory: WebSocketConnectorFactory | None = None,
) -> StreamDockApplication:
    """Build one unstarted application over the production runtime stack.

    ``ActionRegistry`` users provide ``action_dependencies_factory``; it is
    called once with the canonical :class:`StreamDockSender`. Native
    three-argument :class:`ActionFactory` implementations leave it unset.
    """

    if not isinstance(launch_arguments, PluginLaunchArguments):
        raise TypeError("launch_arguments must be PluginLaunchArguments")
    if action_dependencies_factory is not None and not callable(action_dependencies_factory):
        raise TypeError("action_dependencies_factory must be callable or None")

    from .._next.boundary.composition import create_stream_dock_boundary

    resolved_queue_config = queue_config or BoundaryQueueConfig(
        raw_inbound_limit=_DEFAULT_QUEUE_LIMIT,
        inbound_event_limit=_DEFAULT_QUEUE_LIMIT,
        outbound_command_limit=_DEFAULT_QUEUE_LIMIT,
        raw_outbound_limit=_DEFAULT_QUEUE_LIMIT,
        session_event_limit=_DEFAULT_SESSION_QUEUE_LIMIT,
    )
    boundary = create_stream_dock_boundary(
        launch_arguments.port,
        resolved_queue_config,
        shutdown_config=shutdown_config,
        connector_factory=connector_factory,
        inbound_overflow_policy=inbound_overflow_policy,
        coalesce_dial_rotations=coalesce_dial_rotations,
        coalesce_commands=coalesce_commands,
    )
    action_dependencies = (
        action_dependencies_factory(boundary.commands)
        if action_dependencies_factory is not None
        else None
    )
    runtime = create_stream_dock_runtime(
        launch_arguments,
        boundary=boundary,
        action_factory=action_factory,
        action_dependencies=action_dependencies,
        plugin_hooks=plugin_hooks,
        config=runtime_config,
        scheduler_factory=scheduler_factory,
    )
    return StreamDockApplication(runtime)


__all__ = [
    "StreamDockApplication",
    "create_stream_dock_application",
]

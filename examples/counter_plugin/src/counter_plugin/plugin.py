"""Counter plugin composition around the reusable SDK runtime."""

from __future__ import annotations

from mirabox_sdk import PluginLaunchArguments, StreamDockConnection, StreamDockPlugin

from . import actions as _actions  # noqa: F401
from .action_registry import ACTION_REGISTRY
from .contracts import ActionDependencies


class Plugin(StreamDockPlugin[ActionDependencies]):
    def __init__(
        self,
        arguments: PluginLaunchArguments,
        *,
        stream_dock: StreamDockConnection,
    ) -> None:
        super().__init__(
            arguments,
            stream_dock=stream_dock,
            action_registry=ACTION_REGISTRY,
            action_dependencies=ActionDependencies(stream_dock),
        )

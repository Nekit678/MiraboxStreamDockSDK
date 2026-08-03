"""Composition root for the counter example plugin."""

from __future__ import annotations

import os

from mirabox_sdk import PluginApplication, PluginLaunchArguments, WebSocketStreamDockConnection

from .action_registry import ACTION_REGISTRY
from .contracts import ActionDependencies
from .plugin import Plugin

EXPERIMENTAL_BOUNDARY_ENV = "MIRABOX_SDK_EXPERIMENTAL_BOUNDARY"
EXPERIMENTAL_RUNTIME_ENV = "MIRABOX_SDK_EXPERIMENTAL_RUNTIME"


def build_application(arguments: PluginLaunchArguments) -> PluginApplication:
    if os.environ.get(EXPERIMENTAL_RUNTIME_ENV) == "1":
        from mirabox_sdk.experimental import create_experimental_stream_dock_application

        return create_experimental_stream_dock_application(
            arguments,
            action_factory=ACTION_REGISTRY,
            action_dependencies_factory=ActionDependencies,
        )
    if os.environ.get(EXPERIMENTAL_BOUNDARY_ENV) == "1":
        from mirabox_sdk.experimental import create_experimental_stream_dock_connection

        connection = create_experimental_stream_dock_connection(arguments.port)
    else:
        connection = WebSocketStreamDockConnection(arguments.port)
    return Plugin(arguments, stream_dock=connection)

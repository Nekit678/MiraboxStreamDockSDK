"""Composition root for the counter example plugin."""

from __future__ import annotations

import os

from mirabox_sdk import PluginLaunchArguments, WebSocketStreamDockConnection

from .plugin import Plugin

EXPERIMENTAL_BOUNDARY_ENV = "MIRABOX_SDK_EXPERIMENTAL_BOUNDARY"


def build_application(arguments: PluginLaunchArguments) -> Plugin:
    if os.environ.get(EXPERIMENTAL_BOUNDARY_ENV) == "1":
        from mirabox_sdk.experimental import create_experimental_stream_dock_connection

        connection = create_experimental_stream_dock_connection(arguments.port)
    else:
        connection = WebSocketStreamDockConnection(arguments.port)
    return Plugin(arguments, stream_dock=connection)

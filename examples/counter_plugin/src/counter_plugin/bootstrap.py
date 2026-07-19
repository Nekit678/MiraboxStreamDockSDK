"""Composition root for the counter example plugin."""

from __future__ import annotations

from mirabox_sdk import PluginLaunchArguments, WebSocketStreamDockConnection

from .plugin import Plugin


def build_application(arguments: PluginLaunchArguments) -> Plugin:
    connection = WebSocketStreamDockConnection(arguments.port)
    return Plugin(arguments, stream_dock=connection)

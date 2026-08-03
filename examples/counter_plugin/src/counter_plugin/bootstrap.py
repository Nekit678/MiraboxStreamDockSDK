"""Composition root for the counter example plugin."""

from __future__ import annotations

from mirabox_sdk import (
    PluginApplication,
    PluginLaunchArguments,
    create_stream_dock_application,
)

from .action_registry import ACTION_REGISTRY
from .contracts import ActionDependencies


def build_application(arguments: PluginLaunchArguments) -> PluginApplication:
    return create_stream_dock_application(
        arguments,
        action_factory=ACTION_REGISTRY,
        action_dependencies_factory=ActionDependencies,
    )

"""Composition shared by source runs, the executable and harness tests."""

from __future__ import annotations

import logging

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    JsonObject,
    KeyDownEvent,
    PluginLaunchArguments,
    RuntimeDispatcherConfig,
    SdkDiagnostic,
    StreamDockApplication,
    WillAppearEvent,
    create_stream_dock_application,
)

from .service import HeartbeatService

PLUGIN_UUID = "com.example.heartbeat"
ACTION_UUID = f"{PLUGIN_UUID}.status"
ACTION_REGISTRY = ActionRegistry[ApplicationContext]()
logger = logging.getLogger("mirabox_sdk.examples.heartbeat")


@ACTION_REGISTRY.register(ACTION_UUID)
class StatusAction(Action[JsonObject, ApplicationContext]):
    def on_will_appear(self, event: WillAppearEvent) -> None:
        self.set_title("Heartbeat")

    def on_key_down(self, event: KeyDownEvent) -> None:
        self.set_title("Pressed")


def observe_error(diagnostic: SdkDiagnostic) -> None:
    # Managed SDK logging queues these metadata-only records. Never log diagnostic.error.
    logger.error(
        "Observed SDK error; category=%s callback=%s exception_type=%s",
        diagnostic.category,
        diagnostic.callback,
        diagnostic.exception_type,
    )


def build_application(arguments: PluginLaunchArguments) -> StreamDockApplication:
    return create_stream_dock_application(
        arguments,
        action_factory=ACTION_REGISTRY,
        action_dependencies_factory=lambda context: context,
        service_factories=(HeartbeatService,),
        error_observer=observe_error,
        runtime_config=RuntimeDispatcherConfig(
            callback_drain_timeout=1.0,
            shutdown_timeout=5.0,
        ),
    )

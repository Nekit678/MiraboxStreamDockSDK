"""Static consumer contract for the supported top-level SDK namespace."""

from __future__ import annotations

from typing import TypeVar

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationRuntime,
    ApplicationService,
    GlobalSettings,
    JsonCodec,
    JsonObject,
    Plugin,
    PluginLaunchArguments,
    StreamDockActionDependencies,
    StreamDockApplication,
    StreamDockRuntimeMetrics,
    StreamDockSender,
    SystemDidWakeUpEvent,
    create_stream_dock_application,
)

SettingsT = TypeVar("SettingsT")


class Dependencies(StreamDockActionDependencies):
    def __init__(self, stream_dock: StreamDockSender) -> None:
        self.stream_dock = stream_dock


class ExampleService(ApplicationService):
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class ExamplePlugin(Plugin):
    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
        del event


registry = ActionRegistry[Dependencies]()


@registry.register("com.example.typed.action")
class ExampleAction(Action[JsonObject, Dependencies]):
    pass


def build_application(arguments: PluginLaunchArguments) -> StreamDockApplication:
    return create_stream_dock_application(
        arguments,
        action_factory=registry,
        action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
        plugin=ExamplePlugin(),
        service_factories=(lambda _context: ExampleService(),),
    )


class ApplicationRuntimeFake:
    @property
    def global_settings(self) -> GlobalSettings:
        raise RuntimeError("typing-only fake")

    def run_forever(self) -> None:
        return None

    def close(self) -> None:
        return None

    def metrics(self) -> StreamDockRuntimeMetrics:
        raise RuntimeError("typing-only fake")

    def update_global_settings(self, update: object) -> None:
        del update

    def set_global_settings(self, settings: JsonObject) -> None:
        del settings

    def set_typed_global_settings(
        self,
        settings: SettingsT,
        codec: JsonCodec[SettingsT],
    ) -> None:
        del settings, codec


runtime: ApplicationRuntime = ApplicationRuntimeFake()
application = StreamDockApplication(runtime)

"""Static consumer contract for the supported top-level SDK namespace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar, assert_type

from mirabox_sdk import (
    Action,
    ActionRegistry,
    ApplicationContext,
    ApplicationRuntime,
    ApplicationService,
    FunctionalJsonCodec,
    GlobalSettings,
    JsonCodec,
    JsonObject,
    LogMessageCommand,
    Plugin,
    PluginLaunchArguments,
    SdkDiagnostic,
    SourceLocation,
    StreamDockActionDependencies,
    StreamDockApplication,
    StreamDockRuntimeMetrics,
    StreamDockSender,
    SystemDidWakeUpEvent,
    create_stream_dock_application,
)
from mirabox_sdk.testing import FakeStreamDockSender, StreamDockHarness

SettingsT = TypeVar("SettingsT")


@dataclass(frozen=True)
class Dependencies(StreamDockActionDependencies):
    stream_dock: StreamDockSender


class ExampleService(ApplicationService):
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class ExamplePlugin(Plugin):
    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    def on_ready(self) -> None:
        self.context.stream_dock.send(LogMessageCommand("session is ready"))

    def on_stop(self) -> None:
        return None

    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
        del event


registry = ActionRegistry[Dependencies]()


@registry.register("com.example.typed.action")
class ExampleAction(Action[JsonObject, Dependencies]):
    def read_context(self) -> str:
        return self.context


assert_type(registry.register("com.example.typed.alias")(ExampleAction), type[ExampleAction])


@dataclass(frozen=True)
class CustomSettings:
    label: str


def decode_settings(settings: JsonObject) -> CustomSettings:
    label = settings.get("label", "")
    if not isinstance(label, str):
        raise ValueError("label must be a string")
    return CustomSettings(label)


def encode_settings(settings: CustomSettings) -> JsonObject:
    return {"label": settings.label}


@registry.register("com.example.typed.settings")
class CustomSettingsAction(Action[CustomSettings, Dependencies]):
    settings_codec = FunctionalJsonCodec[CustomSettings](decode_settings, encode_settings)

    def read_label(self) -> str:
        return self.settings.label


def check_registered_classes(dependencies: Dependencies) -> None:
    action = ExampleAction("com.example.typed.action", "button", {}, dependencies)
    assert_type(action, ExampleAction)
    assert_type(action.read_context(), str)
    assert_type(action.dependencies, Dependencies)
    typed_action = CustomSettingsAction(
        "com.example.typed.settings", "button", CustomSettings("label"), dependencies
    )
    assert_type(typed_action, CustomSettingsAction)
    assert_type(typed_action.settings, CustomSettings)
    assert_type(typed_action.read_label(), str)


def observe_error(diagnostic: SdkDiagnostic) -> None:
    assert_type(diagnostic.error, Exception)
    assert_type(diagnostic.field_path, tuple[str | int, ...])
    assert_type(diagnostic.source_locations, tuple[SourceLocation, ...])


def build_application(arguments: PluginLaunchArguments) -> StreamDockApplication:
    return create_stream_dock_application(
        arguments,
        action_factory=registry,
        action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
        plugin_factory=ExamplePlugin,
        service_factories=(lambda _context: ExampleService(),),
        error_observer=observe_error,
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
assert_type(application.global_settings.loaded, bool)
assert_type(application.global_settings.snapshot(), JsonObject)
assert_type(application.metrics(), StreamDockRuntimeMetrics)


def build_test_harness(arguments: PluginLaunchArguments) -> StreamDockHarness:
    sender: StreamDockSender = FakeStreamDockSender()
    sender.send(LogMessageCommand("unit test"))
    return StreamDockHarness(
        arguments,
        action_factory=registry,
        action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
        plugin_factory=ExamplePlugin,
        error_observer=observe_error,
    )

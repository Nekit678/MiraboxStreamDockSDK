# Stream Dock protocol map

This document maps the public MiraBox Stream Dock plugin protocol to the Python
API implemented by `mirabox-stream-dock-sdk`. It is a compatibility map, not a
replacement for the upstream protocol specification.

The supported application model starts with `StreamDockApplication`. Import
public names only from `mirabox_sdk`; `mirabox_sdk._internal` is an implementation
namespace and not an application extension point. The complete, tested plugin
in [`examples/counter_plugin`](../examples/counter_plugin) is the executable
companion to this map.

The supported import paths are `mirabox_sdk` and `mirabox_sdk.runtime`, with
testing helpers in `mirabox_sdk.testing`. Version `0.5.0` removes the unsupported
direct legacy runtime modules and the temporary `mirabox_sdk._next` namespace
from distributions.

## Current implementation

The source checkout has one production stack. The earlier boundary, contract
parity, and runtime dispatcher design documents were removed after the legacy
and `_next` implementations were retired. Use this map for the current protocol
contract and the following sources for implementation details:

| Responsibility | Current source |
|---|---|
| Application composition, shared dependencies, and managed services | [`runtime/application.py`](../src/mirabox_sdk/runtime/application.py) |
| Public runtime contracts and configuration | [`runtime/ports.py`](../src/mirabox_sdk/runtime/ports.py), [`runtime/config.py`](../src/mirabox_sdk/runtime/config.py) |
| Boundary composition and shutdown | [`_internal/boundary/composition.py`](../src/mirabox_sdk/_internal/boundary/composition.py) |
| WebSocket transport and raw queues | [`_internal/transport/`](../src/mirabox_sdk/_internal/transport) |
| Protocol decoding and command encoding | [`_internal/protocol/`](../src/mirabox_sdk/_internal/protocol) |
| Typed event and command queues, reader, and writer | [`_internal/messaging/`](../src/mirabox_sdk/_internal/messaging) |
| Runtime lifecycle, session initialization, routing, and scheduling | [`_internal/runtime/`](../src/mirabox_sdk/_internal/runtime) |

These source links do not make `_internal` a supported plugin import path.
Boundary contract tests live in [`tests/internal/`](../tests/internal), runtime
dispatcher tests in [`tests/internal/runtime/`](../tests/internal/runtime), and
public application and testing-helper checks in
[`tests/test_application.py`](../tests/test_application.py) and
[`tests/test_testing.py`](../tests/test_testing.py). Current test commands and
the scheduler benchmark are documented in
[`CONTRIBUTING.md`](../CONTRIBUTING.md#checks); release verification is documented
in [`RELEASING.md`](../RELEASING.md#preparing-a-release).

## Sources

The implementation is based primarily on:

1. [Official StreamDock Plugin SDK repository](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK)
2. [Official SDK documentation](https://sdk.key123.vip/en/)
3. [Registration procedure](https://sdk.key123.vip/en/guide/registration.html)
4. [Received events](https://sdk.key123.vip/en/guide/events-received.html)
5. [Events sent](https://sdk.key123.vip/en/guide/events-sent.html)
6. [`manifest.json` reference](https://sdk.key123.vip/en/guide/manifest.html)
7. [Property Inspector guide](https://sdk.key123.vip/en/guide/property-inspector.html)
8. [Upstream Python template](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK/tree/main/SDPythonSDK)

The [DeepWiki overview](https://deepwiki.com/MiraboxSpace/StreamDock-Plugin-SDK)
is useful as a generated guide to the upstream repository, but the official
documentation, repository source, and observed wire behavior take precedence.

The declared minimum compatible version is Stream Dock `2.10.179.426`, matching
the upstream manifest reference and this repository's example plugin. The
latest recorded manual runtime verification was performed with Stream Dock
`3.10.203.0701`. During the 2026-07-28 experimental-boundary verification, that
installed build supplied `2.10.179.426` in `-info.application.version`; callers
must treat the launch field as host-provided compatibility metadata rather than
derive the installed application version from it. Automated tests simulate the
protocol and do not require a physical Stream Dock device.

## Executable registration

Stream Dock starts a compiled plugin with four named arguments:

| Wire argument | SDK representation |
|---|---|
| `-port` | `PluginLaunchArguments.port` |
| `-pluginUUID` | `PluginLaunchArguments.plugin_uuid` |
| `-registerEvent` | `PluginLaunchArguments.register_event` |
| `-info` | `PluginLaunchArguments.info` / `RegistrationInfo` |

Use `parse_plugin_cli_arguments()` to validate those arguments or
`run_plugin_cli()` to parse them and manage the application lifecycle. After the
WebSocket opens, the `StreamDockApplication` runtime sends
`RegisterPluginCommand` with the runtime-provided event name and plugin UUID.
It then sends `GetGlobalSettingsCommand` as part of session initialization.
Plugin code does not send either startup command itself.

`create_stream_dock_application()` creates an unstarted typed boundary and
runtime. The boundary parses and validates WebSocket messages, while the runtime
owns action contexts, global settings, and the immutable route registry for all
known event types. Applications register action UUIDs with `ActionRegistry`;
they do not configure protocol parsers or runtime routes.

```python
from __future__ import annotations

from dataclasses import dataclass

from mirabox_sdk import (
    Action,
    ActionRegistry,
    JsonObject,
    PluginLaunchArguments,
    StreamDockApplication,
    StreamDockSender,
    create_stream_dock_application,
    run_plugin_cli,
)


@dataclass(frozen=True, slots=True)
class Dependencies:
    stream_dock: StreamDockSender


registry: ActionRegistry[Dependencies] = ActionRegistry()


@registry.register("com.example.plugin.action")
class ExampleAction(Action[JsonObject, Dependencies]):
    pass


def build_application(arguments: PluginLaunchArguments) -> StreamDockApplication:
    return create_stream_dock_application(
        arguments,
        action_factory=registry,
        action_dependencies_factory=lambda ctx: Dependencies(ctx.stream_dock),
    )


if __name__ == "__main__":
    raise SystemExit(run_plugin_cli(build_application))
```

`action_dependencies_factory` always receives the shared `ApplicationContext`;
parameter names and annotations do not affect dispatch. Sender-only factories
can be wrapped as shown above or temporarily passed through the deprecated
`legacy_action_dependencies_factory` parameter, which emits
`DeprecationWarning`. The two dependency-factory parameters are mutually
exclusive.

## Events received by a plugin

The boundary parses known messages with `parse_stream_dock_event()`. The
resulting model is dispatched to the corresponding `Action` callback where
applicable.

| Wire event | Python model | `Action` callback or runtime effect |
|---|---|---|
| `willAppear` | `WillAppearEvent` | Creates the context, then `on_will_appear()` |
| `willDisappear` | `WillDisappearEvent` | Removes the context, then calls `on_will_disappear()` |
| `didReceiveSettings` | `DidReceiveSettingsEvent` | Updates typed settings, then `on_did_receive_settings()` |
| `didReceiveGlobalSettings` | `DidReceiveGlobalSettingsEvent` | Updates runtime state, then notifies `Plugin` and active actions through `on_did_receive_global_settings()`; the latest event is replayed to actions created later |
| `titleParametersDidChange` | `TitleParametersDidChangeEvent` | Updates title state, then `on_title_parameters_did_change()` |
| `keyDown` | `KeyDownEvent` | `on_key_down()` |
| `keyUp` | `KeyUpEvent` | `on_key_up()` |
| `dialDown` | `DialDownEvent` | `on_dial_down()` |
| `dialUp` | `DialUpEvent` | `on_dial_up()` |
| `dialRotate` | `DialRotateEvent` | `on_dial_rotate()` |
| `propertyInspectorDidAppear` | `PropertyInspectorDidAppearEvent` | `on_property_inspector_did_appear()` |
| `propertyInspectorDidDisappear` | `PropertyInspectorDidDisappearEvent` | `on_property_inspector_did_disappear()` |
| `sendToPlugin` | `SendToPluginEvent` | `on_send_to_plugin()` |
| `deviceDidConnect` | `DeviceDidConnectEvent` | Broadcasts `on_device_did_connect()` |
| `deviceDidDisconnect` | `DeviceDidDisconnectEvent` | Broadcasts `on_device_did_disconnect()` |
| `applicationDidLaunch` | `ApplicationDidLaunchEvent` | Broadcasts `on_application_did_launch()` |
| `applicationDidTerminate` | `ApplicationDidTerminateEvent` | Broadcasts `on_application_did_terminate()` |
| `systemDidWakeUp` | `SystemDidWakeUpEvent` | Broadcasts `on_system_did_wake_up()` |

`touchTap` is also implemented as `TouchTapEvent` and dispatched through
`on_touch_tap()`. It has been retained from observed Stream Dock protocol
behavior even though it is not currently listed on the upstream “Received
Events” page.

Known events are parsed into typed immutable models before the runtime dispatches
them. `parse_stream_dock_event()` remains available for tests and advanced input
validation, but a normal plugin does not call it for WebSocket traffic.
For application integration tests, `mirabox_sdk.testing.StreamDockHarness`
provides in-memory session initialization, JSON/event injection and outbound
wire assertions using the production pipeline. Its context manager waits for
readiness and shuts down the application; `FakeStreamDockSender` supports
isolated action tests. See [Testing plugins](../README.md#testing-plugins).
Transport and scheduler injection are private SDK implementation details.

An unknown event is preserved as `UnknownStreamDockEvent` by default and
delivered once to `Plugin.on_unhandled_event()`. Subclass `Plugin` and pass an
instance as `plugin=` to `create_stream_dock_application()` to observe known
plugin-wide broadcasts and unknown events. `PluginHooks` remains available only
for an existing unknown-event-only integration. Unknown events are not broadcast
to actions because an SDK version that does not recognize an event cannot safely
infer its action or broadcast scope. Pass `allow_unknown=False` to
`parse_stream_dock_event()` when strict rejection with `UnsupportedEventError`
is preferable.

For each known plugin-wide broadcast, the `Plugin` callback runs before a stable
snapshot of active action callbacks. A failing callback is logged and isolated
so the other recipients still receive the broadcast. Action callbacks are serial
for one context and may overlap for different contexts; lifecycle, broadcast,
and unknown events are ordering barriers.

## Global settings

`application.global_settings` is the canonical runtime-owned `GlobalSettings`
facade. The same object is supplied as `ApplicationContext.global_settings` to
context-aware action-dependency and service factories. Incoming global-settings
events replace its state before callbacks; a successfully sent local write also
replaces its state and becomes the value replayed to actions that appear later.

The facade exposes no mutable `settings` property; use `snapshot()` to read an
isolated copy. Mutating that return value never changes runtime state and must
not be used as a write mechanism. The permitted writes
are `global_settings.update(callback)` for one rollback-safe mutation of an
isolated draft, `global_settings.set(settings)` for a complete raw JSON object,
and `global_settings.set_typed(settings, codec)` for a complete typed value. If
the callback, validation, or `setGlobalSettings` command fails, the previous
local state remains unchanged. `global_settings.loaded` becomes true only after
a received or successfully persisted value exists. Each recipient of
`DidReceiveGlobalSettingsEvent` receives an isolated settings view, so one
callback cannot change the canonical value or another callback's event.

## Events sent by a plugin

Commands can be constructed directly and passed to `StreamDockSender.send()`
or `send_async()`. The latter returns a `CommandFuture` without waiting for
serialization or WebSocket I/O. `Action` methods cover ordinary context-scoped
commands, while `application.global_settings` is the preferred global-settings
write facade.

| Wire event | Command model | Convenience API |
|---|---|---|
| Runtime registration event | `RegisterPluginCommand` | Sent by the runtime on connection |
| `setSettings` | `SetSettingsCommand` | `Action.set_settings()` |
| `getSettings` | `GetSettingsCommand` | `Action.get_settings()` |
| `setGlobalSettings` | `SetGlobalSettingsCommand` | `application.global_settings.update()`, `set()`, or `set_typed()` |
| `getGlobalSettings` | `GetGlobalSettingsCommand` | Sent by the runtime during session initialization |
| `setTitle` | `SetTitleCommand` | `Action.set_title()` / `set_title_async()` |
| `setImage` | `SetImageCommand` | `Action.set_image()` / `set_image_async()` |
| `setState` | `SetStateCommand` | `Action.set_state()` / `set_state_async()` |
| `showOk` | `ShowOkCommand` | `Action.show_ok()` |
| `showAlert` | `ShowAlertCommand` | `Action.show_alert()` |
| `openUrl` | `OpenUrlCommand` | `Action.open_url()` |
| `logMessage` | `LogMessageCommand` | `Action.log_message()` |
| `sendToPropertyInspector` | `SendToPropertyInspectorCommand` | `Action.send_to_property_inspector()` / `send_typed_to_property_inspector()` |

All command models expose `to_wire()` for the exact JSON object sent through
the WebSocket. `SetSettingsCommand`, `SetGlobalSettingsCommand`, and
`SendToPropertyInspectorCommand` retain extensible JSON as an
`OwnedJsonPayload` backed by one `ValidatedJsonObject` snapshot. Their
`to_validated_wire()` implementations compose a `ValidatedWireMessage` without
another recursive payload pass. Custom `StreamDockCommand` implementations can
continue to implement only `to_wire()`; the inherited `to_validated_wire()`
validates and owns that output before the transport receives it. The transport
therefore serializes one uniform validated-message contract and retains
`allow_nan=False` as a final encoder safeguard.

One connection-owned outbound writer performs validation, serialization,
logging, and WebSocket writes in FIFO queue order. The bounded queue rejects
overflow explicitly instead of silently dropping protocol commands. Optional
coalescing replaces only compatible adjacent pending state-setting commands,
so intervening commands remain ordering barriers. `send()` waits for the
writer's result and propagates serialization and transport errors to its caller.

`send()` supports overlapping calls from action callbacks and application
services after the outbound writer starts. Before the writer starts, both
`send()` and `send_async()` fail immediately with
`OutboundCommandBusNotReadyError` and do not queue a command. In particular,
services must not send commands or wait for the session from
`ApplicationService.start()`: that phase runs before the runtime begins.
Session-aware service workers receive `ApplicationContext.session_readiness`;
they may wait there and submit commands only after `wait()` returns `True`.
That signal opens after connection, registration, and the initial global
settings request complete, but does not wait for the first settings response.
Scalar-only frozen commands may be shared between threads. A mutable
`OwnedJsonPayload` must have one owner at a time and must not change after any
thread submits its command. The same single-owner rule applies to mutable COW
event and settings views; immutable `ValidatedJsonObject` backing snapshots may
be handed between threads.

Per-message protocol logs are emitted only at DEBUG and contain routing metadata
such as the event and context. Message payloads are redacted by default because
settings and Property Inspector messages may contain secrets under plugin-defined
field names. Pass `include_payload=True` to `configure_logging()` to include
complete messages temporarily in a trusted development environment.
SDK logging is disabled and isolated from the root logger by default. Use
`configure_logging()` to select a level and write to stderr or a rotating UTF-8
file. Records reach that destination through a managed queue, so file I/O does
not run in the WebSocket reader or protocol dispatcher. The queue is bounded by
`logging_queue_limit`; `logging_overflow_policy` selects which same-priority
record is discarded, while ERROR and CRITICAL records take priority over lower
levels and cannot be displaced by them. Monitor the process-wide
`dropped_log_records()` counter for overflow. Call
`configure_logging(enabled=False)` to drain the queue and restore the silent
default.

## Property Inspector API

Stream Dock expects a browser-side global function named
`connectElgatoStreamDeckSocket`. The JavaScript resource shipped by this package
defines that compatibility callback and exposes a higher-level singleton as
`window.MiraBoxPropertyInspector`.

| Browser API | Purpose |
|---|---|
| `on(eventName, listener)` / `off(...)` | Subscribe or unsubscribe from connection and protocol events |
| `send(message)` | Send a raw JSON object |
| `sendToPlugin(payload)` | Send the `sendToPlugin` event |
| `setSettings(settings)` | Replace persisted action settings |
| `updateSettings(patch)` | Merge and persist selected setting fields |
| `getSettings()` | Request the latest action settings |
| `action`, `context`, `settings`, `info`, `actionInfo` | Read current registration state |
| `isConnected` | Check whether the WebSocket is open |

Start application sends from `connected`, after registration and the initial
queue flush. Send helpers return `true` when sent immediately and `false` when
accepted into the connecting socket's queue. Calls before the host callback or
while closing/closed throw `Error`; invalid JSON data and immediate send failures
also throw. The queue owns serialized JSON snapshots, so subsequent mutation
cannot change a pending message.

Observe `sendError` for `{ message, error }` when registration or a deferred send
fails. Every unsent queued message receives this event on close, and a failed
send does not suppress later queued attempts. Failed registration closes the
socket and suppresses `connected`; `connected` otherwise fires after the queue
has been attempted if the socket remains open. There are no automatic retries.

`settings` returns a deep JSON snapshot. Settings writes update local optimistic
state only after acceptance; a rejected write leaves it intact. Deferred failures
do not roll back accepted writes. Only `didReceiveSettings` refreshes this state
from the host; sending or connecting does not acknowledge persistence.

Run `mirabox-sdk copy-property-inspector DESTINATION` to copy the version that
matches the installed Python package into a `.sdPlugin` bundle.

## Manifest scope

The SDK consumes action UUIDs and runtime metadata declared by `manifest.json`,
and validates assembled bundles with `mirabox-sdk validate-plugin PATH`.
Use the upstream
[`manifest.json` reference](https://sdk.key123.vip/en/guide/manifest.html) and
the complete local
[`counter_plugin` manifest](../examples/counter_plugin/com.example.counter.sdPlugin/manifest.json)
when creating a plugin bundle.

Validation checks required fields and known optional types, unique action UUIDs,
declared executable/resource paths, and local HTML script/link/image references.
Paths must resolve inside the bundle. Unknown manifest members are accepted;
image dimensions, executable contents, remote resources, dynamic JavaScript
imports and host compatibility still require separate verification. All
bundled `mirabox-sdk.js` copies must match the installed SDK's bytes.
Traversal follows internal directory symlinks and scans each resolved directory
once to avoid cycles. Symlinks escaping the bundle are rejected without
inspecting their targets; unresolvable links and directory read failures are
diagnostics.

Pass `--registry MODULE:OBJECT` to import a populated `ActionRegistry` instance
and compare registered and manifest UUIDs in both directions. The module must
load every action decorator without starting the plugin. Without this option,
registry comparison is explicitly skipped. The public Python function
`validate_plugin(path, action_uuids=registry.action_uuids)` returns a tuple of
field/file diagnostics; an empty tuple means success. The CLI returns `0` on
success and `1` on validation or registry import errors.

The UUID supplied to `ActionRegistry.register()` must exactly match an action
UUID in the manifest. `CodePath` must point to the packaged executable, and
`Software.MinimumVersion` should describe the oldest Stream Dock version the
plugin intends to support. Test that minimum version before publishing when
practical.

## Keeping the map current

When adding or changing protocol behavior:

1. compare the official documentation and templates;
2. update the typed event or command model and the runtime route that consumes
   it;
3. record the Stream Dock version used for runtime verification;
4. add or update a wire-level regression test and an application-level dispatch
   test;
5. update this map, the public exports, and the changelog when the supported API
   changes.

# Stream Dock protocol map

This document maps the public MiraBox Stream Dock plugin protocol to the Python
API implemented by `mirabox-stream-dock-sdk`. It is a compatibility map, not a
replacement for the upstream protocol specification.

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
`3.10.203.0701`. Automated tests simulate the protocol and do not require a
physical Stream Dock device.

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
WebSocket opens, `StreamDockPlugin` sends `RegisterPluginCommand` with the
runtime-provided event name and plugin UUID.

## Events received by a plugin

All known messages are parsed by `parse_stream_dock_event()`. The resulting
model is dispatched to the corresponding `Action` callback where applicable.

| Wire event | Python model | `Action` callback or runtime effect |
|---|---|---|
| `willAppear` | `WillAppearEvent` | Creates the context, then `on_will_appear()` |
| `willDisappear` | `WillDisappearEvent` | Removes the context, then calls `on_will_disappear()` |
| `didReceiveSettings` | `DidReceiveSettingsEvent` | Updates typed settings, then `on_did_receive_settings()` |
| `didReceiveGlobalSettings` | `DidReceiveGlobalSettingsEvent` | Updates runtime state, broadcasts `on_did_receive_global_settings()`, and replays the latest event to actions created later |
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

An unknown event is preserved as `UnknownStreamDockEvent` by default. Pass
`allow_unknown=False` to `parse_stream_dock_event()` when strict rejection with
`UnsupportedEventError` is preferable.

## Events sent by a plugin

Commands can be constructed directly and passed to `StreamDockSender.send()`.
The `Action` and `StreamDockPlugin` helpers cover the common cases.

| Wire event | Command model | Convenience API |
|---|---|---|
| Runtime registration event | `RegisterPluginCommand` | Sent by `StreamDockPlugin` on connection |
| `setSettings` | `SetSettingsCommand` | `Action.set_settings()` |
| `getSettings` | `GetSettingsCommand` | `Action.get_settings()` |
| `setGlobalSettings` | `SetGlobalSettingsCommand` | `StreamDockPlugin.set_global_settings()` / `set_typed_global_settings()` |
| `getGlobalSettings` | `GetGlobalSettingsCommand` | `StreamDockPlugin.get_global_settings()` |
| `setTitle` | `SetTitleCommand` | `Action.set_title()` |
| `setImage` | `SetImageCommand` | `Action.set_image()` |
| `setState` | `SetStateCommand` | `Action.set_state()` |
| `showOk` | `ShowOkCommand` | `Action.show_ok()` |
| `showAlert` | `ShowAlertCommand` | `Action.show_alert()` |
| `openUrl` | `OpenUrlCommand` | `Action.open_url()` |
| `logMessage` | `LogMessageCommand` | `Action.log_message()` |
| `sendToPropertyInspector` | `SendToPropertyInspectorCommand` | `Action.send_to_property_inspector()` / `send_typed_to_property_inspector()` |

All command models expose `to_wire()` for the exact JSON object sent through
the WebSocket. The transport validates that this object contains only JSON
values and rejects non-finite numbers such as `NaN` before sending.

Per-message protocol logs are emitted only at DEBUG and contain routing metadata
such as the event and context. Message payloads are redacted by default because
settings and Property Inspector messages may contain secrets under plugin-defined
field names. Pass `include_payload=True` to `configure_logging()` to include
complete messages temporarily in a trusted development environment.
SDK logging is disabled and isolated from the root logger by default. Use
`configure_logging()` to select a level and write to stderr or a rotating UTF-8
file, then call it with `enabled=False` to restore the silent default.

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

Run `mirabox-sdk copy-property-inspector DESTINATION` to copy the version that
matches the installed Python package into a `.sdPlugin` bundle.

## Manifest scope

The SDK consumes action UUIDs and runtime metadata declared by `manifest.json`,
but it does not currently generate or validate a manifest. Use the upstream
[`manifest.json` reference](https://sdk.key123.vip/en/guide/manifest.html) and
the complete local
[`counter_plugin` manifest](../examples/counter_plugin/com.example.counter.sdPlugin/manifest.json)
when creating a plugin bundle.

The UUID supplied to `ActionRegistry.register()` must exactly match an action
UUID in the manifest. `CodePath` must point to the packaged executable, and
`Software.MinimumVersion` should describe the oldest Stream Dock version the
plugin intends to support. Test that minimum version before publishing when
practical.

## Keeping the map current

When adding or changing protocol behavior:

1. compare the official documentation and templates;
2. record the Stream Dock version used for runtime verification;
3. add or update a wire-level regression test;
4. update this map, the public exports, and the changelog when the supported API
   changes.

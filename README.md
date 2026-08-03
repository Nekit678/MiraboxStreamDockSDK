<div align="center">
  <p><strong>English</strong> · <a href="https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/README.ru.md">Русский</a></p>
  <img src="https://raw.githubusercontent.com/Nekit678/MiraboxStreamDockSDK/main/docs/assets/logo.svg" width="104" height="104" alt="MiraBox Stream Dock SDK logo">
  <h1>MiraBox Stream Dock SDK</h1>
  <p><strong>A typed Python SDK for building MiraBox Stream Dock plugins</strong></p>
  <p>
    <a href="https://pypi.org/project/mirabox-stream-dock-sdk/"><img src="https://img.shields.io/pypi/v/mirabox-stream-dock-sdk?style=flat-square&amp;logo=pypi&amp;logoColor=white" alt="PyPI version"></a>
    <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.11+"></a>
    <img src="https://img.shields.io/badge/Stream%20Dock-2.10%2B-087DEA?style=flat-square" alt="MiraBox Stream Dock 2.10+">
    <a href="https://github.com/Nekit678/MiraboxStreamDockSDK/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Nekit678/MiraboxStreamDockSDK/ci.yml?branch=main&amp;style=flat-square&amp;label=CI&amp;logo=githubactions&amp;logoColor=white" alt="CI status"></a>
    <a href="https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/mirabox-stream-dock-sdk?style=flat-square" alt="MIT license"></a>
  </p>
  <p>
    Build reusable actions for keys, touch panels, and dials<br>
    without hand-writing the Stream Dock WebSocket protocol.
  </p>
  <p>
    <a href="#quick-start">Quick start</a> ·
    <a href="https://pypi.org/project/mirabox-stream-dock-sdk/">PyPI</a> ·
    <a href="#counter-example-plugin">Example plugin</a> ·
    <a href="#protocol-basis">Protocol</a> ·
    <a href="#api-overview">API</a> ·
    <a href="#development">Development</a>
  </p>
</div>

---

## About

`mirabox-stream-dock-sdk` provides the protocol, runtime, and browser-side tools
needed to build Python plugins for MiraBox Stream Dock. It validates launch
arguments and incoming messages, creates one typed action instance per visible
control, dispatches lifecycle events, and serializes commands back to the
Stream Dock application.

The SDK was originally developed as part of a Stream Dock plugin. It was later
extracted into a standalone project so the protocol and runtime could be reused
across plugins, tested independently, and evolved as a public package. The SDK
will continue to be improved as it is used in real plugins and more Stream Dock
behavior is verified.

> [!IMPORTANT]
> The project is currently in the `0.x` series. It is ready for experimentation
> and real plugin development, but public APIs may evolve between minor releases
> before `1.0`.

> [!NOTE]
> This is an unofficial community project and is not affiliated with or endorsed
> by MiraBox, HotSpot, or Elgato. The callback name
> `connectElgatoStreamDeckSocket` is retained because Stream Dock uses it for
> Property Inspector compatibility.

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Property Inspector client](#property-inspector-client)
- [Counter example plugin](#counter-example-plugin)
- [Protocol basis](#protocol-basis)
- [API overview](#api-overview)
- [Errors and unknown events](#errors-and-unknown-events)
- [Inbound event queue](#inbound-event-queue)
- [Outbound command bus](#outbound-command-bus)
- [Concurrency contract](#concurrency-contract)
- [Logging](#logging)
- [Project structure](#project-structure)
- [Development](#development)
- [Releasing](#releasing)

## Features

| | Feature | What it provides |
|:--:|---|---|
| 🧩 | Typed protocol | Dataclass models for registration, commands, and key, touch, dial, device, application, and settings events. |
| 🧭 | Precise validation | Malformed payloads report the event name and exact JSON field path that failed validation. |
| 🎛️ | Action runtime | One action instance per Stream Dock context, declarative UUID registration, and automatic lifecycle dispatch. |
| 🔌 | WebSocket transport | Registration, message parsing, command serialization, logging, and graceful shutdown. |
| 🗃️ | Typed settings | Pluggable codecs for action settings, global settings, and Property Inspector messages. |
| 🖥️ | Property Inspector | A versioned, dependency-free JavaScript client with connection state, events, settings helpers, and queued startup messages. |
| 🧰 | Plugin services | Start and stop plugin-owned background services in a predictable order. |
| 📦 | Distribution tooling | A CLI resource copier, PyInstaller example, package verification, CI, and Trusted Publishing workflow. |
| 🛡️ | Forward compatibility | Unknown but valid events can be preserved as `UnknownStreamDockEvent` instead of breaking the plugin. |

## How it works

```mermaid
flowchart LR
    App["MiraBox Stream Dock<br>Windows"] <-->|"WebSocket · JSON"| Boundary["Typed Stream Dock boundary"]
    Boundary --> Runtime["StreamDockRuntime<br>keyed-serial dispatcher"]
    Registry["ActionRegistry<br>UUID → Action class"] --> Runtime
    Runtime --> Actions["Action instances<br>one per context"]
    PI["Property Inspector<br>HTML / JavaScript"] <-->|"settings and messages"| App
    Client["MiraBoxPropertyInspector<br>browser client"] --> PI
```

Stream Dock starts the packaged plugin executable with the WebSocket port,
plugin UUID, registration event, and application metadata. `run_plugin_cli()`
parses those arguments, while `create_stream_dock_application()` composes the
typed boundary and runtime that register the plugin and route incoming events.

## Requirements

- Python `3.11+`;
- MiraBox Stream Dock `2.10.179.426` or newer (declared minimum);
- `websocket-client>=1.8,<2` (installed automatically);
- Windows to run Stream Dock and package a standalone plugin with PyInstaller.

The SDK's Stream Dock integration has been manually verified with Stream Dock
`3.10.203.0701`.

The SDK itself and its test suite can be developed on Windows, Linux, or WSL.
The final `.exe` must be built on Windows because PyInstaller is not a
cross-compiler.

## Installation

Install the released package from PyPI:

```bash
python -m pip install mirabox-stream-dock-sdk
```

To work on the SDK from source:

```bash
git clone https://github.com/Nekit678/MiraboxStreamDockSDK.git
cd MiraboxStreamDockSDK
python -m venv .venv
```

<details>
<summary><strong>Windows PowerShell</strong></summary>

```powershell
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

</details>

<details>
<summary><strong>Linux / WSL</strong></summary>

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

</details>

## Quick start

Define dependencies shared by your action instances, register each action UUID,
and return a configured `StreamDockApplication` from the application factory:

```python
from __future__ import annotations

from dataclasses import dataclass

from mirabox_sdk import (
    Action,
    ActionRegistry,
    JsonObject,
    KeyDownEvent,
    PluginLaunchArguments,
    StreamDockApplication,
    StreamDockSender,
    WillAppearEvent,
    create_stream_dock_application,
    run_plugin_cli,
)

ACTION_UUID = "com.example.counter.increment"


@dataclass(frozen=True, slots=True)
class Dependencies:
    stream_dock: StreamDockSender


registry: ActionRegistry[Dependencies] = ActionRegistry()


@registry.register(ACTION_UUID)
class CounterAction(Action[JsonObject, Dependencies]):
    def _render(self) -> None:
        count = self.settings.get("count", 0)
        self.set_title(str(count if type(count) is int else 0))

    def on_will_appear(self, _event: WillAppearEvent) -> None:
        self._render()

    def on_key_down(self, _event: KeyDownEvent) -> None:
        count = self.settings.get("count", 0)
        self.set_settings({"count": (count if type(count) is int else 0) + 1})
        self._render()


def build_application(arguments: PluginLaunchArguments) -> StreamDockApplication:
    return create_stream_dock_application(
        arguments,
        action_factory=registry,
        action_dependencies_factory=Dependencies,
    )


if __name__ == "__main__":
    raise SystemExit(run_plugin_cli(build_application))
```

The exact same action UUID must appear in the plugin's `manifest.json`. Stream
Dock creates and removes action contexts through `willAppear` and
`willDisappear`; the runtime manages the corresponding Python instances.

### Action callbacks

Override only the callbacks an action needs:

| Input or lifecycle | `Action` callback |
|---|---|
| Action becomes visible or disappears | `on_will_appear`, `on_will_disappear` |
| Key press or release | `on_key_down`, `on_key_up` |
| Touch panel tap | `on_touch_tap` |
| Dial press, release, or rotation | `on_dial_down`, `on_dial_up`, `on_dial_rotate` |
| Settings or title parameters change | `on_did_receive_settings`, `on_title_parameters_did_change` |
| Property Inspector opens, closes, or sends data | `on_property_inspector_did_appear`, `on_property_inspector_did_disappear`, `on_send_to_plugin` |
| Device, application, and wake-up notifications | `on_device_did_connect`, `on_device_did_disconnect`, `on_application_did_launch`, `on_application_did_terminate`, `on_system_did_wake_up` |

Action helper methods cover the common outbound commands: `set_title()`,
`set_image()`, `set_state()`, `set_settings()`, `get_settings()`, `show_ok()`,
`show_alert()`, `open_url()`, `log_message()`, and
`send_to_property_inspector()`. Display-only updates also have non-blocking
`set_title_async()`, `set_image_async()`, and `set_state_async()` variants.

### Typed settings

Actions use JSON objects by default. To work with an application-specific type,
provide a `JsonCodec` on the action class:

```python
from dataclasses import dataclass

from mirabox_sdk import Action, FunctionalJsonCodec, JsonObject


@dataclass(frozen=True, slots=True)
class CounterSettings:
    count: int


def decode_settings(value: JsonObject) -> CounterSettings:
    count = value.get("count", 0)
    if type(count) is not int:
        raise ValueError("count must be an integer")
    return CounterSettings(count)


COUNTER_SETTINGS_CODEC = FunctionalJsonCodec(
    decoder=decode_settings,
    encoder=lambda value: {"count": value.count},
)


class CounterAction(Action[CounterSettings, Dependencies]):
    settings_codec = COUNTER_SETTINGS_CODEC
```

The codec boundary verifies that encoded values are valid JSON. Decode errors
are wrapped with the relevant event name and settings path.

### Global settings

Use `update_global_settings()` when several in-memory changes belong to one
logical operation. The callback works on an isolated draft; an exception or
invalid JSON result rolls back the complete update:

```python
def append_items(settings: JsonObject) -> None:
    items = settings.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    items.extend(values)


runtime.update_global_settings(append_items)
```

After the callback succeeds, the transaction validates the complete draft and
persists it with one `setGlobalSettings` command. Callback, validation, and send
failures leave the previous local state unchanged. Direct mutations of
`runtime.global_settings` remain supported for local replay state, but the
transactional method is preferred for a batch of related persisted changes.

## Property Inspector client

Copy the JavaScript client shipped with the installed SDK into the plugin
bundle:

```bash
mirabox-sdk copy-property-inspector \
  com.example.counter.sdPlugin/property-inspector
```

The command refuses to overwrite a different copy by default. Pass `--force`
when intentionally updating the bundled client.

Load it before the action-specific script:

```html
<script src="mirabox-sdk.js"></script>
<script src="counter.js"></script>
```

Stream Dock invokes the compatibility callback automatically. The action script
uses the shared client through `window.MiraBoxPropertyInspector`:

```javascript
const client = window.MiraBoxPropertyInspector;

client.on("connected", ({ settings }) => {
  console.log("Current settings", settings);
});

client.on("didReceiveSettings", ({ payload }) => {
  console.log("Updated settings", payload.settings);
});

client.sendToPlugin({ event: "refresh" });
client.updateSettings({ mode: "toggle" });
```

The client exposes `on()`, `off()`, `send()`, `sendToPlugin()`, `setSettings()`,
`updateSettings()`, and `getSettings()`, plus connection and registration state.
Messages sent while the WebSocket is connecting are queued until it opens.

## Counter example plugin

[`examples/counter_plugin`](https://github.com/Nekit678/MiraboxStreamDockSDK/tree/main/examples/counter_plugin)
is a complete plugin rather
than an isolated code fragment. It includes:

- a package with a registered counter action;
- a Property Inspector that can reset the counter;
- a valid `.sdPlugin` bundle and manifest;
- SVG assets and a PyInstaller specification;
- tests for the plugin behavior.

Build its executable on Windows:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --clean --noconfirm examples/counter_plugin/build.spec
Copy-Item dist\CounterPlugin.exe `
  examples\counter_plugin\com.example.counter.sdPlugin\
```

Copy the resulting `com.example.counter.sdPlugin` directory to
`%APPDATA%\HotSpot\StreamDock\plugins\` and restart Stream Dock. See the
[example guide](https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/examples/counter_plugin/README.md)
for a source-run command and
the complete packaging flow.

## Protocol basis

This package is an independent, typed Python implementation of the WebSocket /
JSON plugin API published by MiraBox. The primary upstream sources are:

- the [official StreamDock Plugin SDK repository](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK),
  including its [Python template](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK/tree/main/SDPythonSDK);
- the official [registration procedure](https://sdk.key123.vip/en/guide/registration.html),
  [received events](https://sdk.key123.vip/en/guide/events-received.html), and
  [events sent](https://sdk.key123.vip/en/guide/events-sent.html) reference;
- the official [`manifest.json` reference](https://sdk.key123.vip/en/guide/manifest.html)
  and [Property Inspector guide](https://sdk.key123.vip/en/guide/property-inspector.html);
- the [upstream template overview on DeepWiki](https://deepwiki.com/MiraboxSpace/StreamDock-Plugin-SDK)
  for secondary, generated explanations of the repository;
- the [Space Platform](https://space.key123.vip/) for publishing completed
  Stream Dock plugins.

The local [protocol map](https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/docs/PROTOCOL.md)
connects each supported wire event
and command to its Python model or helper and calls out behavior verified in
Stream Dock but not currently listed in the upstream event reference. When the
published documentation and observed runtime behavior differ, tests record the
behavior implemented by this SDK.

## API overview

| Area | Public API |
|---|---|
| Runtime | `StreamDockApplication`, `StreamDockRuntime`, `create_stream_dock_application`, `RuntimeDispatcherConfig`, runtime metrics and ports |
| Actions | `Action`, `ActionRegistry`, `StreamDockSender` |
| Launch and registration | `PluginLaunchArguments`, registration dataclasses, `parse_plugin_cli_arguments`, `run_plugin_cli` |
| Input events | Typed immutable event models and `InboundOverflowPolicy` |
| Output commands | Registration, settings, title, image, state, feedback, URL, log, and Property Inspector command models; `ValidatedWireMessage` |
| Application data | `JsonCodec`, `FunctionalJsonCodec`, `JsonObjectCodec`, `ValidatedJsonObject`, `OwnedJsonPayload`, typed encode/decode helpers |
| Resources | `copy_property_inspector_client`, `property_inspector_client_bytes`, `mirabox-sdk` CLI |
| Parsing | `parse_stream_dock_event`, `parse_registration_info`, typed protocol errors |
| Logging | `configure_logging` with isolated console, file, and disable controls |

The supported public surface is exported from `mirabox_sdk`. Objects from
individual modules should be treated as implementation details unless they are
also exported there.

## Errors and unknown events

| Exception | Meaning |
|---|---|
| `InvalidPluginLaunchArgumentsError` | Stream Dock did not provide valid executable arguments. |
| `InvalidRegistrationInfoError` | The registration metadata JSON has an invalid field. |
| `MalformedEventError` / `InvalidFieldError` | A known event is malformed; the error includes its JSON path. |
| `UnsupportedEventError` | An unknown event was parsed with `allow_unknown=False`. |
| `JsonCodecDecodeError` | Plugin-owned settings or messages could not be decoded. |
| `JsonCodecEncodeError` | A codec produced a value that cannot be sent as JSON. |
| `OutboundQueueFullError` | The bounded outbound command queue is full. |
| `OutboundCommandBusClosedError` | A command was submitted after outbound shutdown began. |

By default, `parse_stream_dock_event()` preserves an unknown but structurally
valid envelope as `UnknownStreamDockEvent`. This lets the SDK tolerate protocol
extensions while known events remain strictly validated. The runtime delivers
each preserved event once to the `PluginHooks` object passed to
`create_stream_dock_application()`. Unknown envelopes are not broadcast to
actions because their routing semantics are not known yet.

Protocol parsing metadata and runtime routing metadata are maintained in
separate validated internal registries. The public API exposes typed event
models rather than registry implementation details.

## Inbound event queue

The typed boundary parses frames outside application callbacks and puts valid
events into a bounded queue. A keyed-serial worker pool invokes action
callbacks: one action context remains strictly ordered, while different
contexts can make progress concurrently. `willAppear`, `willDisappear`,
broadcast, and unknown events are exclusive ordering barriers; each waits for
earlier context callbacks and completes before later callbacks start. On normal
shutdown, the queue drains before `StreamDockApplication.run()` returns and
before the runtime releases actions.

The pool defaults to four workers and the queue defaults to 1,024 events.
Lifecycle, settings, input, broadcast, unknown, and every other event except
`dialRotate` are lossless by default. `dialRotate` is explicitly coalescable
and may be discarded on overflow. Configure worker concurrency, the limit, and
overflow behavior for discardable events when constructing the application:

```python
from mirabox_sdk import (
    InboundOverflowPolicy,
    RuntimeDispatcherConfig,
    StreamDockQueueConfig,
    create_stream_dock_application,
)

application = create_stream_dock_application(
    arguments,
    action_factory=registry,
    action_dependencies_factory=Dependencies,
    queue_config=StreamDockQueueConfig(
        raw_inbound_limit=512,
        inbound_event_limit=512,
        outbound_command_limit=512,
        raw_outbound_limit=512,
        session_event_limit=16,
    ),
    runtime_config=RuntimeDispatcherConfig(worker_count=4),
    inbound_overflow_policy=InboundOverflowPolicy.DROP_OLDEST,
    coalesce_dial_rotations=True,
)
```

`DROP_NEWEST` (the default) discards the newest eligible `dialRotate`;
`DROP_OLDEST` discards the oldest eligible rotation. Neither policy may evict a
lossless event. If the queue contains only lossless events, another lossless
event applies backpressure to the WebSocket reader until the dispatcher frees
space; an incoming rotation is discarded instead. This keeps memory bounded
without allowing overflow to corrupt runtime state.

Rotation coalescing is opt-in: compatible pending `dialRotate` events for the
same context and pressed state are combined by summing `ticks`; an intervening
event for that context, or any broadcast/unknown event, prevents coalescing.

Read `application.metrics()` for immutable queue, event-pump, scheduler, route,
action, session, and transport snapshots. Runtime and boundary shutdown stages
are bounded to five seconds by default; pass `None` explicitly only when an
unbounded wait is required. At a timeout, pending and active work remains
observable in metrics and metadata-only diagnostics.

Python cannot safely stop a running thread. A callback that exceeds the timeout
continues on its daemon worker until the callback itself returns, even though
`close()` proceeds. Callback code should therefore use its own bounded I/O and
cooperative cancellation where appropriate.

## Outbound command bus

Every `StreamDockApplication` owns one dedicated outbound writer.
Calling `send()` puts the typed command into a bounded FIFO queue; only that
writer validates and serializes the command, emits its protocol log, and calls
the WebSocket transport. Concurrent plugin threads therefore cannot interleave
frames. `send()` waits for its command's result, so serialization and transport
errors still reach the caller and state-update helpers retain their rollback
behavior.

`send_async()` performs the same queue acceptance but returns a
`CommandFuture` before serialization or WebSocket I/O. Queue-full and
shutdown rejections are raised immediately; call `future.result()` only when
the eventual writer-side error or completion matters. For high-frequency
display rendering, `Action.set_image_async()`, `set_title_async()`, and
`set_state_async()` avoid holding an inbound callback while the writer is slow.
Rollback-sensitive settings helpers remain synchronous.

The outbound queue holds 1,024 waiting commands by default. It never silently
drops a command when full: `send()` and `send_async()` raise
`OutboundQueueFullError`. Configure its capacity together with the other
boundary limits:

```python
from mirabox_sdk import StreamDockQueueConfig, create_stream_dock_application

application = create_stream_dock_application(
    arguments,
    action_factory=registry,
    action_dependencies_factory=Dependencies,
    queue_config=StreamDockQueueConfig(
        raw_inbound_limit=512,
        inbound_event_limit=512,
        outbound_command_limit=512,
        raw_outbound_limit=512,
        session_event_limit=16,
    ),
    coalesce_commands=True,
)
```

Coalescing is opt-in. Compatible adjacent pending `setState`, `setTitle`,
`setImage`, `setSettings`, or `setGlobalSettings` commands for the same
semantic target are replaced by their newest value. Commands of another type or
target are ordering barriers. All callers whose commands were combined receive
distinct `CommandFuture` handles backed by the queued command's single
completion state, and therefore observe the same final write result. The queue
retains at most one completion state per physical entry.

Read `application.metrics().boundary` for atomic outbound queue, writer, raw
transport, and connector snapshots. Once shutdown starts, new submissions raise
`OutboundCommandBusClosedError`; accepted commands receive exactly one terminal
result through the canonical `CommandFuture`.

## Concurrency contract

The runtime uses explicit thread ownership:

| Surface | Supported caller or owner |
|---|---|
| `configure_logging()` and `StreamDockApplication.run()` / `stop()` | Application lifecycle thread; configure logging before `run()`; `stop()` is idempotent and may also be called concurrently |
| WebSocket frame I/O and typed protocol parsing | Boundary-owned transport/codec workers |
| Every `Action` callback and `PluginHooks` callback | Runtime-owned keyed workers; callbacks are serial per context and may overlap across contexts, while lifecycle, broadcast, and unknown barriers run exclusively |
| `StreamDockSender.send()` / `send_async()` and action command helpers | Any application, service, or action-callback thread; overlapping calls are supported |
| `StreamDockApplication.stop()` | Any application or action-callback thread; calls are idempotent and may overlap |

The outbound queue establishes FIFO order when it accepts commands. Calls that
do not overlap retain caller order; the relative order of simultaneous calls
is intentionally unspecified. Each `send()` waits only for its own accepted
submission (or the final coalesced write) and receives its serialization,
transport, overflow, or shutdown result. `send_async()` returns after
acceptance; the returned `CommandFuture` exposes the later result.

Scalar-only frozen command objects may be shared between threads.
Payload-bearing commands own mutable `OwnedJsonPayload` data: do not mutate a
command or its payload once any thread begins `send()` or `send_async()`.
`ValidatedJsonObject` backing snapshots are safe to hand between threads after
construction, but every mutable COW view—event settings, `Action.settings`,
`runtime.global_settings`, and `OwnedJsonPayload`—allows only one accessing or
mutating thread at a time. Use `update_global_settings()` for serialized,
rollback-safe updates from background services; do not share a live mutable
view between threads.

Shutdown closes the typed boundary, drains owned inbound work while callback
commands can still finish, stops the scheduler and pumps, and finally releases
all action contexts. A background thread that needs to interrupt `run()` calls
`application.stop()`.

## Logging

SDK logging is disabled by default: it does not propagate to the application's
root logger and does not create a log file. Enable diagnostics explicitly before
calling `run_plugin_cli()`:

```python
from mirabox_sdk import configure_logging

configure_logging(level="INFO")
```

When enabled without a file, the destination is stderr. To write UTF-8 logs to
a file, pass a path; missing parent directories are created automatically. File
logging rotates at 5 MiB with three backups by default:

```python
from pathlib import Path

from mirabox_sdk import LoggingOverflowPolicy, configure_logging

configure_logging(
    level="DEBUG",
    log_file=Path.home() / ".mirabox-counter" / "plugin.log",
    max_bytes=5 * 1024 * 1024,
    backup_count=3,
    logging_queue_limit=1024,
    logging_overflow_policy=LoggingOverflowPolicy.DROP_NEWEST,
)
```

Adjust `max_bytes` and `backup_count` for the plugin's needs. Set
`max_bytes=0` only when intentionally requesting an unbounded file.
`logging_queue_limit` bounds the number of records waiting for the managed
listener; it must be positive. `DROP_NEWEST` preserves already queued records,
while `DROP_OLDEST` keeps the most recent records of the same priority.

`include_payload=True` adds the complete inbound and outbound protocol message
to `DEBUG` records. Payloads may contain tokens, settings, and other secrets, so
enable this option only temporarily in a trusted development environment. Omit
the option (its default is `False`) to return to redacted payloads while keeping
other diagnostics enabled.

```python
configure_logging(
    level="DEBUG",
    log_file=Path.home() / ".mirabox-counter" / "plugin.log",
    include_payload=True,
)
```

Repeated calls replace the handler previously installed by
`configure_logging()`, draining its queue first, so the level or destination can
be changed without duplicating messages. Return the SDK to its default silent
state and flush pending records with:

```python
configure_logging(enabled=False)
```

`INFO` records cover connection lifecycle and operational status. Per-message
protocol direction, event, and context are emitted only at `DEBUG`. Message
payloads remain redacted unless `include_payload=True` is explicitly configured.
SDK records are handed to one managed logging thread, so stream and rotating
file I/O never runs in the WebSocket reader, inbound dispatcher, outbound
writer, or calling service thread. Handlers installed manually by the
application remain its responsibility and are outside that guarantee. The
managed queue is bounded and producers never wait for destination I/O. On
overflow, an `ERROR` or `CRITICAL` record displaces a lower-level record when
possible and is processed ahead of queued `DEBUG` through `WARNING` records.
Lower-level records never displace queued errors; within the same priority
class, the configured overflow policy is applied. `dropped_log_records()`
returns the process-wide, thread-safe count of records discarded by all managed
logging queues:

```python
from mirabox_sdk import dropped_log_records

if dropped_log_records():
    # The destination has not kept up with the configured log volume.
    ...
```

## Project structure

```text
MiraboxStreamDockSDK/
├── pyproject.toml                     # Package metadata and tool configuration
├── src/mirabox_sdk/
│   ├── action.py                      # Reusable action base class
│   ├── action_registry.py             # Action UUID registry
│   ├── completion.py                  # Canonical command completion contract
│   ├── commands.py                    # Typed outbound commands
│   ├── events.py                      # Typed inbound event models
│   ├── parser.py                      # Strict wire-message parser
│   ├── runtime/                       # Stable application/runtime API
│   ├── _next/                         # Private boundary/dispatcher implementation
│   ├── logging_config.py              # Isolated SDK logging configuration
│   └── property_inspector/            # Browser-side SDK resource
├── examples/counter_plugin/           # Complete buildable plugin
├── tests/                             # SDK and release-tool tests
├── scripts/                           # Version and distribution verification
└── .github/workflows/                 # CI and Trusted Publishing release jobs
```

## Development

Install the development dependencies, then run the same checks as CI:

```bash
python -m unittest discover -s tests -v
PYTHONPATH=examples/counter_plugin/src \
  python -m unittest discover -s examples/counter_plugin/tests -v
python -m compileall -q src tests scripts examples
ruff check src tests scripts examples
ruff format --check src tests scripts examples
python -m build
python scripts/verify_distribution.py dist
python -m twine check dist/*
```

The test suite uses fake connections and protocol messages; it does not require
a running Stream Dock instance. CI runs the SDK on Linux and Windows across all
supported Python versions.

Contributions are welcome. Please read
[CONTRIBUTING.md](https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/CONTRIBUTING.md)
before
submitting a change, and include the Stream Dock version and a regression test
when changing observed protocol behavior.

## Releasing

Releases are built from version tags, published to PyPI through Trusted
Publishing, and attached to a generated GitHub Release. The required one-time
configuration and release checklist are documented in
[RELEASING.md](https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/RELEASING.md).

## License

Distributed under the
[MIT License](https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/LICENSE).

---

<div align="center">
  Built for reusable Python plugins on MiraBox Stream Dock.
</div>

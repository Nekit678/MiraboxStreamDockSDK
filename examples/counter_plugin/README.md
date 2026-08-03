# Counter example plugin

This directory contains a complete binary Stream Dock plugin built with
`mirabox-stream-dock-sdk`. Each key press increments a persisted counter, and
its Property Inspector can reset the value.

The example demonstrates:

- an `ActionRegistry` and one action instance per Stream Dock context;
- persistent action settings;
- commands that update a key title;
- Property Inspector messages in both directions;
- a `.sdPlugin` manifest and assets;
- PyInstaller packaging and isolated behavior tests.

## Layout

```text
counter_plugin/
├── build.spec                         # PyInstaller configuration
├── src/counter_plugin/                # Python plugin package
├── tests/test_counter_plugin.py       # Behavior tests with a fake connection
└── com.example.counter.sdPlugin/
    ├── manifest.json                  # Plugin and action metadata
    ├── assets/icon.svg
    └── property-inspector/            # Browser configuration UI
```

## Run from source

From the SDK repository root, install the development environment and refresh
the browser client included in the example:

```bash
python -m pip install -e ".[dev]"
mirabox-sdk copy-property-inspector \
  examples/counter_plugin/com.example.counter.sdPlugin/property-inspector
```

Stream Dock normally supplies the launch arguments. For a protocol-level source
run against a WebSocket server on port `12345`, use:

```bash
PYTHONPATH=examples/counter_plugin/src python -m counter_plugin \
  -port 12345 \
  -pluginUUID com.example.counter \
  -registerEvent registerPlugin \
  -info '{"application":{"language":"en","platform":"windows","platformVersion":"11","version":"2.10"},"colors":{},"devicePixelRatio":1,"devices":[],"plugin":{"uuid":"com.example.counter","version":"0.1.0"}}'
```

The command waits for Stream Dock protocol messages; it is not a standalone UI.

## Experimental runtime opt-in

The example continues to use the legacy `WebSocketStreamDockConnection` and
`StreamDockPlugin` by default. To run this one plugin directly through the
experimental typed boundary and the new runtime dispatcher, set
`MIRABOX_SDK_EXPERIMENTAL_RUNTIME` to the exact value `1` before starting it:

```powershell
$env:MIRABOX_SDK_EXPERIMENTAL_RUNTIME = "1"
python -m counter_plugin `
  -port 12345 `
  -pluginUUID com.example.counter `
  -registerEvent registerPlugin `
  -info '{"application":{"language":"en","platform":"windows","platformVersion":"11","version":"2.10.179.426"},"colors":{},"devicePixelRatio":1,"devices":[],"plugin":{"uuid":"com.example.counter","version":"0.1.0"}}'
```

For the packaged executable, set the variable before launching Stream Dock so
the plugin process inherits it. Unset the variable and restart Stream Dock to
return to the legacy runtime. The opt-in path uses
`create_experimental_stream_dock_application()` and does not place
`BoundaryStreamDockConnection` between the dispatcher and boundary.

The older `MIRABOX_SDK_EXPERIMENTAL_BOUNDARY=1` switch remains available during
the migration window. It selects the typed boundary with the legacy runtime
through `BoundaryStreamDockConnection`. If both variables are `1`, the new
runtime opt-in takes precedence. SDK diagnostics identify which experimental
path was selected without logging event payloads.

The transitional boundary adapter was manually verified on 2026-07-28 with
installed Stream Dock `3.10.203.0701`. The host's
`-info.application.version` launch metadata reported the compatibility value
`2.10.179.426`; the installed application version and the launch metadata are
therefore recorded separately.
The acceptance run confirmed all of the following:

1. the Counter plugin registers and remains connected;
2. adding the Counter action delivers `willAppear` and renders its title;
3. pressing the key delivers `keyDown`, persists the count, and updates the
   title;
4. resetting in the Property Inspector delivers `sendToPlugin` and sends the
   expected settings and title commands.

The observed title sequence was `0 → 1 → 0`, and the Stream Dock log recorded
`com.example.counter.sdPlugin` as connected.

The new runtime-dispatcher path was manually verified on 2026-08-03 with the
same installed Stream Dock `3.10.203.0701`. A Windows bundle built from the
current tree was launched with a process-local runtime opt-in. The host log
confirmed registration, the device and Property Inspector produced the same
`0 → 1 → 0` sequence, and the profile persisted the final `count: 0`. When the
host process ended, both plugin processes and their WebSocket connection exited
without hanging. The original installed bundle and legacy-default Stream Dock
launch were restored after the acceptance run.

The fake-connector integration test additionally covers registration,
global-settings, action, outbound-command, acknowledgement, and shutdown flows
deterministically.

## Test

The example tests use a fake connection and do not require Stream Dock:

```bash
PYTHONPATH=examples/counter_plugin/src \
  python -m unittest discover -s examples/counter_plugin/tests -v
```

## Build on Windows

PyInstaller must run on Windows to produce the `.exe` expected by the manifest:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --clean --noconfirm examples/counter_plugin/build.spec
Copy-Item dist\CounterPlugin.exe `
  examples\counter_plugin\com.example.counter.sdPlugin\
```

The final bundle must contain `CounterPlugin.exe` at its root because
`manifest.json` declares `"CodePath": "CounterPlugin.exe"`.

## Install locally

Copy the complete `com.example.counter.sdPlugin` directory to:

```text
%APPDATA%\HotSpot\StreamDock\plugins\
```

Restart Stream Dock, then add **Examples → Counter** to a compatible key. Open
the Property Inspector to reset the persisted count.

## Use as a starting point

Before turning the example into a new plugin:

1. replace `com.example.counter` and the action UUID everywhere in the Python
   package, manifest, and Property Inspector;
2. update the manifest name, author, description, URL, versions, icons, and
   supported controllers;
3. rename the executable and keep `CodePath` consistent with the PyInstaller
   output;
4. add tests for each new action and any observed protocol behavior.

See the [official manifest reference](https://sdk.key123.vip/en/guide/manifest.html)
and this repository's [protocol map](../../docs/PROTOCOL.md) for the relevant
wire contract.

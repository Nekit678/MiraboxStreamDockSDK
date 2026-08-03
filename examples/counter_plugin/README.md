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

## Production runtime

The example uses `create_stream_dock_application()` directly. The typed
boundary, runtime dispatcher, keyed-serial scheduler, and bounded queues are the
default and require no environment switch. The former experimental runtime and
boundary variables are no longer read.

The runtime-dispatcher path was manually verified on 2026-08-03 with installed
Stream Dock `3.10.203.0701`. Registration, `willAppear`, physical `keyDown`,
`sendToPlugin`, settings/title commands, the `0 → 2 → 0` Counter sequence, and
bounded process/WebSocket shutdown were confirmed.

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

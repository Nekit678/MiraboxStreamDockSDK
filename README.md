# MiraBox Stream Dock SDK for Python

Typed, unofficial Python SDK for building plugins for MiraBox Stream Dock. It
implements the WebSocket protocol, registration, typed event parsing, outbound
commands, per-plugin action registration, action lifecycle, plugin services,
and a shared Property Inspector client.

This project is not affiliated with or endorsed by MiraBox, HotSpot, or Elgato.
The `connectElgatoStreamDeckSocket` callback name is kept because Stream Dock
uses it for Property Inspector compatibility.

## Requirements

- Python 3.11 or newer;
- MiraBox Stream Dock 2.10.179.426 or newer;
- Windows for packaging a Python plugin with PyInstaller.

## Installation

```bash
python -m pip install mirabox-stream-dock-sdk
```

For SDK development:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

On Windows, use `.venv\Scripts\python.exe` instead.

## Minimal plugin

```python
from __future__ import annotations

from dataclasses import dataclass

from mirabox_sdk import (
    Action,
    ActionRegistry,
    JsonObject,
    KeyDownEvent,
    PluginLaunchArguments,
    StreamDockPlugin,
    StreamDockSender,
    WebSocketStreamDockConnection,
    WillAppearEvent,
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


def build_application(arguments: PluginLaunchArguments) -> StreamDockPlugin[Dependencies]:
    connection = WebSocketStreamDockConnection(arguments.port)
    return StreamDockPlugin(
        arguments,
        stream_dock=connection,
        action_registry=registry,
        action_dependencies=Dependencies(connection),
    )


if __name__ == "__main__":
    raise SystemExit(run_plugin_cli(build_application))
```

The action UUID must also be declared in the plugin's `manifest.json`.

## Property Inspector client

Copy the versioned JavaScript client into the plugin bundle:

```bash
mirabox-sdk copy-property-inspector \
  com.example.counter.sdPlugin/property-inspector
```

Use `--force` to replace a different existing copy. Include the client before
the action-specific script:

```html
<script src="mirabox-sdk.js"></script>
<script src="counter.js"></script>
```

The action script can use `window.MiraBoxPropertyInspector`:

```javascript
const client = window.MiraBoxPropertyInspector;
client.on("connected", ({ settings }) => console.log(settings));
client.sendToPlugin({ event: "refresh" });
client.updateSettings({ mode: "toggle" });
```

## Public API

- `events.py` contains typed inbound event models.
- `commands.py` contains outbound command models.
- `parser.py` validates wire JSON and reports exact invalid field paths.
- `codecs.py` converts plugin-owned settings and messages to typed objects.
- `Action` and `ActionRegistry` model action instances and registrations.
- `StreamDockPlugin` dispatches events and manages service lifecycle.
- `WebSocketStreamDockConnection` implements the Stream Dock transport.
- `run_plugin_cli` handles the standard executable arguments and shutdown.

The supported public surface is exported from `mirabox_sdk`. Other module-level
objects are implementation details unless documented otherwise.

## Checks

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests scripts examples
ruff check src tests scripts examples
ruff format --check src tests scripts examples
python -m build
python scripts/verify_distribution.py dist
```

## Releasing

1. Update the version in `pyproject.toml`, `src/mirabox_sdk/__init__.py`, and
   `CHANGELOG.md`.
2. Run all checks and build the distributions.
3. Create and push the matching tag, for example `v0.1.0`.
4. The release workflow publishes the wheel and source archive to PyPI through
   Trusted Publishing and attaches them to a GitHub Release.

## License

[MIT](https://github.com/Nekit678/MiraboxStreamDockSDK/blob/main/LICENSE)

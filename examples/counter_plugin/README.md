# Counter example plugin

This is a minimal binary Stream Dock plugin built with `mirabox-stream-dock-sdk`.
Each press increments a persisted counter; the Property Inspector can reset it.

From the SDK repository:

```bash
python -m pip install -e ".[dev]"
mirabox-sdk copy-property-inspector \
  examples/counter_plugin/com.example.counter.sdPlugin/property-inspector
PYTHONPATH=examples/counter_plugin/src python -m counter_plugin \
  -port 12345 \
  -pluginUUID com.example.counter \
  -registerEvent registerPlugin \
  -info '{"application":{"language":"en","platform":"windows","platformVersion":"11","version":"2.10"},"colors":{},"devicePixelRatio":1,"devices":[],"plugin":{"uuid":"com.example.counter","version":"0.1.0"}}'
```

Build the Windows executable from the SDK virtual environment:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --clean --noconfirm examples/counter_plugin/build.spec
Copy-Item dist\CounterPlugin.exe `
  examples\counter_plugin\com.example.counter.sdPlugin\
```

Copy the `.sdPlugin` directory to
`%APPDATA%\HotSpot\StreamDock\plugins\` and restart Stream Dock.

# Background service example

This complete plugin sends `Heartbeat N` to the Stream Dock log every second.
Its status action shows **Heartbeat** on appearance and **Pressed** on a key
press. The Python package, manifest, asset, PyInstaller spec and harness tests
use the current public SDK API, including the RT-02/RT-03 and DX-02 changes.

From the SDK repository root:

```bash
python -m pip install -e ".[dev]"
PYTHONPATH=examples/background_service/src python -m unittest discover -s examples/background_service/tests -v
```

The tests replay registration/action events and exercise readiness, cancellation
before readiness, command failure, command timeout, partial startup failure and
a failed join. They require neither Stream Dock nor a socket.

For a real source run, set `PYTHONPATH=examples/background_service/src` and run
`python -m heartbeat_plugin` with Stream Dock's standard `-port`, `-pluginUUID`,
`-registerEvent`, `-info` arguments. Importing `bootstrap` only registers actions;
it does not start the service or connect a socket.

On Windows, build, assemble and validate from the SDK root:

```powershell
python -m pip install "pyinstaller>=6,<7"
python -m PyInstaller --clean --noconfirm examples/background_service/build.spec
Copy-Item dist\HeartbeatPlugin.exe examples\background_service\com.example.heartbeat.sdPlugin\
$env:PYTHONPATH = "examples/background_service/src"
mirabox-sdk validate-plugin examples/background_service/com.example.heartbeat.sdPlugin --registry heartbeat_plugin.bootstrap:ACTION_REGISTRY
```

Copy the complete `com.example.heartbeat.sdPlugin` directory into
`%APPDATA%\HotSpot\StreamDock\plugins\`, restart Stream Dock and add
**Examples → Heartbeat**. The source bundle intentionally fails validation until
the executable is built. Validation checks files and UUIDs, and does not prove
binary or device compatibility. For a new plugin, follow the SDK README's
`init-plugin` quickstart.

## Ownership and shutdown rules

`service_factories=(HeartbeatService,)` supplies the same `ApplicationContext`
used by actions. `start()` starts the worker and returns immediately: services
start before the runtime connects, so waiting for readiness in `start()` would
deadlock. The worker waits in 50 ms increments, checking both local cancellation
and `context.stop_signal`; terminal readiness ends the wait and preserves an
initialization failure.

Commands go through `context.stream_dock.send_async()` and
`completion.result(timeout=0.25)`. A timeout ends this worker; it does not retract
an already submitted command. Any external I/O added here needs its own finite
timeout. Background threads must use the sender or their own locked data,
instead of reading or mutating an action's context-local fields.

`application.stop()` requests cooperative cancellation before runtime resources
close. The worker checks it before each new operation and at most every 50 ms
during idle/readiness waits. An in-flight command may take up to its 250 ms wait.
Only a closed command bus during requested cancellation is treated as expected;
other failures are retained and surfaced by `service.stop()` with their cause.

`stop()` sets the local signal and joins for at most one second. Repeated calls
are safe, including before startup. If the thread is still alive, it raises
`TimeoutError` and retains ownership; shared resources must remain open until
the worker actually exits. A daemon thread is a process-exit fallback, not a
shutdown guarantee. If `start()` fails, the service rolls back its own partial
initialization because the SDK only stops successfully started services.

The SDK releases services in reverse order after callbacks, action cleanup and
plugin cleanup finish. A callback that outlives the shared five-second
`shutdown_timeout` can defer service cleanup. `callback_drain_timeout` only
bounds the shutdown drain wait, and cannot terminate a running callback.
Inspect a fresh `application.shutdown_outcome` for `complete`, `successful`,
`unfinished_callbacks`, `pending_cleanup` and `cleanup_failures`; a timeout is
not evidence that resources are safe to close. The example entry point uses
`run_plugin_cli`, which returns exit code 1 for failed or incomplete shutdown.

`observe_error` demonstrates the synchronous, potentially concurrent SDK error
observer using managed logging and safe metadata only. Worker failures are
recorded separately by the service and become shutdown cleanup failures; the
SDK observer covers parser and action/plugin callbacks, not arbitrary threads.
Exception messages and `diagnostic.error` may contain secrets and are not logged.
The entry point also filters CLI lifecycle exception records to retain the error
type without rendering the worker exception or its chained traceback.

# Changelog

All notable changes to this project are documented in this file. The project
uses [Semantic Versioning](https://semver.org/); releases before `1.0.0` may
change public APIs between minor versions.

## [Unreleased]

### Added

- Add public `SdkDiagnostic` / `SourceLocation` and an application/harness
  `error_observer` for protocol and action/plugin callback failures (DX-02).
  Default error logs identify schema paths and traceback locations without
  exception messages, source text, locals, or arbitrary settings keys.
- Expose immutable `ShutdownOutcome` / `ShutdownFailure` diagnostics and a shared
  `ApplicationContext.stop_signal` for cooperative cancellation (RT-02, RT-03).

- Add `mirabox-sdk validate-plugin PATH` and public `validate_plugin()`
  (SDK-DX-001) for offline manifest, code/resource path, local HTML dependency
  and bundled Property Inspector client checks. The explicit
  `--registry MODULE:OBJECT` option compares action UUIDs in both directions.
- Load Counter action decorators in its bootstrap so source validation and
  executable startup see the same populated registry without test imports.
- Add `mirabox_sdk.testing.StreamDockHarness` and `FakeStreamDockSender` for
  public in-memory integration and action tests (SDK-TEST-001). The harness
  exercises production registration, settings, routing and command serialization
  with bounded waits, joined shutdown, and runtime/service error propagation.
- Export every immutable leaf metric type referenced by runtime snapshots.

### Changed

- Adopt a facade-first API (SDK-API-001): remove `ActionFactory`,
  `HandlerSchedulerFactory`, `WebSocketConnectorFactory`, `StreamDockRuntime`
  and `StreamDockRuntimeLifecycleError` from supported exports. Remove the
  `scheduler_factory` and `connector_factory` application parameters; use
  `RuntimeDispatcherConfig` for scheduler selection and `StreamDockHarness`
  for tests. Internal SDK injection seams remain private.
- Migrate Counter unit and integration tests to public testing helpers.

### Fixed

- Restore inbound/outbound DEBUG protocol traces and `include_payload=True`,
  reusing decoded/serialized frames and restoring redaction on reconfiguration
  (DX-01).
- Reject Property Inspector sends before initialization or after close explicitly,
  and run documented startup operations from `connected` (PI-01).
- Snapshot deferred PI messages and nested settings as JSON on acceptance,
  preserve local state on rejected writes, and report individual deferred send
  failures through `sendError` without losing later messages (PI-02).
- Run executable Property Inspector regression tests with Node.js in CI and releases.
- Terminalize scheduler events after `BaseException`, preserve the fatal cause
  and supervise unexpected reader/writer exits, including partial pool startup
  (RT-01).
- Share one shutdown deadline across boundary, runtime and services; defer
  resource cleanup until callbacks and workers finish. Add `callback_drain_timeout`
  as the explicit name for the deprecated `callback_timeout` shutdown wait (RT-02).
- Report cleanup failures and unfinished shutdown publicly without replacing a
  primary runtime failure; return CLI status `1` for unsuccessful shutdown (RT-03).

- Inspect internal directory symlinks when validating bundled Property Inspector
  clients (SDK-DX-002), scanning each resolved directory once to avoid cycles.
  Reject symlinks outside the bundle without inspecting their targets and report
  unresolvable links and directory read failures as diagnostics.
- Reject incompatible action dependencies at registration (SDK-TYPE-002)
  through the public `ActionRegistration` decorator protocol, preserving the
  concrete action class and settings type. Cover registration with strict
  consumer checks and negative fixtures against source and installed wheels.
- Align developer documentation with the current implementation (SDK-DOC-001):
  replace retired architecture/parity guidance with source and test links in
  the protocol map, document the current scheduler benchmark, and confirm
  source and wheel typing checks in development and release checklists.
- Check the real typed consumer API (SDK-TYPE-001): follow imports across SDK
  sources, preserve dependency types through runtime composition, and verify
  positive, invalid-call and `Any`-guard fixtures against an isolated wheel
  installation in CI and releases. Ship checked JSON boundary stubs while
  keeping private copy-on-write container typing separate; verify stub/runtime
  signatures and public `get_type_hints()` against the installed wheel.
- Resolve recursive JSON annotations in public API type introspection, with a
  regression check for transitive public types and wheel-installed testing.

- Share canonical application dependencies with plugins (SDK-PLUGIN-CTX-001)
  through `plugin_factory(ApplicationContext)`, mutually exclusive with
  `plugin` and `plugin_hooks`. Add once-only `Plugin.on_ready()` and
  `Plugin.on_stop()` callbacks for session work and cleanup, with isolated,
  redacted callback failures and cleanup after partial ready initialization.
- Remove dependency-factory guessing (SDK-FACTORY-001):
  `action_dependencies_factory` always receives `ApplicationContext`.
  Sender-only factories can use `lambda ctx: factory(ctx.stream_dock)` or the
  deprecated `legacy_action_dependencies_factory` parameter; passing both
  factory parameters is rejected before boundary creation.
- Close the global-settings mutable escape (SDK-SETTINGS-002): remove the
  facade's `settings` property and return isolated copies from the state
  backend, preserving rollback-safe writes and isolated snapshots.

## [0.5.0] - 2026-08-15

### Added

- Add the stable `mirabox_sdk.runtime` package and package-level
  `create_stream_dock_application()` production composition factory.
- Add public immutable runtime/boundary metrics, queue/shutdown configuration,
  plugin hooks, and global-settings methods.
- Add `ApplicationService` and ordered `services=` lifecycle management around
  the Stream Dock runtime, including reverse and partial-startup cleanup.
- Add the runtime-owned `GlobalSettings` facade, `ApplicationContext`, and
  context-aware dependency/service factories so application collaborators share
  one rollback-safe global settings store.
- Add `ApplicationContext.session_readiness` so service workers can wait for
  completed Stream Dock session initialization without blocking process start.
- Add executable scheduler performance checks with throughput,
  callback-latency, boundedness, and prefetch-coalescing budgets.
- Add release gates that keep supported Python metadata aligned with CI and
  verify the stable runtime, completion, typing, wheel, and sdist surface.

### Changed

- Redact callback exception messages and disconnect reasons from runtime
  diagnostics while retaining event, context, status, and exception-type
  metadata.
- Make the bounded keyed-serial scheduler the default with four workers and a
  pending limit of 64; sequential dispatch remains explicitly selectable.
- Unify action and boundary command completion and submission errors around one
  canonical `CommandFuture` contract.
- Switch the Counter example unconditionally to the typed boundary and runtime
  dispatcher.

### Removed

- Remove the public `StreamDockPlugin`, `WebSocketStreamDockConnection`,
  connection/listener runtime contracts, combined `EVENT_REGISTRY`, and legacy
  store/queue metrics from the package-level API.
- Remove `mirabox_sdk.experimental`, its environment switches, the transitional
  boundary application adapter, and the future/sender compatibility adapters.
- Remove the legacy direct-module runtime (`connection`, `inbound`, `outbound`,
  `plugin`, and `stores`), its `EVENT_REGISTRY` compatibility adapter, and the
  obsolete connection/listener protocols. These imports were unsupported after
  0.4.0 and are removed in this 0.5.0 breaking pre-1.0 release.
- Replace the temporary `mirabox_sdk._next` namespace with the private
  `mirabox_sdk._internal` implementation namespace. Neither is a supported
  application import path.

## [0.4.0] - 2026-07-28

### Added

- Add a private experimental Stream Dock boundary with API-independent
  WebSocket transport, strict protocol codecs, bounded raw and typed queues,
  per-command completion, session events, graceful shutdown, and aggregate
  metrics.
- Add the opt-in `mirabox_sdk.experimental` runtime adapter and Counter example
  switch while retaining `WebSocketStreamDockConnection` as the default.
- Add `StreamDockPlugin.update_global_settings()` for atomic, rollback-safe
  updates and persistence of global settings.
- Add reusable `ValidatedJsonObject`, `OwnedJsonPayload`, and
  `ValidatedWireMessage` ownership and command-boundary types.
- Add `StreamDockPlugin.on_unhandled_event()` so forward-compatible
  `UnknownStreamDockEvent` envelopes reach application code.
- Add a bounded asynchronous inbound event queue with configurable overflow,
  metrics, graceful draining, per-context ordering, and optional dial-rotation
  coalescing.
- Add a bounded outbound command bus with one serialization and WebSocket
  writer thread, FIFO ordering, explicit overflow and shutdown errors, metrics,
  graceful draining, and optional state-command coalescing.
- Add a bounded managed logging queue with configurable overflow, a process-wide
  dropped-record counter, and priority admission and delivery for ERROR records.
- Add `send_async()`, `CommandFuture`, and non-blocking action helpers for
  image, title, and state display updates.
- Add `ActionStore` and `GlobalSettingsStore` as the dedicated owners of
  runtime action routing and global-settings state.

### Changed

- Emit per-message protocol metadata only at DEBUG while retaining connection
  lifecycle and operational records at INFO.
- Move outbound validation behind `StreamDockCommand.to_validated_wire()`, so
  the WebSocket transport no longer depends on a private command marker.
- Drive known-event parsing, routing scope, callbacks, and special runtime
  handling from one validated, read-only `EVENT_REGISTRY`.
- Move plugin callbacks out of the `websocket-client` reader thread.
- Replace the single inbound callback worker with a configurable keyed-serial
  pool: action contexts can progress concurrently, while lifecycle, broadcast,
  and unknown events retain global ordering through exclusive barriers.
- Route every outbound command through the connection-owned writer while
  preserving synchronous serialization and transport error reporting.
- Route SDK records through one managed logging queue so destination stream and
  rotating-file I/O does not run on protocol or application worker threads.
- Formalize the supported lifecycle, callback, command, COW-view, concurrent
  send, and shutdown thread contract.
- Bound inbound shutdown to five seconds by default while retaining `None` as
  an explicit opt-in to an unbounded callback drain.

### Fixed

- Fail fast with `OutboundCommandBusNotReadyError` when commands or synchronous
  global-settings setters are called before the outbound writer starts.
- Prevent inbound overflow from discarding lifecycle, settings, broadcast,
  unknown, and other stateful events; only explicitly coalescable rotations are
  eligible for dropping, while a full lossless queue applies bounded
  backpressure.
- Isolate action settings from both caller-owned values and outbound command
  payloads after a successful settings update.
- Validate and isolate mutations of runtime global settings before committing
  them, so failed updates leave the public view and replay state unchanged.
- Record callback shutdown timeouts separately and log the active event name
  and context when a callback prevents the inbound dispatcher from draining.

### Performance

- Validate and clone retained event and codec JSON in one traversal, avoid
  copying unused fields from known event envelopes, and serialize owned action,
  global-settings, and Property Inspector payloads without a separate recursive
  pre-validation pass.
- Reuse the owned action-settings snapshot for isolated local state after
  `Action.set_settings()` instead of decoding from another deep copy.
- Share one prepared global-settings snapshot across action broadcasts, keep
  dictionary changes in sparse overlays, and materialize lists only for
  structural mutations, avoiding per-action copies of wide roots.
- Batch consecutive runtime global-settings mutations and rebuild their replay
  snapshot only once when it is next needed.
- Reuse serialized WebSocket frames for outbound DEBUG payload logging instead
  of encoding the same command twice.

## [0.3.1] - 2026-07-19

### Documentation

- Add comprehensive IDE docstrings for the public Python API, covering
  parameters, return values, exceptions, lifecycle behavior, and side effects.
- Add JSDoc for the Property Inspector browser client and keep the bundled
  example copy synchronized.

## [0.3.0] - 2026-07-19

### Added

- Add an explicit `include_payload=True` logging option for temporary full
  protocol diagnostics while keeping payloads redacted by default.

## [0.2.0] - 2026-07-19

### Added

- Keep SDK logging disabled by default and add `configure_logging()` for
  isolated console or rotating UTF-8 file diagnostics, repeatable level
  changes, and explicit suppression.

### Documentation

- Distinguish the declared minimum Stream Dock version `2.10.179.426` from the
  manually verified runtime version `3.10.203.0701`.

## [0.1.2] - 2026-07-19

### Fixed

- Reject non-finite numbers and other non-JSON values at the WebSocket boundary.
- Preserve action and global settings state when encoding or sending an update
  fails.
- Replay the latest global settings to actions created later, including settings
  set before the first response, and isolate action callbacks with defensive
  copies.

### Security

- Redact all protocol payloads from INFO and DEBUG logs while retaining routing
  metadata useful for diagnostics.

## [0.1.1] - 2026-07-19

### Added

- Added comprehensive English and Russian guides, project artwork, and the
  project's extraction history and development status.
- Added a protocol map linking the SDK surface to the official MiraBox Stream
  Dock documentation, templates, events, commands, manifest, and Property
  Inspector API.
- Added contributor and release guides, issue forms, and a pull request
  checklist.

### Changed

- Added Python 3.14 to package metadata and the Linux/Windows CI matrix.
- Updated GitHub Actions to their current major releases and added package
  metadata and README rendering validation with Twine.

## [0.1.0] - 2026-07-19

- Added typed models for Stream Dock registration, events, and commands.
- Added strict JSON parsing with diagnostic field paths.
- Added typed codecs for settings and Property Inspector messages.
- Added the reusable action registry and plugin runtime.
- Added the WebSocket transport and common CLI lifecycle runner.
- Added the shared Property Inspector JavaScript client.

[Unreleased]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/releases/tag/v0.1.0

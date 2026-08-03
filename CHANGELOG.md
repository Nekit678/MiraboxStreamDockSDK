# Changelog

All notable changes to this project are documented in this file. The project
uses [Semantic Versioning](https://semver.org/); releases before `1.0.0` may
change public APIs between minor versions.

## [Unreleased]

### Added

- Add an explicit experimental application factory that composes the typed
  boundary directly with the new runtime dispatcher, plus a separate Counter
  example opt-in and migration diagnostics while preserving both legacy paths.
- Add shared legacy/experimental runtime behavioral contracts and an executable
  scheduler performance gate with throughput, callback-latency, boundedness,
  and boundary-coalescing budgets.
- Add release gates that keep supported Python metadata aligned with CI and
  verify the explicit experimental runtime/type distribution surface.

### Changed

- Redact callback exception messages and disconnect reasons from legacy,
  experimental-adapter, and new-runtime diagnostics while retaining event,
  context, status, and exception-type metadata.

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

[Unreleased]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/releases/tag/v0.1.0

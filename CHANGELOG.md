# Changelog

All notable changes to this project are documented in this file. The project
uses [Semantic Versioning](https://semver.org/); releases before `1.0.0` may
change public APIs between minor versions.

## [Unreleased]

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

[Unreleased]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Nekit678/MiraboxStreamDockSDK/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Nekit678/MiraboxStreamDockSDK/releases/tag/v0.1.0

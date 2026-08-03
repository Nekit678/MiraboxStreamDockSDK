"""Verify the contents of built MiraBox SDK wheel and source distributions."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    from .verify_version import verify_version
except ImportError:  # pragma: no cover - direct script execution
    from verify_version import verify_version

WHEEL_REQUIRED_SUFFIXES = {
    "mirabox_sdk/__init__.py",
    "mirabox_sdk/_next/boundary/composition.py",
    "mirabox_sdk/_next/runtime/adapters/action_registry.py",
    "mirabox_sdk/_next/runtime/composition.py",
    "mirabox_sdk/_next/runtime/config.py",
    "mirabox_sdk/_next/runtime/keyed_scheduler.py",
    "mirabox_sdk/_next/runtime/metrics.py",
    "mirabox_sdk/completion.py",
    "mirabox_sdk/py.typed",
    "mirabox_sdk/runtime/__init__.py",
    "mirabox_sdk/runtime/application.py",
    "mirabox_sdk/runtime/config.py",
    "mirabox_sdk/runtime/metrics.py",
    "mirabox_sdk/runtime/ports.py",
    "mirabox_sdk/property_inspector/mirabox-sdk.js",
}
WHEEL_FORBIDDEN_SUFFIXES = {
    "mirabox_sdk/_next/protocol/adapters/legacy.py",
    "mirabox_sdk/_next/runtime/adapters/legacy_actions.py",
    "mirabox_sdk/experimental.py",
}
SDIST_REQUIRED_SUFFIXES = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "README.ru.md",
    "RELEASING.md",
    "docs/PROTOCOL.md",
    "docs/assets/logo.svg",
    "examples/counter_plugin/com.example.counter.sdPlugin/manifest.json",
    "examples/counter_plugin/src/counter_plugin/__main__.py",
    "pyproject.toml",
    "src/mirabox_sdk/_next/boundary/composition.py",
    "src/mirabox_sdk/_next/runtime/adapters/action_registry.py",
    "src/mirabox_sdk/_next/runtime/composition.py",
    "src/mirabox_sdk/_next/runtime/config.py",
    "src/mirabox_sdk/_next/runtime/keyed_scheduler.py",
    "src/mirabox_sdk/_next/runtime/metrics.py",
    "src/mirabox_sdk/completion.py",
    "src/mirabox_sdk/py.typed",
    "src/mirabox_sdk/runtime/__init__.py",
    "src/mirabox_sdk/runtime/application.py",
    "src/mirabox_sdk/runtime/config.py",
    "src/mirabox_sdk/runtime/metrics.py",
    "src/mirabox_sdk/runtime/ports.py",
    "src/mirabox_sdk/property_inspector/mirabox-sdk.js",
}
SDIST_FORBIDDEN_SUFFIXES = {
    "examples/counter_plugin/src/counter_plugin/plugin.py",
    "src/mirabox_sdk/_next/protocol/adapters/legacy.py",
    "src/mirabox_sdk/_next/runtime/adapters/legacy_actions.py",
    "src/mirabox_sdk/experimental.py",
}


def _single_match(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"Expected one {pattern!r} in {directory}, found {len(matches)}")
    return matches[0]


def _has_suffix(names: set[str], suffix: str) -> bool:
    suffix_parts = PurePosixPath(suffix).parts
    return any(PurePosixPath(name).parts[-len(suffix_parts) :] == suffix_parts for name in names)


def verify_distribution(directory: Path) -> tuple[Path, Path]:
    version = verify_version()
    normalized_version = version.replace("-", "_")
    wheel = _single_match(directory, f"mirabox_stream_dock_sdk-{normalized_version}-*.whl")
    source = _single_match(directory, f"mirabox_stream_dock_sdk-{version}.tar.gz")

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    missing_wheel = sorted(
        suffix for suffix in WHEEL_REQUIRED_SUFFIXES if not _has_suffix(wheel_names, suffix)
    )
    if missing_wheel:
        raise ValueError(f"Wheel is missing required files: {', '.join(missing_wheel)}")
    forbidden_wheel = sorted(
        suffix for suffix in WHEEL_FORBIDDEN_SUFFIXES if _has_suffix(wheel_names, suffix)
    )
    if forbidden_wheel:
        raise ValueError(f"Wheel contains removed files: {', '.join(forbidden_wheel)}")

    with tarfile.open(source, mode="r:gz") as archive:
        source_names = {member.name for member in archive.getmembers() if member.isfile()}
    missing_source = sorted(
        suffix for suffix in SDIST_REQUIRED_SUFFIXES if not _has_suffix(source_names, suffix)
    )
    if missing_source:
        raise ValueError(f"Source archive is missing required files: {', '.join(missing_source)}")
    forbidden_source = sorted(
        suffix for suffix in SDIST_FORBIDDEN_SUFFIXES if _has_suffix(source_names, suffix)
    )
    if forbidden_source:
        raise ValueError(f"Source archive contains removed files: {', '.join(forbidden_source)}")

    return wheel, source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        wheel, source = verify_distribution(args.directory)
    except (OSError, tarfile.TarError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Distribution verification failed: {exc}", file=sys.stderr)
        return 1
    print(wheel)
    print(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

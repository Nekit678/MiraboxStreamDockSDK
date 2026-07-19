"""Verify the contents of built MiraBox SDK wheel and source distributions."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from verify_version import verify_version

WHEEL_REQUIRED_SUFFIXES = {
    "mirabox_sdk/__init__.py",
    "mirabox_sdk/py.typed",
    "mirabox_sdk/property_inspector/mirabox-sdk.js",
}
SDIST_REQUIRED_SUFFIXES = {
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "examples/counter_plugin/com.example.counter.sdPlugin/manifest.json",
    "examples/counter_plugin/src/counter_plugin/__main__.py",
    "pyproject.toml",
    "src/mirabox_sdk/py.typed",
    "src/mirabox_sdk/property_inspector/mirabox-sdk.js",
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

    with tarfile.open(source, mode="r:gz") as archive:
        source_names = {member.name for member in archive.getmembers() if member.isfile()}
    missing_source = sorted(
        suffix for suffix in SDIST_REQUIRED_SUFFIXES if not _has_suffix(source_names, suffix)
    )
    if missing_source:
        raise ValueError(f"Source archive is missing required files: {', '.join(missing_source)}")

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

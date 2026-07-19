"""Access versioned non-Python assets distributed with the SDK."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

PROPERTY_INSPECTOR_CLIENT_FILENAME = "mirabox-sdk.js"


def property_inspector_client_bytes() -> bytes:
    """Return the bundled Property Inspector client."""

    return (
        files("mirabox_sdk")
        .joinpath("property_inspector", PROPERTY_INSPECTOR_CLIENT_FILENAME)
        .read_bytes()
    )


def copy_property_inspector_client(
    destination_directory: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Copy the shared client into a plugin's Property Inspector directory."""

    destination = Path(destination_directory)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / PROPERTY_INSPECTOR_CLIENT_FILENAME
    content = property_inspector_client_bytes()

    if target.exists():
        if target.is_dir():
            raise IsADirectoryError(f"Property Inspector client target is a directory: {target}")
        if target.read_bytes() == content:
            return target
        if not overwrite:
            raise FileExistsError(
                f"Refusing to replace a different Property Inspector client: {target}"
            )

    target.write_bytes(content)
    return target

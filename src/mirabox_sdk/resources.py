"""Access versioned non-Python assets distributed with the SDK."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

PROPERTY_INSPECTOR_CLIENT_FILENAME = "mirabox-sdk.js"


def property_inspector_client_bytes() -> bytes:
    """Read the version-matched Property Inspector JavaScript client.

    Returns:
        Raw bytes of ``mirabox-sdk.js`` distributed inside the installed Python
        package. Reading through :mod:`importlib.resources` also works when the
        package is not represented by ordinary source files.
    """

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
    """Copy the bundled client into a Property Inspector directory.

    The destination directory and missing parents are created automatically.
    An existing byte-identical client is left in place regardless of
    ``overwrite``.

    Args:
        destination_directory: Directory inside the plugin bundle that should
            receive :data:`PROPERTY_INSPECTOR_CLIENT_FILENAME`.
        overwrite: Replace a different existing file when ``True``. The default
            protects local modifications and stale versioned copies.

    Returns:
        Path to the copied or already-identical client file.

    Raises:
        IsADirectoryError: If the target filename already names a directory.
        FileExistsError: If a different file exists and ``overwrite`` is false.
        OSError: If directories or the destination file cannot be read/written.
    """

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

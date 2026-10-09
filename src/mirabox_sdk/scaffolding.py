"""Create a version-matched plugin project from packaged templates."""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from importlib.resources import files
from pathlib import Path

from . import __version__
from .resources import copy_property_inspector_client

_PROJECT_FILES = {
    "bootstrap.py": "src/dock_plugin/bootstrap.py",
    "main.py": "src/dock_plugin/__main__.py",
    "init.py": "src/dock_plugin/__init__.py",
    "test_plugin.py": "tests/test_plugin.py",
    "build.spec": "build.spec",
    "pyproject.toml": "pyproject.toml",
    "README.md": "README.md",
    "gitignore": ".gitignore",
    "manifest.json": "@@BUNDLE@@/manifest.json",
    "inspector.html": "@@BUNDLE@@/property-inspector/inspector.html",
    "inspector.js": "@@BUNDLE@@/property-inspector/inspector.js",
    "icon.svg": "@@BUNDLE@@/assets/icon.svg",
}


def init_plugin(destination: Path, *, plugin_uuid: str, name: str) -> Path:
    """Create a new project; refuse existing paths and roll back partial writes.

    The only variable path component is a validated reverse-domain UUID. Names
    are inserted as JSON string literals, which are also valid Python/TOML.
    Templates and the PI client come from the same installed SDK distribution.
    This helper is used by the CLI and is not part of the exported Python API.
    """
    if (
        len(plugin_uuid) > 200
        or plugin_uuid.count(".") < 2
        or re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", plugin_uuid) is None
    ):
        raise ValueError("UUID must be a lowercase reverse-domain name, e.g. com.example.hello")
    if not name.strip() or any(
        unicodedata.category(character) in {"Cc", "Cs"} for character in name
    ):
        raise ValueError("name must be non-empty and contain no control characters")

    substitutions = {
        "@@UUID@@": plugin_uuid,
        "@@UUID_JSON@@": json.dumps(plugin_uuid),
        "@@ACTION_JSON@@": json.dumps(f"{plugin_uuid}.press"),
        "@@NAME_JSON@@": json.dumps(name, ensure_ascii=False),
        "@@BUNDLE@@": f"{plugin_uuid}.sdPlugin",
        "@@SDK_VERSION@@": __version__,
    }

    def render(value: str) -> str:
        # One pass prevents a user-supplied name from expanding template tokens.
        return re.sub(r"@@[A-Z_]+@@", lambda match: substitutions[match.group()], value)

    templates = files("mirabox_sdk").joinpath("_templates", "plugin")
    project_files = {
        render(path): render(templates.joinpath(f"{template}.tmpl").read_text(encoding="utf-8"))
        for template, path in _PROJECT_FILES.items()
    }
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for relative_path, contents in project_files.items():
            target = destination / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        copy_property_inspector_client(
            destination / f"{plugin_uuid}.sdPlugin" / "property-inspector"
        )
    except BaseException as error:
        try:
            shutil.rmtree(destination)
        except OSError as cleanup_error:
            error.add_note(f"Failed to remove incomplete project: {cleanup_error}")
        raise
    return destination

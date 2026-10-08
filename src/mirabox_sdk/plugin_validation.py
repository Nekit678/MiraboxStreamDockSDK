"""Offline validation of Stream Dock manifests and assembled plugin bundles."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit

from .resources import PROPERTY_INSPECTOR_CLIENT_FILENAME, property_inspector_client_bytes

_UUID = re.compile(r"[a-z0-9.-]+")
_PLATFORMS = {"mac", "windows"}
_CONTROLLERS = {"Keypad", "Information", "SecondaryScreen", "Knob", "btn"}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


class _HtmlResources(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = {"script": "src", "link": "href", "img": "src", "source": "src"}.get(tag)
        for name, value in attrs:
            if name == attribute and value:
                self.references.append(value)


class _PluginValidator:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.issues: list[str] = []
        self.action_uuids: set[str] = set()
        self._checked_html: set[Path] = set()
        self._checked_clients: set[Path] = set()

    def error(self, path: str, message: str) -> None:
        self.issues.append(f"{path}: {message}")

    @staticmethod
    def field(path: str, name: str) -> str:
        return f"{path}.{name}" if path else name

    def mapping(self, value: object, path: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.error(path, "expected an object")
            return None
        return value

    def array(self, value: object, path: str, *, nonempty: bool = True) -> list[Any]:
        if not isinstance(value, list) or (nonempty and not value):
            self.error(path, "expected a non-empty array" if nonempty else "expected an array")
            return []
        return value

    def strings(
        self,
        obj: dict[str, Any],
        path: str,
        *,
        required: tuple[str, ...] = (),
        optional: tuple[str, ...] = (),
    ) -> None:
        for name in (*required, *optional):
            if name not in obj and name not in required:
                continue
            value = obj.get(name)
            if not isinstance(value, str) or (name in required and not value.strip()):
                self.error(
                    self.field(path, name),
                    "expected a non-empty string" if name in required else "expected a string",
                )

    def booleans(self, obj: dict[str, Any], path: str, names: tuple[str, ...]) -> None:
        for name in names:
            if name in obj and type(obj[name]) is not bool:
                self.error(self.field(path, name), "expected a boolean")

    def choices(self, value: object, path: str, allowed: set[str]) -> None:
        if not isinstance(value, str) or value not in allowed:
            self.error(path, f"expected one of {', '.join(sorted(allowed))}")

    def file(self, value: object, path: str, *, directory: Path | None = None) -> None:
        if not isinstance(value, str) or not value.strip():
            self.error(path, "expected a non-empty relative file path")
            return
        normalized = value.replace("\\", "/")
        try:
            if Path(normalized).is_absolute() or PureWindowsPath(value).drive:
                self.error(path, "expected a relative file path inside the bundle")
                return
            source = (directory or self.root) / normalized
            target = source.resolve()
            if not target.is_relative_to(self.root):
                self.error(path, "file resolves outside the bundle")
                return
            if not target.is_file():
                self.error(path, f"file not found: {value}")
                return
            if PROPERTY_INSPECTOR_CLIENT_FILENAME in (source.name, target.name):
                self.client(target, str(source.relative_to(self.root)))
            if target.suffix.lower() in {".html", ".htm"}:
                self.html(target)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error(path, f"cannot inspect file {value}: {exc}")

    def files(self, obj: dict[str, Any], path: str, names: tuple[str, ...]) -> None:
        for name in names:
            if name in obj:
                self.file(obj[name], self.field(path, name))

    def client(self, target: Path, path: str) -> None:
        if target in self._checked_clients:
            return
        self._checked_clients.add(target)
        if target.read_bytes() != property_inspector_client_bytes():
            self.error(
                path,
                "client differs from the installed SDK; refresh with "
                f"mirabox-sdk copy-property-inspector {target.parent} --force",
            )

    def bundled_clients(self) -> None:
        pending = [self.root]
        checked_directories: set[Path] = set()
        while pending:
            source = pending.pop()
            path = str(source.relative_to(self.root))
            try:
                target = source.resolve(strict=True)
                if not target.is_relative_to(self.root):
                    self.error(path, "path resolves outside the bundle")
                    continue
                if source != self.root and source.name == PROPERTY_INSPECTOR_CLIENT_FILENAME:
                    self.file(path, path)
                if target.is_dir() and target not in checked_directories:
                    checked_directories.add(target)
                    pending.extend(sorted(source.iterdir(), reverse=True))
            except (OSError, ValueError, RuntimeError) as exc:
                self.error(path, f"cannot inspect Property Inspector clients: {exc}")

    def html(self, target: Path) -> None:
        if target in self._checked_html:
            return
        self._checked_html.add(target)
        parser = _HtmlResources()
        parser.feed(target.read_text(encoding="utf-8-sig"))
        path = str(target.relative_to(self.root))
        for reference in parser.references:
            # A Windows drive is a local path, even though urlsplit treats it as a scheme.
            if PureWindowsPath(reference).drive and not reference.startswith("//"):
                self.file(reference, path, directory=target.parent)
                continue
            parts = urlsplit(reference)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            self.file(unquote(parts.path), path, directory=target.parent)

    def manifest(self, obj: dict[str, Any]) -> None:
        self.strings(
            obj,
            "",
            required=("Name", "Author", "Description", "Version", "CodePath", "Icon"),
            optional=(
                "Category",
                "CategoryIcon",
                "CodePathMac",
                "CodePathWin",
                "PropertyInspectorPath",
                "URL",
            ),
        )
        if type(obj.get("SDKVersion")) is not int or obj["SDKVersion"] != 1:
            self.error("SDKVersion", "expected integer 1")
        self.files(
            obj,
            "",
            (
                "CodePath",
                "CodePathMac",
                "CodePathWin",
                "Icon",
                "CategoryIcon",
                "PropertyInspectorPath",
            ),
        )
        for index, value in enumerate(self.array(obj.get("OS"), "OS")):
            path = f"OS[{index}]"
            platform_entry = self.mapping(value, path)
            if platform_entry is not None:
                self.choices(platform_entry.get("Platform"), f"{path}.Platform", _PLATFORMS)
                self.strings(platform_entry, path, required=("MinimumVersion",))
        if "Software" in obj:
            software = self.mapping(obj["Software"], "Software")
            if software is not None:
                self.strings(software, "Software", required=("MinimumVersion",))
        if "Nodejs" in obj:
            node = self.mapping(obj["Nodejs"], "Nodejs")
            if node is not None:
                self.strings(node, "Nodejs", required=("Version",), optional=("Debug",))
        if "ApplicationsToMonitor" in obj:
            applications = self.mapping(obj["ApplicationsToMonitor"], "ApplicationsToMonitor")
            if applications is not None:
                for platform in sorted(_PLATFORMS & applications.keys()):
                    path = f"ApplicationsToMonitor.{platform}"
                    for index, name in enumerate(
                        self.array(applications[platform], path, nonempty=False)
                    ):
                        if not isinstance(name, str) or not name.strip():
                            self.error(f"{path}[{index}]", "expected a non-empty string")
        for index, value in enumerate(self.array(obj.get("Actions"), "Actions")):
            path = f"Actions[{index}]"
            action = self.mapping(value, path)
            if action is not None:
                self.action(action, path)

    def action(self, obj: dict[str, Any], path: str) -> None:
        self.strings(
            obj,
            path,
            required=("UUID", "Name"),
            optional=("Tooltip", "Icon", "PropertyInspectorPath"),
        )
        uuid = obj.get("UUID")
        if not isinstance(uuid, str) or _UUID.fullmatch(uuid) is None:
            self.error(f"{path}.UUID", "expected lowercase letters, digits, periods or hyphens")
        elif uuid in self.action_uuids:
            self.error(f"{path}.UUID", f"duplicate action UUID: {uuid}")
        else:
            self.action_uuids.add(uuid)
        self.booleans(
            obj, path, ("SupportedInMultiActions", "UserTitleEnabled", "VisibleInActionsList")
        )
        if obj.get("VisibleInActionsList") is not False and "Icon" not in obj:
            self.error(f"{path}.Icon", "required for a visible action")
        self.files(obj, path, ("Icon", "PropertyInspectorPath"))
        for name, allowed in (("Controllers", _CONTROLLERS), ("OS", _PLATFORMS)):
            if name in obj:
                for index, value in enumerate(self.array(obj[name], f"{path}.{name}")):
                    self.choices(value, f"{path}.{name}[{index}]", allowed)
        if "Settings" in obj:
            self.mapping(obj["Settings"], f"{path}.Settings")
        states = self.array(obj.get("States"), f"{path}.States")
        if "state" in obj:
            state = obj["state"]
            if type(state) is not int or not 0 <= state < len(states):
                self.error(f"{path}.state", "expected an index into States")
        for index, value in enumerate(states):
            state_path = f"{path}.States[{index}]"
            state_obj = self.mapping(value, state_path)
            if state_obj is None:
                continue
            self.strings(
                state_obj,
                state_path,
                required=("Image",),
                optional=("Title", "TitleColor", "FontFamily", "FontStyle", "FontSize"),
            )
            self.booleans(state_obj, state_path, ("ShowTitle", "FontUnderline"))
            if "TitleAlignment" in state_obj:
                self.choices(
                    state_obj["TitleAlignment"],
                    f"{state_path}.TitleAlignment",
                    {"top", "bottom", "center"},
                )
            if "FontStyle" in state_obj:
                self.choices(
                    state_obj["FontStyle"],
                    f"{state_path}.FontStyle",
                    {"Regular", "Bold", "Italic", "Bold Italic"},
                )
            self.files(state_obj, state_path, ("Image",))


def validate_plugin(
    path: str | Path,
    *,
    action_uuids: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Check an assembled bundle without executing its plugin or contacting Stream Dock.

    Validate required manifest members and known optional field types, action
    UUID syntax/uniqueness, declared code and resource files, local HTML
    script/link/image references, and every bundled ``mirabox-sdk.js`` against
    the installed SDK's bytes. Unknown manifest members are accepted. Paths
    must resolve to files inside the bundle, including through symlinks.
    Bundle traversal follows internal directory symlinks, checking each
    resolved directory once. Symlinks outside the bundle are diagnostics;
    their targets are not inspected.

    Args:
        path: Plugin directory containing ``manifest.json``.
        action_uuids: Optional registered UUIDs, normally
            ``ActionRegistry.action_uuids``. Compare both directions when given;
            otherwise only manifest UUID syntax and uniqueness are checked.

    Returns:
        Field/file diagnostics, or an empty tuple when all requested checks
        pass. Invalid JSON, missing files and filesystem errors are diagnostics.
        This does not verify executable contents, image dimensions, remote
        resources, dynamic JavaScript imports or Stream Dock compatibility.
    """

    try:
        root = Path(path).resolve()
        if not root.is_dir():
            return (f"{path}: expected a plugin bundle directory",)
        manifest_path = (root / "manifest.json").resolve()
        if not manifest_path.is_relative_to(root):
            return ("manifest.json: file resolves outside the bundle",)
        data = json.loads(
            manifest_path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return (f"manifest.json: {exc}",)

    validator = _PluginValidator(root)
    manifest = validator.mapping(data, "manifest.json")
    if manifest is None:
        return tuple(validator.issues)
    validator.manifest(manifest)
    if action_uuids is not None:
        registered = set(action_uuids)
        for uuid in sorted(validator.action_uuids - registered):
            validator.error("Actions", f"manifest action UUID is not registered: {uuid}")
        for uuid in sorted(registered - validator.action_uuids):
            validator.error("Actions", f"registered action UUID is missing from manifest: {uuid}")
    validator.bundled_clients()
    return tuple(validator.issues)

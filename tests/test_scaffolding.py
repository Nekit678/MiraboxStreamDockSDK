"""Exercise generated projects as independent consumers of the installed SDK."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mirabox_sdk
from mirabox_sdk import __version__, property_inspector_client_bytes
from mirabox_sdk.resource_cli import main

SDK_IMPORT_ROOT = Path(mirabox_sdk.__file__).resolve().parents[1]


class ScaffoldingTests(unittest.TestCase):
    def _init(self, destination: Path, uuid: str, name: str) -> tuple[int, str]:
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = main(["init-plugin", str(destination), "--uuid", uuid, "--name", name])
        return result, output.getvalue()

    def _run(self, project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join((str(project / "src"), str(SDK_IMPORT_ROOT)))
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _validate(self, project: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
        return self._run(
            project,
            "-c",
            "from mirabox_sdk.resource_cli import main; raise SystemExit(main())",
            "validate-plugin",
            str(bundle),
            "--registry",
            "dock_plugin.bootstrap:ACTION_REGISTRY",
        )

    def test_independent_projects_replay_and_validate_with_matching_resources(self) -> None:
        scenarios = (
            ("com.example.hello", "Hello"),
            ("org.example.media-keys", "Media keys"),
            ("net.example.status", 'Статус 🟢 "quoted" @@UUID@@ `literal`'),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (uuid, name) in enumerate(scenarios):
                with self.subTest(uuid=uuid):
                    project = Path(directory) / f"plugin {index}"
                    result, output = self._init(project, uuid, name)
                    self.assertEqual(result, 0, output)
                    bundle = project / f"{uuid}.sdPlugin"
                    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
                    self.assertEqual(manifest["Name"], name)
                    self.assertEqual(manifest["Actions"][0]["UUID"], f"{uuid}.press")
                    metadata = tomllib.loads(
                        (project / "pyproject.toml").read_text(encoding="utf-8")
                    )
                    self.assertEqual(metadata["project"]["description"], name)
                    self.assertEqual(
                        metadata["project"]["dependencies"],
                        [f"mirabox-stream-dock-sdk=={__version__}"],
                    )
                    self.assertEqual(
                        (bundle / "property-inspector/mirabox-sdk.js").read_bytes(),
                        property_inspector_client_bytes(),
                    )
                    for source in project.rglob("*.py"):
                        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
                        for node in ast.walk(tree):
                            if isinstance(node, ast.ImportFrom) and node.module:
                                self.assertFalse(node.module.startswith("mirabox_sdk._internal"))
                    replay = self._run(project, "-m", "unittest", "discover", "-s", "tests", "-v")
                    self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
                    missing_binary = self._validate(project, bundle)
                    self.assertEqual(missing_binary.returncode, 1)
                    self.assertIn("Plugin.exe", missing_binary.stderr)
                    # A validator fixture proves file/UUID checks, not executable behavior.
                    (bundle / manifest["CodePath"]).write_bytes(b"validator test fixture")
                    assembled = self._validate(project, bundle)
                    self.assertEqual(assembled.returncode, 0, assembled.stderr)
                    self.assertNotIn("skipped", assembled.stdout.lower())
                    manifest["Actions"][0]["UUID"] = f"{uuid}.unknown"
                    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    mismatch = self._validate(project, bundle)
                    self.assertEqual(mismatch.returncode, 1)
                    self.assertIn("not registered", mismatch.stderr)

    def test_refuses_existing_directories_and_files_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for existing in (root, root / "empty", root / "file"):
                if existing.name == "empty":
                    existing.mkdir()
                elif existing.name == "file":
                    existing.write_text("keep", encoding="utf-8")
                result, output = self._init(existing, "com.example.hello", "Hello")
                self.assertEqual(result, 1)
                self.assertIn("Failed to create", output)
            self.assertEqual((root / "file").read_text(encoding="utf-8"), "keep")
            self.assertEqual(list((root / "empty").iterdir()), [])

    def test_rejects_unsafe_identifiers_and_names_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "plugin"
            for uuid in (
                "../com.example",
                "com.example/a",
                "Com.example.plugin",
                "com..example.plugin",
                "example",
                "com.example.-plugin",
                "com.example." + "a" * 201,
            ):
                with self.subTest(uuid=uuid):
                    result, _ = self._init(project, uuid, "Hello")
                    self.assertEqual(result, 1)
                    self.assertFalse(project.exists())
            for name in ("", "  ", "Hello\nInjected", "Hello\x00", "Hello\x7f", "Hello\ud800"):
                with self.subTest(name=name):
                    result, _ = self._init(project, "com.example.hello", name)
                    self.assertEqual(result, 1)
                    self.assertFalse(project.exists())

    def test_rolls_back_partial_project_on_resource_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "plugin"
            with patch(
                "mirabox_sdk.scaffolding.copy_property_inspector_client", side_effect=OSError
            ):
                result, _ = self._init(project, "com.example.hello", "Hello")
            self.assertEqual(result, 1)
            self.assertFalse(project.exists())


if __name__ == "__main__":
    unittest.main()

"""Public CLI and bundle validation regressions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mirabox_sdk import Action, ActionRegistry, ApplicationContext, copy_property_inspector_client
from mirabox_sdk.resource_cli import main

ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict[str, object]:
    return {
        "Name": "Counter",
        "Author": "Example",
        "Description": "Example plugin",
        "Version": "1.0.0",
        "SDKVersion": 1,
        "CodePath": "plugin.exe",
        "Icon": "icon.svg",
        "OS": [{"Platform": "windows", "MinimumVersion": "11"}],
        "Actions": [
            {
                "UUID": "com.example.counter",
                "Name": "Counter",
                "Icon": "icon.svg",
                "States": [{"Image": "icon.svg"}],
                "PropertyInspectorPath": "pi/index.html",
            }
        ],
    }


class PluginValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bundle = Path(temporary.name) / "counter.sdPlugin"
        self.bundle.mkdir()
        self.manifest = _manifest()
        self._write_manifest()
        (self.bundle / "plugin.exe").write_bytes(b"test executable placeholder")
        (self.bundle / "icon.svg").write_text("<svg/>", encoding="utf-8")
        inspector = self.bundle / "pi"
        copy_property_inspector_client(inspector)
        (inspector / "index.html").write_text(
            '<script src="mirabox-sdk.js"></script><script src="action.js"></script>',
            encoding="utf-8",
        )
        (inspector / "action.js").write_text("// action", encoding="utf-8")

    def _write_manifest(self) -> None:
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def _validate(self, **kwargs: object) -> tuple[str, ...]:
        from mirabox_sdk import validate_plugin

        return validate_plugin(self.bundle, **kwargs)

    def test_cli_accepts_complete_bundle_and_reports_skipped_registry_comparison(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            result = main(["validate-plugin", str(self.bundle)])

        self.assertEqual(result, 0)
        self.assertIn("valid", stdout.getvalue().lower())
        self.assertIn("--registry", stdout.getvalue())

    def test_public_validation_accepts_matching_action_uuids(self) -> None:
        self.assertEqual(self._validate(action_uuids={"com.example.counter"}), ())

    def test_uuid_comparison_reports_both_directions(self) -> None:
        issues = self._validate(action_uuids={"com.example.typo"})

        self.assertIn("com.example.counter", "\n".join(issues))
        self.assertIn("com.example.typo", "\n".join(issues))
        self.assertEqual(len(issues), 2)

    def test_reports_missing_code_paths_resources_and_html_dependencies(self) -> None:
        self.manifest["CodePathWin"] = "windows.exe"
        self.manifest["CodePathMac"] = "mac-plugin"
        self.manifest["PropertyInspectorPath"] = "missing.html"
        self._write_manifest()
        (self.bundle / "plugin.exe").unlink()
        (self.bundle / "icon.svg").unlink()
        (self.bundle / "pi" / "action.js").unlink()

        issues = "\n".join(self._validate())
        for field in ("CodePath:", "CodePathWin:", "CodePathMac:", "Icon:", "States[0].Image:"):
            with self.subTest(field=field):
                self.assertIn(field, issues)
        self.assertIn("missing.html", issues)
        self.assertIn("action.js", issues)

    def test_rejects_malformed_json_wrong_root_and_nonstandard_json(self) -> None:
        for content in ("{", "[]", '{"SDKVersion": NaN}', '{"Name":"a","Name":"b"}', "\xff"):
            with self.subTest(content=content):
                (self.bundle / "manifest.json").write_bytes(content.encode("latin1"))
                self.assertTrue(self._validate())

    def test_schema_errors_include_field_paths(self) -> None:
        cases = (
            ("Author", None, "Author"),
            ("SDKVersion", True, "SDKVersion"),
            ("SDKVersion", 2, "SDKVersion"),
            ("CodePath", "", "CodePath"),
            ("OS", [{"Platform": "linux", "MinimumVersion": 11}], "OS[0].Platform"),
            ("Software", {}, "Software.MinimumVersion"),
            ("Actions", [], "Actions"),
            ("Actions", [None], "Actions[0]"),
            ("Actions", [{"UUID": "INVALID", "States": []}], "Actions[0].UUID"),
            ("Actions", [{"UUID": "com.example.counter", "States": [None]}], "States[0]"),
            ("Actions", [{"States": [{"Image": 1}]}], "States[0].Image"),
        )
        for field, value, error in cases:
            with self.subTest(field=field, value=value):
                self.manifest = _manifest()
                self.manifest[field] = value
                self._write_manifest()
                self.assertIn(error, "\n".join(self._validate()))

    def test_duplicate_uuids_and_invalid_optional_action_fields(self) -> None:
        self.manifest["Actions"] = [
            {
                "UUID": "com.example.counter",
                "Name": "Counter",
                "Icon": "icon.svg",
                "States": [{"Image": "icon.svg", "ShowTitle": "yes"}],
                "Controllers": ["unknown"],
                "UserTitleEnabled": 1,
                "state": 9,
            }
        ] * 2
        self._write_manifest()
        issues = "\n".join(self._validate())
        for field in ("duplicate", "Controllers", "ShowTitle", "UserTitleEnabled", ".state"):
            with self.subTest(field=field):
                self.assertIn(field, issues)

    def test_accepts_optional_fields_hidden_action_and_unknown_extensions(self) -> None:
        self.manifest.update(
            {
                "Category": "",
                "Software": {"MinimumVersion": "2.10.179.426"},
                "ApplicationsToMonitor": {"windows": ["app.exe"]},
                "Nodejs": {"Version": "20", "Debug": "--inspect=127.0.0.1:3210"},
                "VendorExtension": {"arbitrary": True},
                "Actions": [
                    {
                        "UUID": "com.example.counter",
                        "Name": "Hidden",
                        "VisibleInActionsList": False,
                        "Controllers": ["Keypad", "Knob", "Information", "SecondaryScreen", "btn"],
                        "States": [{"Image": "icon.svg", "FontSize": "18"}],
                    }
                ],
            }
        )
        self._write_manifest()

        self.assertEqual(self._validate(), ())

    def test_rejects_paths_outside_bundle_and_directories(self) -> None:
        for value in ("../outside", "/etc/passwd", "C:\\plugin.exe", "C:plugin.exe", "pi", "\x00"):
            with self.subTest(value=value):
                self.manifest["CodePath"] = value
                self._write_manifest()
                self.assertIn("CodePath", "\n".join(self._validate()))

    def test_rejects_symlinks_outside_bundle(self) -> None:
        target = self.bundle.parent / "outside.js"
        target.write_text("outside", encoding="utf-8")
        link = self.bundle / "pi" / "action.js"
        link.unlink()
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        self.assertIn("outside", "\n".join(self._validate()))

    def test_checks_client_symlinks_to_differently_named_files_inside_bundle(self) -> None:
        target = self.bundle / "pi" / "old.js"
        target.write_text("// outdated SDK", encoding="utf-8")
        link = self.bundle / "pi" / "mirabox-sdk.js"
        link.unlink()
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        issues = "\n".join(self._validate())
        self.assertIn("mirabox-sdk.js", issues)
        self.assertIn("differs", issues)

    def test_checks_unreferenced_bundled_client_copies(self) -> None:
        client = self.bundle / "unused" / "mirabox-sdk.js"
        client.parent.mkdir()
        client.write_text("// outdated SDK", encoding="utf-8")

        self.assertIn("unused", "\n".join(self._validate()))

    def test_rejects_unreferenced_external_directory_symlinks_without_reading_targets(self) -> None:
        external = self.bundle.parent / "external"
        external.mkdir()
        (external / "mirabox-sdk.js").write_text("// outdated SDK", encoding="utf-8")
        link = self.bundle / "unused"
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        original_iterdir = Path.iterdir
        original_read_bytes = Path.read_bytes

        def iterdir(path: Path) -> Iterator[Path]:
            self.assertFalse(path.resolve().is_relative_to(external))
            return original_iterdir(path)

        def read_bytes(path: Path) -> bytes:
            self.assertFalse(path.resolve().is_relative_to(external))
            return original_read_bytes(path)

        with patch.object(Path, "iterdir", iterdir), patch.object(Path, "read_bytes", read_bytes):
            self.assertIn("unused: path resolves outside the bundle", self._validate())

    def test_accepts_internal_directory_symlinks_and_checks_unused_clients_once(self) -> None:
        target = self.bundle / "unused"
        client = copy_property_inspector_client(target)
        link = self.bundle / "alias"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        self.assertEqual(self._validate(), ())
        client.write_text("// outdated SDK", encoding="utf-8")

        issues = self._validate()
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("mirabox-sdk.js: client differs", issues[0])

    def test_internal_directory_symlink_cycles_are_not_traversed_repeatedly(self) -> None:
        unused = self.bundle / "unused"
        unused.mkdir()
        (unused / "mirabox-sdk.js").write_text("// outdated SDK", encoding="utf-8")
        try:
            (unused / "parent").symlink_to(self.bundle, target_is_directory=True)
            (unused / "self").symlink_to(unused, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        issues = self._validate()
        self.assertEqual(len(issues), 1, issues)
        self.assertIn(f"{Path('unused/mirabox-sdk.js')}: client differs", issues[0])

    def test_unresolvable_symlink_cycles_are_diagnostics(self) -> None:
        first = self.bundle / "first"
        second = self.bundle / "second"
        try:
            first.symlink_to(second, target_is_directory=True)
            second.symlink_to(first, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        issues = "\n".join(self._validate())
        self.assertIn("first: cannot inspect Property Inspector clients", issues)
        self.assertIn("second: cannot inspect Property Inspector clients", issues)

    def test_unreadable_directory_is_a_diagnostic_and_other_clients_are_checked(self) -> None:
        unreadable = self.bundle / "unreadable"
        unreadable.mkdir()
        unused = self.bundle / "unused"
        unused.mkdir()
        (unused / "mirabox-sdk.js").write_text("// outdated SDK", encoding="utf-8")
        original_iterdir = Path.iterdir

        def iterdir(path: Path) -> Iterator[Path]:
            if path == unreadable:
                raise PermissionError("permission denied")
            return original_iterdir(path)

        with patch.object(Path, "iterdir", iterdir):
            issues = "\n".join(self._validate())

        self.assertIn(
            "unreadable: cannot inspect Property Inspector clients: permission denied", issues
        )
        self.assertIn(f"{Path('unused/mirabox-sdk.js')}: client differs", issues)

    def test_cli_rejects_external_directory_symlinks(self) -> None:
        external = self.bundle.parent / "external"
        external.mkdir()
        try:
            (self.bundle / "unused").symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        with redirect_stderr(StringIO()) as stderr:
            self.assertEqual(main(["validate-plugin", str(self.bundle)]), 1)

        self.assertIn("unused", stderr.getvalue())
        self.assertIn("outside the bundle", stderr.getvalue())

    def test_manifest_symlink_cannot_escape_bundle(self) -> None:
        target = self.bundle.parent / "outside.json"
        target.write_text(json.dumps(self.manifest), encoding="utf-8")
        manifest = self.bundle / "manifest.json"
        manifest.unlink()
        try:
            manifest.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        self.assertIn("outside", "\n".join(self._validate()))

    def test_html_local_references_use_document_directory_and_ignore_remote_urls(self) -> None:
        (self.bundle / "pi" / "index.html").write_text(
            '<script src="mirabox-sdk.js?v=1"></script>'
            '<img src="../icon.svg#image"><link href="style%20sheet.css" rel="stylesheet">'
            '<script src="https://example.com/remote.js"></script>'
            '<img src="data:image/png;base64,AA"><script src="//example.com/remote.js"></script>',
            encoding="utf-8",
        )
        (self.bundle / "pi" / "style sheet.css").write_text("", encoding="utf-8")

        self.assertEqual(self._validate(), ())

    def test_checks_client_copies_and_does_not_require_sdk_client_for_custom_inspectors(
        self,
    ) -> None:
        client = self.bundle / "pi" / "mirabox-sdk.js"
        client.write_text("// outdated SDK", encoding="utf-8")
        issues = "\n".join(self._validate())
        self.assertIn("mirabox-sdk.js", issues)
        self.assertIn("copy-property-inspector", issues)
        client.unlink()
        self.assertIn("mirabox-sdk.js", "\n".join(self._validate()))
        (self.bundle / "pi" / "index.html").write_text("<html></html>", encoding="utf-8")
        self.assertEqual(self._validate(), ())

    def test_invalid_bundle_and_missing_or_unreadable_manifest_are_diagnostics(self) -> None:
        from mirabox_sdk import validate_plugin

        self.assertTrue(validate_plugin(self.bundle / "absent"))
        (self.bundle / "manifest.json").unlink()
        self.assertIn("manifest.json", "\n".join(self._validate()))
        self._write_manifest()
        with patch.object(Path, "read_text", side_effect=PermissionError("permission denied")):
            self.assertIn("permission denied", "\n".join(self._validate()))

    def test_unreadable_client_and_invalid_html_encoding_are_diagnostics(self) -> None:
        with patch.object(Path, "read_bytes", side_effect=PermissionError("permission denied")):
            self.assertIn("permission denied", "\n".join(self._validate()))
        (self.bundle / "pi" / "index.html").write_bytes(b"\xff")
        self.assertIn("pi/index.html", "\n".join(self._validate()))

    def test_cli_reports_validation_errors_with_nonzero_status(self) -> None:
        (self.bundle / "plugin.exe").unlink()
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = main(["validate-plugin", str(self.bundle)])

        self.assertEqual(result, 1)
        self.assertIn("CodePath", stderr.getvalue())

    def test_cli_loads_only_explicit_registry_and_compares_uuids(self) -> None:
        registry = ActionRegistry[ApplicationContext]()
        registry.register("com.example.counter")(Action)
        stdout = StringIO()
        with (
            patch("mirabox_sdk.resource_cli.import_module") as import_module,
            redirect_stdout(stdout),
        ):
            import_module.return_value.registry = registry
            result = main(["validate-plugin", str(self.bundle), "--registry", "plugin:registry"])
        self.assertEqual(result, 0)
        import_module.assert_called_once_with("plugin")
        self.assertNotIn("skipped", stdout.getvalue())

    def test_cli_registry_errors_are_reported_without_traceback(self) -> None:
        for reference in ("invalid", "does_not_exist:registry", "mirabox_sdk:Action"):
            with self.subTest(reference=reference), redirect_stderr(StringIO()) as stderr:
                result = main(["validate-plugin", str(self.bundle), "--registry", reference])
                self.assertEqual(result, 1)
                self.assertIn("registry", stderr.getvalue().lower())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_reports_registry_import_failures_and_uuid_mismatch(self) -> None:
        registry = ActionRegistry[ApplicationContext]()
        with (
            patch("mirabox_sdk.resource_cli.import_module") as import_module,
            redirect_stderr(StringIO()) as stderr,
        ):
            import_module.side_effect = RuntimeError("registration failed")
            self.assertEqual(
                main(["validate-plugin", str(self.bundle), "--registry", "plugin:registry"]), 1
            )
            self.assertIn("registration failed", stderr.getvalue())
            import_module.side_effect = None
            import_module.return_value.registry = registry
            self.assertEqual(
                main(["validate-plugin", str(self.bundle), "--registry", "plugin:registry"]), 1
            )
            self.assertIn("not registered", stderr.getvalue())

    def test_counter_example_from_fresh_process_reports_only_unbuilt_executable(self) -> None:
        # A fresh import must load action decorators without relying on other tests.
        destination = self.bundle.parent / "example.sdPlugin"
        shutil.copytree(ROOT / "examples/counter_plugin/com.example.counter.sdPlugin", destination)
        (destination / "CounterPlugin.exe").unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "examples" / "counter_plugin" / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from mirabox_sdk.resource_cli import main; "
                "import sys; sys.exit(main(sys.argv[1:]))",
                "validate-plugin",
                str(destination),
                "--registry",
                "counter_plugin.bootstrap:ACTION_REGISTRY",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertIn("CodePath", result.stderr)
        self.assertIn("CounterPlugin.exe", result.stderr)

    def test_counter_example_validates_when_executable_is_present(self) -> None:
        from mirabox_sdk import validate_plugin

        destination = self.bundle.parent / "example.sdPlugin"
        shutil.copytree(ROOT / "examples/counter_plugin/com.example.counter.sdPlugin", destination)
        (destination / "CounterPlugin.exe").write_bytes(b"test executable placeholder")

        self.assertEqual(
            validate_plugin(destination, action_uuids={"com.example.counter.increment"}), ()
        )


if __name__ == "__main__":
    unittest.main()

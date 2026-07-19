"""Tests for distributable SDK resources and their command-line helper."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from mirabox_sdk import (
    PROPERTY_INSPECTOR_CLIENT_FILENAME,
    copy_property_inspector_client,
    property_inspector_client_bytes,
)
from mirabox_sdk.resource_cli import main


class PropertyInspectorResourceTests(unittest.TestCase):
    def test_bundled_client_exposes_stream_dock_callbacks(self) -> None:
        client = property_inspector_client_bytes().decode("utf-8")

        self.assertIn("window.connectElgatoStreamDeckSocket", client)
        self.assertIn("window.MiraBoxPropertyInspector", client)
        self.assertIn("sendToPlugin(payload)", client)
        self.assertIn("setSettings(settings)", client)

    def test_copies_client_and_keeps_identical_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "property-inspector"

            first_target = copy_property_inspector_client(destination)
            second_target = copy_property_inspector_client(destination)

            self.assertEqual(first_target, second_target)
            self.assertEqual(first_target.name, PROPERTY_INSPECTOR_CLIENT_FILENAME)
            self.assertEqual(first_target.read_bytes(), property_inspector_client_bytes())

    def test_requires_overwrite_for_different_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            target = destination / PROPERTY_INSPECTOR_CLIENT_FILENAME
            target.write_text("stale", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                copy_property_inspector_client(destination)

            copy_property_inspector_client(destination, overwrite=True)

            self.assertEqual(target.read_bytes(), property_inspector_client_bytes())

    def test_cli_reports_copy_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            (destination / PROPERTY_INSPECTOR_CLIENT_FILENAME).write_text(
                "stale",
                encoding="utf-8",
            )
            stderr = StringIO()

            with redirect_stderr(stderr):
                result = main(["copy-property-inspector", str(destination)])

            self.assertEqual(result, 1)
            self.assertIn("Refusing to replace", stderr.getvalue())

    def test_cli_copies_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = main(["copy-property-inspector", temporary_directory])

            self.assertEqual(result, 0)
            target = Path(stdout.getvalue().strip())
            self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()

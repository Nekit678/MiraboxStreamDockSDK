"""Tests for release version validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.verify_version import verify_version


class VersionVerificationTests(unittest.TestCase):
    def test_current_project_version_is_consistent(self) -> None:
        version = verify_version(tag="v0.1.0")

        self.assertEqual(version, "0.1.0")

    def test_rejects_mismatched_release_tag(self) -> None:
        with self.assertRaisesRegex(ValueError, "must match"):
            verify_version(tag="v9.9.9")

    def test_rejects_mismatched_package_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            package_directory = project_root / "src" / "mirabox_sdk"
            package_directory.mkdir(parents=True)
            (project_root / "pyproject.toml").write_text(
                '[project]\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            (package_directory / "__init__.py").write_text(
                '__version__ = "2.0.0"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Version mismatch"):
                verify_version(project_root)


if __name__ == "__main__":
    unittest.main()

"""Tests for release version validation."""

from __future__ import annotations

import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.verify_version import verify_version

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHON_VERSIONS = {"3.11", "3.12", "3.13", "3.14"}


class VersionVerificationTests(unittest.TestCase):
    def test_current_project_version_is_consistent(self) -> None:
        version = verify_version(tag="v0.4.0")

        self.assertEqual(version, "0.4.0")

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

    def test_supported_python_metadata_matches_the_ci_matrix(self) -> None:
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        classifiers = project["project"]["classifiers"]
        classified_versions = {
            match.group(1)
            for classifier in classifiers
            if (match := re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", classifier))
        }
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        matrix = re.search(r"python-version:\n((?:\s+- \"3\.\d+\"\n)+)", workflow)
        self.assertIsNotNone(matrix)
        assert matrix is not None
        ci_versions = set(re.findall(r'"(3\.\d+)"', matrix.group(1)))

        self.assertEqual(project["project"]["requires-python"], ">=3.11")
        self.assertEqual(classified_versions, SUPPORTED_PYTHON_VERSIONS)
        self.assertEqual(ci_versions, SUPPORTED_PYTHON_VERSIONS)
        self.assertEqual(project["tool"]["ruff"]["target-version"], "py311")


if __name__ == "__main__":
    unittest.main()

"""Verify that project and package versions match an optional release tag."""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_project_version(project_root: Path = PROJECT_ROOT) -> str:
    with (project_root / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    if not isinstance(version, str):
        raise TypeError("project.version must be a string")
    return version


def read_package_version(project_root: Path = PROJECT_ROOT) -> str:
    package_file = project_root / "src" / "mirabox_sdk" / "__init__.py"
    package_tree = ast.parse(package_file.read_text(encoding="utf-8"), filename=str(package_file))
    for node in package_tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise ValueError(f"__version__ is missing from {package_file}")


def verify_version(project_root: Path = PROJECT_ROOT, *, tag: str | None = None) -> str:
    project_version = read_project_version(project_root)
    package_version = read_package_version(project_root)
    if project_version != package_version:
        raise ValueError(
            "Version mismatch: "
            f"pyproject.toml={project_version}, mirabox_sdk.__version__={package_version}"
        )
    if tag is not None and tag != f"v{project_version}":
        raise ValueError(f"Release tag {tag!r} must match project version v{project_version}")
    return project_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    args = parser.parse_args(argv)
    try:
        version = verify_version(tag=args.tag)
    except (KeyError, OSError, SyntaxError, TypeError, ValueError) as exc:
        print(f"Version verification failed: {exc}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

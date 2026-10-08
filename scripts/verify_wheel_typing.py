"""Check consumer contracts against a wheel installed outside the source checkout."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tomllib
import venv
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSUMER_CONFIG = """\
[mypy]
python_version = 3.11
follow_imports = normal
warn_unused_ignores = True

[mypy-public_api,public_api_errors,no_any]
disallow_any_unimported = True
disallow_subclassing_any = True
disallow_untyped_defs = True

[mypy-no_any]
disallow_any_expr = True
"""


def verify_wheel_typing(directory: Path) -> None:
    """Install the current SDK wheel into a clean environment and check its typed API."""
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"].replace("-", "_")
    wheels = sorted(directory.resolve().glob(f"mirabox_stream_dock_sdk-{version}-*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"Expected one SDK {version} wheel in {directory}, found {len(wheels)}")
    mypy_requirement = next(
        requirement
        for requirement in project["project"]["optional-dependencies"]["dev"]
        if requirement.startswith("mypy==")
    )
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "MYPYPATH", "PYTHONHOME"):
        environment.pop(name, None)

    with TemporaryDirectory(prefix="mirabox-wheel-typing-") as temporary_directory:
        workspace = Path(temporary_directory)
        virtualenv = workspace / "venv"
        venv.EnvBuilder(with_pip=True).create(virtualenv)
        python = virtualenv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

        def run(*arguments: str) -> None:
            subprocess.run([str(python), *arguments], cwd=workspace, env=environment, check=True)

        run("-m", "pip", "install", "--disable-pip-version-check", str(wheels[0]), mypy_requirement)
        run(
            "-c",
            "import sys; from pathlib import Path; import mirabox_sdk; "
            "assert Path(mirabox_sdk.__file__).is_relative_to(Path(sys.prefix)), "
            "mirabox_sdk.__file__; print('Checking installed SDK:', mirabox_sdk.__file__)",
        )
        for fixture in PROJECT_ROOT.joinpath("tests/typing").glob("*.py"):
            shutil.copy2(fixture, workspace / fixture.name)
        shutil.copy2(PROJECT_ROOT / "tests/test_public_api.py", workspace / "test_public_api.py")
        (workspace / "mypy.ini").write_text(CONSUMER_CONFIG, encoding="utf-8")
        run("-m", "mypy", "--config-file=mypy.ini", "--no-incremental", "-p", "mirabox_sdk")
        run(
            "-m",
            "mypy",
            "--config-file=mypy.ini",
            "--no-incremental",
            "public_api.py",
            "public_api_errors.py",
            "no_any.py",
        )
        run("-m", "mypy.stubtest", "mirabox_sdk.json_types", "--mypy-config-file=mypy.ini")
        run(
            "-m",
            "unittest",
            "test_public_api.PublicAnnotationTests."
            "test_supported_annotations_resolve_and_only_reference_public_sdk_types",
            "-v",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory containing the current SDK wheel")
    arguments = parser.parse_args()
    verify_wheel_typing(arguments.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

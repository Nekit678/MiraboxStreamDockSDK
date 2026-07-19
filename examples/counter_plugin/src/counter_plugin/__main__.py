"""Executable entry point for the counter example plugin."""

from __future__ import annotations

from counter_plugin.bootstrap import build_application
from mirabox_sdk import run_plugin_cli


def main(argv: list[str] | None = None) -> int:
    return run_plugin_cli(
        build_application,
        argv,
        description="Counter example plugin for MiraBox Stream Dock",
    )


if __name__ == "__main__":
    raise SystemExit(main())

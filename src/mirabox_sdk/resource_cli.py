"""Command-line helpers for copying SDK resources into plugin bundles."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .resources import copy_property_inspector_client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage MiraBox Stream Dock SDK resources")
    subparsers = parser.add_subparsers(dest="command", required=True)
    copy_parser = subparsers.add_parser(
        "copy-property-inspector",
        help="copy the shared Property Inspector JavaScript client",
    )
    copy_parser.add_argument("destination", type=Path)
    copy_parser.add_argument(
        "--force",
        action="store_true",
        help="replace a different existing client",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "copy-property-inspector":
        raise AssertionError(f"Unsupported command: {args.command}")

    try:
        target = copy_property_inspector_client(args.destination, overwrite=args.force)
    except OSError as exc:
        print(f"Failed to copy Property Inspector client: {exc}", file=sys.stderr)
        return 1

    print(target)
    return 0

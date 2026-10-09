"""Command-line helpers for SDK resources and plugin bundle validation."""

from __future__ import annotations

import argparse
import sys
from importlib import import_module
from pathlib import Path

from .action_registry import ActionRegistry
from .plugin_validation import validate_plugin
from .resources import copy_property_inspector_client
from .scaffolding import init_plugin


def build_parser() -> argparse.ArgumentParser:
    """Build the ``mirabox-sdk`` project, resource and validation argument parser.

    Returns:
        Parser containing project creation, copying and validation subcommands.
    """

    parser = argparse.ArgumentParser(description="Manage MiraBox Stream Dock plugin bundles")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser(
        "init-plugin", help="create a plugin project with tests and a Windows bundle specification"
    )
    init_parser.add_argument(
        "destination", type=Path, help="new project directory (must not exist)"
    )
    init_parser.add_argument("--uuid", required=True, help="lowercase reverse-domain plugin UUID")
    init_parser.add_argument("--name", required=True, help="plugin and action display name")
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
    validate_parser = subparsers.add_parser(
        "validate-plugin",
        help="validate a manifest and assembled plugin bundle without launching Stream Dock",
    )
    validate_parser.add_argument("path", type=Path, help="plugin bundle directory")
    validate_parser.add_argument(
        "--registry",
        metavar="MODULE:OBJECT",
        help="import an ActionRegistry instance to compare registered action UUIDs; "
        "the module must load all action registrations without starting the plugin",
    )
    return parser


def _validate_plugin(path: Path, registry_reference: str | None) -> int:
    action_uuids: frozenset[str] | None = None
    if registry_reference is not None:
        try:
            module_name, separator, object_name = registry_reference.partition(":")
            if not separator or not module_name or not object_name or ":" in object_name:
                raise ValueError("expected MODULE:OBJECT")
            registry = getattr(import_module(module_name), object_name)
            if not isinstance(registry, ActionRegistry):
                raise TypeError("object must be an ActionRegistry instance")
            action_uuids = registry.action_uuids
        except Exception as exc:
            print(f"Failed to load registry {registry_reference}: {exc}", file=sys.stderr)
            return 1
    issues = validate_plugin(path, action_uuids=action_uuids)
    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        return 1
    print(f"Plugin bundle is valid: {path}")
    if action_uuids is None:
        print("Registry UUID comparison skipped; use --registry MODULE:OBJECT to enable it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the SDK resource and plugin-validation command-line interface.

    Args:
        argv: Arguments without the executable name. ``None`` reads
            :data:`sys.argv`.

    Returns:
        ``0`` after project creation, resource copying or successful validation;
        ``1`` after printing creation, copy, registry import or validation errors.

    Raises:
        SystemExit: If command-line arguments are missing or invalid.
    """

    args = build_parser().parse_args(argv)
    if args.command == "init-plugin":
        try:
            project = init_plugin(args.destination, plugin_uuid=args.uuid, name=args.name)
        except (OSError, ValueError) as exc:
            print(f"Failed to create plugin project: {exc}", file=sys.stderr)
            return 1
        print(f"Created plugin project: {project}")
        print(f"Follow {project / 'README.md'} to test, build, validate and install it.")
        return 0
    if args.command == "validate-plugin":
        return _validate_plugin(args.path, args.registry)
    if args.command != "copy-property-inspector":
        raise AssertionError(f"Unsupported command: {args.command}")

    try:
        target = copy_property_inspector_client(args.destination, overwrite=args.force)
    except OSError as exc:
        print(f"Failed to copy Property Inspector client: {exc}", file=sys.stderr)
        return 1

    print(target)
    return 0

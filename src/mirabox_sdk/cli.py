"""Common command-line parsing for Stream Dock plugin executables."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable

from .errors import InvalidPluginLaunchArgumentsError, InvalidRegistrationInfoError
from .protocols import PluginApplication
from .registration import PluginLaunchArguments, parse_plugin_launch_arguments

logger = logging.getLogger(__name__)


def build_plugin_argument_parser(
    *,
    description: str | None = None,
) -> argparse.ArgumentParser:
    """Build a parser for the arguments supplied to every plugin executable."""

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("-port", type=int, required=True, help="Stream Dock WebSocket port")
    parser.add_argument("-pluginUUID", dest="plugin_uuid", required=True)
    parser.add_argument("-registerEvent", dest="register_event", required=True)
    parser.add_argument("-info", required=True, help="Stream Dock information as JSON")
    return parser


def parse_plugin_cli_arguments(
    argv: list[str] | None = None,
    *,
    description: str | None = None,
) -> PluginLaunchArguments:
    """Parse and validate the standard Stream Dock executable arguments."""

    args = build_plugin_argument_parser(description=description).parse_args(argv)
    try:
        info = json.loads(args.info)
    except json.JSONDecodeError as exc:
        raise InvalidPluginLaunchArgumentsError(
            f"invalid JSON: {exc.msg}",
            path=("info",),
        ) from exc
    return parse_plugin_launch_arguments(
        port=args.port,
        plugin_uuid=args.plugin_uuid,
        register_event=args.register_event,
        info=info,
    )


def run_plugin_cli(
    build_application: Callable[[PluginLaunchArguments], PluginApplication],
    argv: list[str] | None = None,
    *,
    description: str | None = None,
    application_logger: logging.Logger | None = None,
) -> int:
    """Parse launch arguments, build the plugin, and manage its full lifecycle."""

    active_logger = application_logger or logger
    try:
        launch_arguments = parse_plugin_cli_arguments(argv, description=description)
    except (InvalidPluginLaunchArgumentsError, InvalidRegistrationInfoError) as exc:
        active_logger.error("Stream Dock supplied invalid launch arguments: %s", exc)
        return 2

    try:
        application = build_application(launch_arguments)
    except Exception:
        active_logger.exception("Failed to build Stream Dock plugin")
        return 1

    exit_code = 0
    try:
        application.run()
    except KeyboardInterrupt:
        active_logger.info("Plugin interrupted")
    except Exception:
        active_logger.exception("Plugin stopped because of an unexpected error")
        exit_code = 1
    finally:
        try:
            application.stop()
        except Exception:
            active_logger.exception("Failed to stop Stream Dock plugin")
            exit_code = 1
    return exit_code

"""Executable entry point with managed logging and shutdown outcome checking."""

from __future__ import annotations

import logging

from heartbeat_plugin.bootstrap import build_application
from mirabox_sdk import configure_logging, run_plugin_cli


class _LifecycleMetadataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # CLI lifecycle exceptions can chain the worker's original, sensitive error.
        if record.exc_info is not None:
            record.msg = (
                f"{record.getMessage()}; exception_type={type(record.exc_info[1]).__name__}"
            )
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    logger = logging.getLogger("mirabox_sdk.examples.heartbeat.lifecycle")
    metadata_filter = _LifecycleMetadataFilter()
    logger.addFilter(metadata_filter)
    try:
        return run_plugin_cli(build_application, argv, application_logger=logger)
    finally:
        logger.removeFilter(metadata_filter)


if __name__ == "__main__":
    raise SystemExit(main())

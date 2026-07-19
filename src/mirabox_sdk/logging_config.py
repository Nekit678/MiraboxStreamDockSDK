"""Opt-in logging configuration for MiraBox SDK diagnostics."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

_SDK_LOGGER_NAME = "mirabox_sdk"
_MANAGED_HANDLER_NAME = "mirabox_sdk.configure_logging"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3
_include_protocol_payload = False


def _normalize_level(level: int | str) -> int:
    if isinstance(level, bool) or not isinstance(level, (int, str)):
        raise TypeError("level must be an integer or logging level name")
    if isinstance(level, int):
        return level

    normalized = logging.getLevelNamesMapping().get(level.upper())
    if normalized is None:
        raise ValueError(f"Unknown logging level: {level!r}")
    return normalized


def _replace_managed_handler(
    logger: logging.Logger,
    handler: logging.Handler,
) -> None:
    for current_handler in tuple(logger.handlers):
        if current_handler.get_name() == _MANAGED_HANDLER_NAME:
            logger.removeHandler(current_handler)
            current_handler.close()
    handler.set_name(_MANAGED_HANDLER_NAME)
    logger.addHandler(handler)


def _silence_sdk_logging(logger: logging.Logger) -> None:
    _set_protocol_payload_logging(False)
    _replace_managed_handler(logger, logging.NullHandler())
    logger.disabled = False
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)


def _set_protocol_payload_logging(enabled: bool) -> None:
    global _include_protocol_payload
    _include_protocol_payload = enabled


def _protocol_payload_logging_enabled() -> bool:
    return _include_protocol_payload


def _validate_rotation(max_bytes: int, backup_count: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    if isinstance(backup_count, bool) or not isinstance(backup_count, int):
        raise TypeError("backup_count must be an integer")
    if backup_count < 0:
        raise ValueError("backup_count must not be negative")
    if max_bytes > 0 and backup_count == 0:
        raise ValueError("backup_count must be positive when rotation is enabled")


def configure_logging(
    *,
    level: int | str = logging.INFO,
    log_file: str | Path | None = None,
    stream: TextIO | None = None,
    enabled: bool = True,
    include_payload: bool = False,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """Configure isolated SDK logging without changing the root logger.

    Repeated calls replace the handler previously installed by this function.
    Set ``enabled=False`` to silence that output. Protocol payloads remain
    redacted unless ``include_payload=True`` is explicitly requested; full
    payloads are emitted only by DEBUG records. Handlers installed directly by
    the application remain under application control.
    """

    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")
    if not isinstance(include_payload, bool):
        raise TypeError("include_payload must be a boolean")
    if log_file is not None and stream is not None:
        raise ValueError("log_file and stream are mutually exclusive")

    logger = logging.getLogger(_SDK_LOGGER_NAME)
    if not enabled:
        _silence_sdk_logging(logger)
        return logger

    normalized_level = _normalize_level(level)
    _validate_rotation(max_bytes, backup_count)
    if log_file is None:
        handler: logging.Handler = logging.StreamHandler(stream)
    else:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )

    handler.setLevel(normalized_level)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATE_FORMAT))
    _replace_managed_handler(logger, handler)
    _set_protocol_payload_logging(include_payload)
    logger.disabled = False
    logger.propagate = False
    logger.setLevel(normalized_level)
    return logger


_silence_sdk_logging(logging.getLogger(_SDK_LOGGER_NAME))

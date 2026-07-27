"""Opt-in logging configuration for MiraBox SDK diagnostics."""

from __future__ import annotations

import logging
from collections import deque
from enum import StrEnum
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import Empty
from threading import Condition, Lock
from typing import TextIO

_SDK_LOGGER_NAME = "mirabox_sdk"
_MANAGED_HANDLER_NAME = "mirabox_sdk.configure_logging"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3
_DEFAULT_LOGGING_QUEUE_LIMIT = 1024
_include_protocol_payload = False
_dropped_log_records_count = 0
_dropped_log_records_lock = Lock()


class LoggingOverflowPolicy(StrEnum):
    """Policy applied to records when the managed logging queue is full."""

    DROP_NEWEST = "drop_newest"
    DROP_OLDEST = "drop_oldest"


class _BoundedPriorityLogQueue:
    """Bound memory while delivering ERROR records ahead of lower levels."""

    def __init__(
        self,
        *,
        limit: int,
        overflow_policy: LoggingOverflowPolicy,
    ) -> None:
        self._limit = limit
        self._overflow_policy = overflow_policy
        self._condition = Condition()
        self._priority_records: deque[logging.LogRecord] = deque()
        self._regular_records: deque[logging.LogRecord] = deque()
        self._sentinel_pending = False

    def put_nowait(self, record: logging.LogRecord | None) -> None:
        """Accept a record without blocking or request a drain-and-stop."""

        if record is None:
            with self._condition:
                self._sentinel_pending = True
                self._condition.notify()
            return

        dropped = False
        with self._condition:
            current_depth = len(self._priority_records) + len(self._regular_records)
            if current_depth >= self._limit:
                if record.levelno >= logging.ERROR and self._regular_records:
                    self._regular_records.popleft()
                    dropped = True
                elif (
                    self._overflow_policy is LoggingOverflowPolicy.DROP_OLDEST
                    and self._records_for(record)
                ):
                    self._records_for(record).popleft()
                    dropped = True
                else:
                    dropped = True
                    record = None

            if record is not None:
                self._records_for(record).append(record)
                self._condition.notify()

        if dropped:
            _increment_dropped_log_records()

    def get(self, block: bool = True) -> logging.LogRecord | None:
        """Return the next record, prioritizing ERROR and CRITICAL."""

        with self._condition:
            while not self._priority_records and not self._regular_records:
                if self._sentinel_pending:
                    self._sentinel_pending = False
                    return None
                if not block:
                    raise Empty
                self._condition.wait()

            if self._priority_records:
                return self._priority_records.popleft()
            return self._regular_records.popleft()

    def _records_for(self, record: logging.LogRecord) -> deque[logging.LogRecord]:
        if record.levelno >= logging.ERROR:
            return self._priority_records
        return self._regular_records


def _increment_dropped_log_records() -> None:
    global _dropped_log_records_count

    with _dropped_log_records_lock:
        _dropped_log_records_count += 1


def dropped_log_records() -> int:
    """Return the process-wide number of records discarded on queue overflow."""

    with _dropped_log_records_lock:
        return _dropped_log_records_count


class _ManagedQueueHandler(QueueHandler):
    """Queue SDK records and own the single destination listener."""

    def __init__(
        self,
        destination: logging.Handler,
        *,
        queue_limit: int,
        overflow_policy: LoggingOverflowPolicy,
    ) -> None:
        self._destination = destination
        self._state_lock = Lock()
        self._closed = False
        queue = _BoundedPriorityLogQueue(
            limit=queue_limit,
            overflow_policy=overflow_policy,
        )
        super().__init__(queue)
        self._listener = QueueListener(
            queue,
            destination,
            respect_handler_level=True,
        )
        self._listener.start()

    def emit(self, record: logging.LogRecord) -> None:
        with self._state_lock:
            if self._closed:
                return
            super().emit(record)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._listener.stop()
        self._destination.close()
        super().close()


def _normalize_level(level: int | str) -> int:
    if isinstance(level, bool) or not isinstance(level, (int, str)):
        raise TypeError("level must be an integer or logging level name")
    if isinstance(level, int):
        return level

    normalized = logging.getLevelNamesMapping().get(level.upper())
    if normalized is None:
        raise ValueError(f"Unknown logging level: {level!r}")
    return normalized


def _normalize_logging_overflow_policy(
    overflow_policy: LoggingOverflowPolicy | str,
) -> LoggingOverflowPolicy:
    try:
        return LoggingOverflowPolicy(overflow_policy)
    except (TypeError, ValueError):
        choices = ", ".join(policy.value for policy in LoggingOverflowPolicy)
        raise ValueError(f"logging_overflow_policy must be one of: {choices}") from None


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
    logging_queue_limit: int = _DEFAULT_LOGGING_QUEUE_LIMIT,
    logging_overflow_policy: LoggingOverflowPolicy = LoggingOverflowPolicy.DROP_NEWEST,
) -> logging.Logger:
    """Configure isolated SDK logging without changing the root logger.

    Records are queued without destination I/O in the calling thread; one
    managed listener writes them to the selected stream or file. Repeated calls
    drain and replace the handler previously installed by this function. Set
    ``enabled=False`` to drain pending records and silence subsequent output.
    Protocol payloads remain redacted unless ``include_payload=True`` is
    explicitly requested; full payloads are emitted only by DEBUG records.
    The managed queue has a fixed capacity. On overflow, ERROR and CRITICAL
    records take priority over lower-level records, and lower-level records
    never displace them. Within the same priority class,
    ``logging_overflow_policy`` decides which record is discarded. Use
    :func:`dropped_log_records` to observe process-wide losses.
    Handlers installed directly by the application remain under application
    control and may still perform I/O in their caller's thread.

    Args:
        level: Numeric logging level or case-insensitive standard level name.
        log_file: Optional UTF-8 log path. Parent directories are created. When
            omitted, a stream handler is used.
        stream: Optional text stream for the stream handler. ``None`` means the
            logging module's default stream, normally ``sys.stderr``.
        enabled: Set to ``False`` to restore the SDK's silent default.
        include_payload: Include complete protocol payloads in DEBUG records.
            Leave disabled when payloads may contain secrets or personal data.
        max_bytes: File size at which rotation occurs. ``0`` disables rotation.
        backup_count: Number of rotated log files retained when rotation is on.
        logging_queue_limit: Maximum records waiting for the managed listener.
        logging_overflow_policy: Record to discard within the same priority
            class when the queue is full: ``drop_newest`` or ``drop_oldest``.

    Returns:
        The configured ``mirabox_sdk`` package logger.

    Raises:
        TypeError: If a boolean, level, or rotation argument has the wrong type.
        ValueError: If the level name is unknown, file and stream destinations
            are both supplied, a queue option is invalid, or rotation limits
            are inconsistent.
        OSError: If a log directory or file handler cannot be created.

    Note:
        This function sets ``propagate=False`` and never changes the root logger.
        It replaces only the handler installed by previous calls; handlers
        attached directly by the application remain untouched.
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
    if type(logging_queue_limit) is not int or logging_queue_limit <= 0:
        raise ValueError("logging_queue_limit must be a positive integer")
    normalized_overflow_policy = _normalize_logging_overflow_policy(logging_overflow_policy)
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
    queue_handler = _ManagedQueueHandler(
        handler,
        queue_limit=logging_queue_limit,
        overflow_policy=normalized_overflow_policy,
    )
    queue_handler.setLevel(normalized_level)
    _replace_managed_handler(logger, queue_handler)
    _set_protocol_payload_logging(include_payload)
    logger.disabled = False
    logger.propagate = False
    logger.setLevel(normalized_level)
    return logger


_silence_sdk_logging(logging.getLogger(_SDK_LOGGER_NAME))

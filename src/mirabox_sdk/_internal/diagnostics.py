"""Report useful error metadata without formatting untrusted exception text."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from ..diagnostics import SdkDiagnostic, SourceLocation
from ..errors import StreamDockProtocolError, _format_path

# Only protocol schema keys are safe to emit. Settings and message keys belong
# to the plugin and may themselves contain secrets, even when identifier-shaped.
_SCHEMA_KEYS = frozenset(
    "event action context device payload settings coordinates column row controller "
    "isInMultiAction state userDesiredState ticks pressed title titleParameters "
    "titleAlignment fontFamily fontSize fontStyle fontUnderline showTitle titleColor "
    "deviceInfo name type size columns rows application".split()
)


def _safe_path(error: Exception) -> tuple[str | int, ...]:
    if not isinstance(error, StreamDockProtocolError):
        return ()
    result: list[str | int] = []
    plugin_keys = False
    for part in error.path:
        if isinstance(part, int):
            result.append(part)
        elif not plugin_keys and part in _SCHEMA_KEYS:
            result.append(part)
        else:
            result.append("<key>")
        if part == "settings" or part not in _SCHEMA_KEYS:
            plugin_keys = True
    return tuple(result)


def _source_locations(error: BaseException) -> tuple[SourceLocation, ...]:
    locations: list[SourceLocation] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            locations.append(SourceLocation(code.co_filename, traceback.tb_lineno, code.co_name))
            traceback = traceback.tb_next
        current = current.__cause__
    return tuple(locations)


def report_error(
    logger: logging.Logger,
    error: Exception,
    *,
    category: Literal["protocol_error", "callback_error"] = "callback_error",
    event_name: str | None = None,
    callback: str | None = None,
    context: str | None = None,
    error_observer: Callable[[SdkDiagnostic], None] | None = None,
) -> None:
    level = logging.WARNING if category == "protocol_error" else logging.ERROR
    if error_observer is None and not logger.isEnabledFor(level):
        return
    if event_name is None and isinstance(error, StreamDockProtocolError):
        event_name = error.event_name
    diagnostic = SdkDiagnostic(
        category=category,
        event_name=event_name,
        callback=callback,
        context=context,
        field_path=_safe_path(error),
        exception_type=type(error).__name__,
        source_locations=_source_locations(error),
        error=error,
    )
    logger.log(
        level,
        "SDK diagnostic; category=%s event %s context=%s callback=%s exception_type=%s "
        "field_path=%s source=%s",
        diagnostic.category,
        diagnostic.event_name,
        diagnostic.context,
        diagnostic.callback,
        diagnostic.exception_type,
        _format_path(diagnostic.field_path),
        " -> ".join(
            f"{location.filename}:{location.line} in {location.function}"
            for location in diagnostic.source_locations
        ),
        extra={
            "category": diagnostic.category,
            "event_name": diagnostic.event_name,
            "context": diagnostic.context,
            "callback": diagnostic.callback,
            "exception_type": diagnostic.exception_type,
            "field_path": diagnostic.field_path,
            "source_locations": diagnostic.source_locations,
        },
    )
    if error_observer is not None:
        try:
            error_observer(diagnostic)
        except Exception as exc:
            # Do not recurse through the failing observer or expose its message.
            logger.error("SDK error observer failed; exception_type=%s", type(exc).__name__)

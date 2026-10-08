"""Structured error diagnostics available to an application's error observer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """One traceback frame, without source text or local variable values."""

    filename: str
    line: int
    function: str


@dataclass(frozen=True, slots=True)
class SdkDiagnostic:
    """One protocol or callback failure, reported before processing continues.

    ``field_path`` retains protocol schema keys and array indexes; arbitrary
    settings/message keys are replaced with ``<key>``. ``source_locations``
    lists traceback frames in call order, including explicit exception causes.
    ``error`` is the original exception and may contain sensitive data. It is
    available only to the explicitly installed observer and excluded from repr.
    SDK logs contain metadata only, with no exception text, source, or locals.
    """

    category: Literal["protocol_error", "callback_error"]
    event_name: str | None
    callback: str | None
    context: str | None
    field_path: tuple[str | int, ...]
    exception_type: str
    source_locations: tuple[SourceLocation, ...]
    error: Exception = field(repr=False, compare=False)


__all__ = ["SdkDiagnostic", "SourceLocation"]

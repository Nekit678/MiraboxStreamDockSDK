"""Dependencies supplied to counter action instances."""

from __future__ import annotations

from dataclasses import dataclass

from mirabox_sdk import StreamDockSender


@dataclass(frozen=True, slots=True)
class ActionDependencies:
    stream_dock: StreamDockSender

"""Public connection protocols for the typed MiraBox SDK layer."""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol

from .commands import StreamDockCommand
from .events import StreamDockEvent


class StreamDockSender(Protocol):
    """Outbound MiraBox Stream Dock messages available to clients."""

    @abstractmethod
    def send(self, command: StreamDockCommand) -> None: ...


class StreamDockListener(Protocol):
    """Events emitted by a MiraBox Stream Dock connection."""

    @abstractmethod
    def on_stream_dock_connected(self) -> None: ...

    @abstractmethod
    def on_stream_dock_event(self, event: StreamDockEvent) -> None: ...


class StreamDockConnection(StreamDockSender, Protocol):
    """Lifecycle and messaging boundary for a Stream Dock connection."""

    @abstractmethod
    def set_listener(self, listener: StreamDockListener) -> None: ...

    @abstractmethod
    def run_forever(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class StreamDockActionDependencies(Protocol):
    """Minimum dependency container required by :class:`Action`."""

    @property
    @abstractmethod
    def stream_dock(self) -> StreamDockSender: ...


class LifecycleService(Protocol):
    """A plugin-owned service started and stopped with the Stream Dock runtime."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class PluginApplication(Protocol):
    """Executable plugin lifecycle used by the common CLI runner."""

    @abstractmethod
    def run(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

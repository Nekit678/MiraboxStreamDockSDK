"""Deterministic application fakes for synchronous runtime tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from threading import Condition
from time import monotonic

from mirabox_sdk import (
    Action,
    Controller,
    Coordinates,
    DidReceiveGlobalSettingsEvent,
    DidReceiveSettingsEvent,
    JsonObject,
    KeyDownEvent,
    StreamDockEvent,
    StreamDockSender,
    SystemDidWakeUpEvent,
    TitleAlignment,
    TitleParameters,
    TitleParametersDidChangeEvent,
    UnknownStreamDockEvent,
    WillAppearEvent,
    WillDisappearEvent,
)
from mirabox_sdk._next.messaging.ports import InboundEventSourceClosedError
from mirabox_sdk._next.runtime.models import DispatchResult
from mirabox_sdk._next.runtime.ports import RuntimeActionCallbacks

ACTION_UUID = "com.example.runtime.action"


class FakeRuntimeEventDispatcher:
    """Adapt one test callback to the runtime event-dispatcher port."""

    def __init__(
        self,
        dispatch: Callable[[StreamDockEvent], DispatchResult],
    ) -> None:
        self._dispatch = dispatch

    def dispatch(self, event: StreamDockEvent) -> DispatchResult:
        return self._dispatch(event)


class FakeInboundEventSource:
    """Deterministic typed source with explicit receive and acknowledgement history."""

    def __init__(self, events: tuple[object, ...] = ()) -> None:
        self._condition = Condition()
        self._events = deque(events)
        self._in_flight: deque[object] = deque()
        self._accepting = True
        self.received: list[object] = []
        self.acknowledged: list[object] = []

    def submit(self, event: object) -> None:
        with self._condition:
            if not self._accepting:
                raise InboundEventSourceClosedError("event source is closed")
            self._events.append(event)
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def receive(self, *, timeout: float | None = None) -> object:
        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while not self._events:
                if not self._accepting:
                    raise InboundEventSourceClosedError("event source is closed")
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("timed out waiting for fake event")
                self._condition.wait(remaining)
            event = self._events.popleft()
            self._in_flight.append(event)
            self.received.append(event)
            self._condition.notify_all()
            return event

    def task_done(self) -> None:
        with self._condition:
            if not self._in_flight:
                raise ValueError("task_done() called too many times")
            self.acknowledged.append(self._in_flight.popleft())
            self._condition.notify_all()

    def wait_for_acknowledged(self, count: int, *, timeout: float = 1.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.acknowledged) >= count,
                timeout=timeout,
            )


@dataclass(frozen=True, slots=True)
class FakeDependencies:
    stream_dock: StreamDockSender


class RecordingAction(Action[JsonObject, FakeDependencies]):
    """Action that records callback events and state visible during callbacks."""

    def __init__(
        self,
        action: str,
        context: str,
        settings: JsonObject,
        dependencies: FakeDependencies,
    ) -> None:
        super().__init__(action, context, settings, dependencies)
        self.events: list[object | None] = []
        self.settings_seen: list[object] = []
        self.titles_seen: list[tuple[str, TitleParameters | None]] = []

    def on_will_appear(self, event: WillAppearEvent) -> None:
        self.events.append(event)

    def on_will_disappear(self, event: WillDisappearEvent | None = None) -> None:
        self.events.append(event)

    def on_did_receive_settings(self, event: DidReceiveSettingsEvent) -> None:
        self.events.append(event)
        self.settings_seen.append(self.settings)

    def on_title_parameters_did_change(
        self,
        event: TitleParametersDidChangeEvent,
    ) -> None:
        self.events.append(event)
        self.titles_seen.append((self.title, self.title_parameters))

    def on_key_down(self, event: KeyDownEvent) -> None:
        self.events.append(event)

    def on_did_receive_global_settings(self, event: DidReceiveGlobalSettingsEvent) -> None:
        self.events.append(event)

    def on_system_did_wake_up(self, event: SystemDidWakeUpEvent) -> None:
        self.events.append(event)


class RecordingActionFactory:
    """Factory with configurable action type and explicit unknown UUIDs."""

    def __init__(
        self,
        stream_dock: StreamDockSender,
        action_type: type[RecordingAction] = RecordingAction,
    ) -> None:
        self._dependencies = FakeDependencies(stream_dock)
        self._action_type = action_type
        self.calls: list[tuple[str, str, JsonObject]] = []
        self.unknown_uuids: set[str] = set()
        self.instances: list[RecordingAction] = []

    def create(
        self,
        action_uuid: str,
        context: str,
        initial_settings: JsonObject,
    ) -> RuntimeActionCallbacks | None:
        self.calls.append((action_uuid, context, initial_settings))
        if action_uuid in self.unknown_uuids:
            return None
        action = self._action_type(
            action_uuid,
            context,
            self._action_type.decode_settings(initial_settings),
            self._dependencies,
        )
        self.instances.append(action)
        return action


class RecordingPluginHooks:
    def __init__(self) -> None:
        self.events: list[UnknownStreamDockEvent] = []
        self.error: Exception | None = None

    def on_unhandled_event(self, event: UnknownStreamDockEvent) -> None:
        self.events.append(event)
        if self.error is not None:
            raise self.error


def will_appear_event(
    *,
    context: str = "button",
    action: str = ACTION_UUID,
    settings: JsonObject | None = None,
) -> WillAppearEvent:
    return WillAppearEvent(
        action=action,
        context=context,
        device="device",
        settings={"count": 1} if settings is None else settings,
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def will_disappear_event(*, context: str = "button") -> WillDisappearEvent:
    return WillDisappearEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def did_receive_settings_event(
    settings: JsonObject,
    *,
    context: str = "button",
) -> DidReceiveSettingsEvent:
    return DidReceiveSettingsEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings=settings,
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def title_event(*, context: str = "button") -> TitleParametersDidChangeEvent:
    parameters = TitleParameters(
        font_family="Arial",
        font_size=14,
        font_style="Bold",
        font_underline=False,
        show_title=True,
        alignment=TitleAlignment.BOTTOM,
        color="#ffffffff",
    )
    return TitleParametersDidChangeEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        title="Updated",
        title_parameters=parameters,
        controller=Controller.KEYPAD,
    )


def key_down_event(*, context: str = "button") -> KeyDownEvent:
    return KeyDownEvent(
        action=ACTION_UUID,
        context=context,
        device="device",
        settings={"count": 1},
        coordinates=Coordinates(0, 0),
        controller=Controller.KEYPAD,
        is_in_multi_action=False,
    )


def _validate_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("timeout must be a non-negative finite number or None")
    return float(timeout)

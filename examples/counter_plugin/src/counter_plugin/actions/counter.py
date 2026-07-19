"""Persisted counter action used by the SDK example plugin."""

from __future__ import annotations

from mirabox_sdk import Action, JsonObject, KeyDownEvent, SendToPluginEvent, WillAppearEvent

from ..action_registry import register_action
from ..contracts import ActionDependencies

ACTION_UUID = "com.example.counter.increment"


@register_action(ACTION_UUID)
class CounterAction(Action[JsonObject, ActionDependencies]):
    """Increment a persisted counter and show it as the key title."""

    def _count(self) -> int:
        value = self.settings.get("count", 0)
        return value if type(value) is int and value >= 0 else 0

    def _render(self) -> None:
        self.set_title(str(self._count()))

    def _reset(self) -> None:
        self.set_settings({"count": 0})
        self._render()

    def on_will_appear(self, _event: WillAppearEvent) -> None:
        self._render()

    def on_key_down(self, _event: KeyDownEvent) -> None:
        self.set_settings({"count": self._count() + 1})
        self._render()

    def on_send_to_plugin(self, event: SendToPluginEvent) -> None:
        if event.message.name == "reset":
            self._reset()

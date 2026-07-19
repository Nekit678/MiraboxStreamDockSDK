"""Counter action registrations."""

from __future__ import annotations

from mirabox_sdk import ActionRegistry

from .contracts import ActionDependencies

ACTION_REGISTRY: ActionRegistry[ActionDependencies] = ActionRegistry()
register_action = ACTION_REGISTRY.register

"""Reference-only registry adapter used by legacy parity tests."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ...events import EventCodecDescriptor, EventDescriptor, EventScope
from .routes import (
    RUNTIME_EVENT_REGISTRY,
    RuntimeEventRegistryError,
    RuntimeEventScope,
    RuntimeTransition,
)

_LEGACY_RUNTIME_HANDLERS: Final = MappingProxyType(
    {
        RuntimeTransition.NONE: None,
        RuntimeTransition.CREATE_ACTION: "_handle_will_appear_event",
        RuntimeTransition.REMOVE_ACTION: "_handle_will_disappear_event",
        RuntimeTransition.UPDATE_ACTION_SETTINGS: "_handle_did_receive_settings_event",
        RuntimeTransition.UPDATE_ACTION_TITLE: "_handle_title_parameters_did_change_event",
        RuntimeTransition.UPDATE_GLOBAL_SETTINGS: "_handle_did_receive_global_settings_event",
    }
)


def build_legacy_event_registry(
    codec_registry: Mapping[str, EventCodecDescriptor],
) -> Mapping[str, EventDescriptor]:
    """Join separated codec/routes into the legacy public descriptor view."""

    descriptors: dict[str, EventDescriptor] = {}
    for wire_name, codec in codec_registry.items():
        route = RUNTIME_EVENT_REGISTRY.get_by_wire_name(wire_name)
        if route is None:  # pragma: no cover - both registries validate completeness
            raise RuntimeEventRegistryError(f"missing runtime route for {wire_name!r}")
        if route.event_class is not codec.event_class:
            raise RuntimeEventRegistryError(f"codec and runtime route disagree for {wire_name!r}")
        if route.scope is RuntimeEventScope.PLUGIN:
            raise RuntimeEventRegistryError(
                f"legacy descriptor cannot represent plugin route {wire_name!r}"
            )
        descriptors[wire_name] = EventDescriptor(
            wire_name=codec.wire_name,
            event_class=codec.event_class,
            parser=codec.parser,
            scope=EventScope(route.scope.value),
            callback=route.callback,
            runtime_handler=_LEGACY_RUNTIME_HANDLERS[route.transition],
        )
    return MappingProxyType(descriptors)

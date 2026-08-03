"""Temporary adapters between the next runtime and the supported public API."""

from .legacy_actions import LegacyActionFactoryAdapter, LegacyActionRegistry

__all__ = [
    "LegacyActionFactoryAdapter",
    "LegacyActionRegistry",
]

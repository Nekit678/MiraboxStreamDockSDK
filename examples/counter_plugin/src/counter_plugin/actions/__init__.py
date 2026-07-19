"""Import action modules so their registrations run during startup."""

from .counter import CounterAction

__all__ = ["CounterAction"]

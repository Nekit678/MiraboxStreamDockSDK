from __future__ import annotations

import unittest
from sys import getsizeof

from mirabox_sdk import Controller, SetSettingsCommand
from mirabox_sdk._internal.transport.buffer_limits import retained_size, utf8_size


class BufferLimitTests(unittest.TestCase):
    def test_utf8_size_counts_multibyte_text_and_stops_above_limit(self) -> None:
        self.assertEqual(utf8_size("aé😀", 100), 7)
        self.assertGreater(utf8_size("é" * 10_000, 10), 10)
        self.assertGreater(utf8_size("x" * 10_000, 10), 10)
        self.assertEqual(utf8_size("", 1), 0)
        self.assertEqual(utf8_size("\ud800", 10), 3)

    def test_retained_size_counts_shared_objects_once_and_handles_cycles(self) -> None:
        text = "x" * 4096
        values: list[object] = [text, text]
        self.assertEqual(retained_size(values, 100_000), getsizeof(values) + getsizeof(text))
        values.append(values)
        self.assertEqual(retained_size(values, 100_000), getsizeof(values) + getsizeof(text))
        self.assertGreater(retained_size(values, 10), 10)

    def test_retained_size_handles_deep_objects_without_python_recursion(self) -> None:
        value: object = None
        for _ in range(2000):
            value = [value]
        self.assertGreater(retained_size(value, 1_000_000), 100_000)

    def test_retained_size_counts_scalar_payloads_and_honors_exact_budget(self) -> None:
        values = [None, True, 42, 1.5, "text", b"bytes", dict, Controller.KEYPAD]
        expected = getsizeof(values) + sum(getsizeof(value) for value in values)

        self.assertEqual(retained_size(values, expected), expected)
        self.assertGreater(retained_size(values, expected - 1), expected - 1)

    def test_cow_backing_memory_is_counted_without_materializing_views(self) -> None:
        command = SetSettingsCommand("button", {"items": [{"text": "x" * 4096}]})
        wire_message = command.to_validated_wire()
        payload = wire_message._json_object()["payload"]
        before = dict(payload._owner._containers)  # type: ignore[attr-defined]
        size = retained_size(wire_message, 100_000)
        self.assertGreater(size, 4096)
        self.assertEqual(payload._owner._containers, before)  # type: ignore[attr-defined]
        self.assertEqual(retained_size(wire_message, 100_000), size)

    def test_native_owned_payload_storage_is_counted(self) -> None:
        command = SetSettingsCommand("button", {"items": [{"text": "x" * 4096}]})

        self.assertGreater(retained_size(command, 100_000), 4096)
        self.assertEqual(list.copy(command.settings["items"]), [{"text": "x" * 4096}])

    def test_custom_command_state_with_private_slots_is_counted(self) -> None:
        class SlottedState:
            __slots__ = ("__data",)

            def __init__(self) -> None:
                self.__data = "x" * 4096

        self.assertGreater(retained_size(SlottedState(), 100_000), 4096)


if __name__ == "__main__":
    unittest.main()

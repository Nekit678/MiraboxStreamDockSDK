"""Tests for opt-in MiraBox SDK logging configuration."""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from mirabox_sdk import configure_logging


class LoggingConfigurationTests(unittest.TestCase):
    def test_sdk_logging_is_silent_by_default_even_with_root_handler(self) -> None:
        script = """
import io
import logging

output = io.StringIO()
root = logging.getLogger()
root.addHandler(logging.StreamHandler(output))
root.setLevel(logging.DEBUG)

import mirabox_sdk

logging.getLogger("mirabox_sdk.connection").critical("must stay silent")
sdk_logger = logging.getLogger("mirabox_sdk")
assert output.getvalue() == ""
assert sdk_logger.propagate is False
assert any(isinstance(handler, logging.NullHandler) for handler in sdk_logger.handlers)
"""

        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self) -> None:
        self.sdk_logger = logging.getLogger("mirabox_sdk")
        self.original_handlers = tuple(self.sdk_logger.handlers)
        self.original_level = self.sdk_logger.level
        self.original_disabled = self.sdk_logger.disabled
        self.original_propagate = self.sdk_logger.propagate
        for handler in self.original_handlers:
            self.sdk_logger.removeHandler(handler)

    def tearDown(self) -> None:
        for handler in tuple(self.sdk_logger.handlers):
            self.sdk_logger.removeHandler(handler)
            handler.close()
        for handler in self.original_handlers:
            self.sdk_logger.addHandler(handler)
        self.sdk_logger.setLevel(self.original_level)
        self.sdk_logger.disabled = self.original_disabled
        self.sdk_logger.propagate = self.original_propagate

    def test_configures_sdk_stream_without_changing_root_logger(self) -> None:
        root_logger = logging.getLogger()
        original_root_level = root_logger.level
        stream = StringIO()

        configured_logger = configure_logging(level="INFO", stream=stream)
        logging.getLogger("mirabox_sdk.connection").debug("hidden")
        logging.getLogger("mirabox_sdk.connection").info("connected")

        self.assertIs(configured_logger, self.sdk_logger)
        self.assertEqual(root_logger.level, original_root_level)
        self.assertFalse(configured_logger.propagate)
        self.assertNotIn("hidden", stream.getvalue())
        self.assertIn("INFO mirabox_sdk.connection: connected", stream.getvalue())

    def test_repeated_configuration_replaces_managed_handler(self) -> None:
        first_stream = StringIO()
        second_stream = StringIO()
        configure_logging(level="INFO", stream=first_stream)

        configure_logging(level="WARNING", stream=second_stream)
        logging.getLogger("mirabox_sdk.plugin").warning("stopped")

        self.assertEqual(first_stream.getvalue(), "")
        self.assertEqual(second_stream.getvalue().count("stopped"), 1)

    def test_writes_utf8_log_file_and_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_file = Path(temporary_directory) / "nested" / "plugin.log"
            configure_logging(level=logging.ERROR, log_file=log_file)

            logging.getLogger("mirabox_sdk.cli").error("Ошибка запуска")

            self.assertIn("Ошибка запуска", log_file.read_text(encoding="utf-8"))

    def test_rotates_log_files_with_configured_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_file = Path(temporary_directory) / "plugin.log"
            configure_logging(
                level="INFO",
                log_file=log_file,
                max_bytes=200,
                backup_count=2,
            )

            logger = logging.getLogger("mirabox_sdk.connection")
            for index in range(20):
                logger.info("message %d %s", index, "x" * 80)

            log_files = tuple(Path(temporary_directory).glob("plugin.log*"))
            self.assertTrue(log_file.is_file())
            self.assertTrue(log_file.with_suffix(".log.1").is_file())
            self.assertLessEqual(len(log_files), 3)

    def test_can_disable_configured_sdk_output(self) -> None:
        stream = StringIO()
        configure_logging(level="DEBUG", stream=stream)
        logging.getLogger("mirabox_sdk.connection").info("before")

        configure_logging(enabled=False)
        logging.getLogger("mirabox_sdk.connection").critical("after")

        self.assertIn("before", stream.getvalue())
        self.assertNotIn("after", stream.getvalue())

    def test_rejects_invalid_configuration(self) -> None:
        self.sdk_logger.disabled = True
        self.sdk_logger.propagate = True
        self.sdk_logger.setLevel(logging.ERROR)

        with self.assertRaisesRegex(ValueError, "Unknown logging level"):
            configure_logging(level="verbose")

        self.assertTrue(self.sdk_logger.disabled)
        self.assertTrue(self.sdk_logger.propagate)
        self.assertEqual(self.sdk_logger.level, logging.ERROR)
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            configure_logging(log_file="plugin.log", stream=StringIO())
        with self.assertRaisesRegex(TypeError, "enabled must be a boolean"):
            configure_logging(enabled=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "max_bytes must not be negative"):
            configure_logging(max_bytes=-1)
        with self.assertRaisesRegex(ValueError, "backup_count must be positive"):
            configure_logging(max_bytes=1, backup_count=0)


if __name__ == "__main__":
    unittest.main()

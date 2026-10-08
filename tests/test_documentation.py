"""Regression checks for the supported API shown in user documentation."""

from __future__ import annotations

import ast
import importlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (ROOT / "README.md", ROOT / "README.ru.md", ROOT / "docs" / "PROTOCOL.md")
_PYTHON_BLOCK = re.compile(r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)
_REMOVED_PUBLIC_NAMES = (
    "StreamDockPlugin",
    "EVENT_REGISTRY",
    "ActionStore",
    "GlobalSettingsStore",
)


def _python_blocks(document: Path) -> tuple[str, ...]:
    return tuple(_PYTHON_BLOCK.findall(document.read_text(encoding="utf-8")))


class DocumentationTests(unittest.TestCase):
    def test_protocol_application_example_runs_without_connecting(self) -> None:
        protocol_map = ROOT / "docs" / "PROTOCOL.md"
        application_example = _python_blocks(protocol_map)[0]
        namespace: dict[str, object] = {"__name__": __name__}

        exec(compile(application_example, str(protocol_map), "exec"), namespace)

        self.assertIn("build_application", namespace)
        self.assertTrue(callable(namespace["build_application"]))

    def test_python_snippets_compile_and_import_supported_names(self) -> None:
        for document in DOCUMENTS:
            with self.subTest(document=document.name):
                blocks = _python_blocks(document)
                self.assertTrue(blocks, f"{document} has no Python snippets")
            for index, block in enumerate(blocks, start=1):
                with self.subTest(document=document.name, block=index):
                    tree = ast.parse(block, filename=str(document))
                    compile(tree, str(document), "exec")
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.ImportFrom) or node.module not in (
                            "mirabox_sdk",
                            "mirabox_sdk.runtime",
                            "mirabox_sdk.testing",
                        ):
                            continue
                        module = importlib.import_module(node.module)
                        for imported_name in node.names:
                            self.assertIn(imported_name.name, module.__all__)
                            self.assertIsNotNone(getattr(module, imported_name.name))

    def test_readme_testing_example_runs_with_the_public_harness(self) -> None:
        from tests.test_testing import _launch_arguments

        document = ROOT / "README.md"
        example = next(block for block in _python_blocks(document) if "StreamDockHarness" in block)
        namespace: dict[str, object] = {"__name__": __name__}
        exec(compile(example, str(document), "exec"), namespace)
        namespace["test_application"](_launch_arguments())

    def test_readmes_share_the_same_python_api_examples(self) -> None:
        self.assertEqual(
            tuple(
                ast.dump(ast.parse(block), include_attributes=False)
                for block in _python_blocks(ROOT / "README.md")
            ),
            tuple(
                ast.dump(ast.parse(block), include_attributes=False)
                for block in _python_blocks(ROOT / "README.ru.md")
            ),
        )

    def test_readmes_use_the_canonical_global_settings_facade(self) -> None:
        for document in DOCUMENTS[:2]:
            with self.subTest(document=document.name):
                contents = document.read_text(encoding="utf-8")
                self.assertIn("application.global_settings.update(append_items)", contents)
                self.assertIn("application.global_settings.snapshot()", contents)
                self.assertNotIn("runtime.update_global_settings", contents)
                self.assertNotIn("runtime.global_settings", contents)

    def test_protocol_map_does_not_advertise_removed_public_architecture(self) -> None:
        protocol_map = (ROOT / "docs" / "PROTOCOL.md").read_text(encoding="utf-8")
        for name in _REMOVED_PUBLIC_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, protocol_map)


if __name__ == "__main__":
    unittest.main()

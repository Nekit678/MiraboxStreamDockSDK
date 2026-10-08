"""Runtime annotation and transitive import contract for supported consumers."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from typing import TypeVar, get_args, get_type_hints

import mirabox_sdk
import mirabox_sdk.runtime
import mirabox_sdk.testing


class PublicAnnotationTests(unittest.TestCase):
    def test_supported_annotations_resolve_and_only_reference_public_sdk_types(self) -> None:
        modules = (mirabox_sdk, mirabox_sdk.runtime, mirabox_sdk.testing)
        exported = [getattr(module, name) for module in modules for name in module.__all__]
        public_types = {value for value in exported if inspect.isclass(value)}
        visited: set[int] = set()

        def check_annotation(annotation: object) -> None:
            if id(annotation) in visited:
                return
            visited.add(id(annotation))
            if inspect.isclass(annotation) and annotation.__module__.startswith("mirabox_sdk"):
                self.assertIn(annotation, public_types, f"Missing public import for {annotation}")
            if isinstance(annotation, TypeVar) and annotation.__bound__ is not None:
                check_annotation(annotation.__bound__)
            if isinstance(annotation, (list, tuple)):
                for argument in annotation:
                    check_annotation(argument)
            for argument in get_args(annotation):
                check_annotation(argument)

        def check_hints(label: str, target: object) -> None:
            with self.subTest(symbol=label):
                visited.clear()
                for annotation in get_type_hints(target).values():
                    check_annotation(annotation)

        for exported_value in exported:
            if inspect.isfunction(exported_value):
                check_hints(exported_value.__qualname__, exported_value)
            elif inspect.isclass(exported_value):
                check_hints(exported_value.__qualname__, exported_value)
                for name, member in inspect.getmembers(exported_value):
                    if name.startswith("_") and name not in ("__init__", "__enter__", "__exit__"):
                        continue
                    if isinstance(member, property):
                        member = member.fget
                    if inspect.isfunction(member) or inspect.ismethod(member):
                        if member.__module__.startswith("mirabox_sdk"):
                            check_hints(f"{exported_value.__qualname__}.{name}", member)

    def test_public_factory_builds_an_idle_application_with_shared_context(self) -> None:
        from tests.test_testing import _launch_arguments

        contexts: list[mirabox_sdk.ApplicationContext] = []
        registry = mirabox_sdk.ActionRegistry[mirabox_sdk.ApplicationContext]()

        def dependencies(context: mirabox_sdk.ApplicationContext) -> mirabox_sdk.ApplicationContext:
            contexts.append(context)
            return context

        application = mirabox_sdk.create_stream_dock_application(
            _launch_arguments(),
            action_factory=registry,
            action_dependencies_factory=dependencies,
        )
        try:
            self.assertIs(contexts[0].global_settings, application.global_settings)
            self.assertFalse(contexts[0].session_readiness.ready)
        finally:
            application.stop()

    def test_counter_consumer_has_no_internal_imports(self) -> None:
        root = Path(__file__).resolve().parents[1] / "examples" / "counter_plugin"
        for path in (*root.joinpath("src").rglob("*.py"), *root.joinpath("tests").rglob("*.py")):
            with self.subTest(path=path):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn("_internal", (node.module or "").split("."))
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotIn("_internal", alias.name.split("."))

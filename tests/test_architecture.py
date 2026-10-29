"""Guard dependency direction as domain and adapters grow."""

import ast
import sys
from pathlib import Path

import pytest

import careerpilot


def allowed_import(module: str, boundary: str) -> bool:
    if module == "careerpilot" or module.startswith("careerpilot."):
        permitted: tuple[str, ...] = ("careerpilot.domain",)
        if boundary in {"application", "workflows"}:
            permitted += ("careerpilot.application",)
        if boundary == "workflows":
            permitted += ("careerpilot.workflows",)
        if boundary == "tools":
            permitted += ("careerpilot.tools", "careerpilot.application")
        if boundary == "providers":
            permitted += (
                "careerpilot.providers",
                "careerpilot.application",
                "careerpilot.integrations",
            )
        return any(module == prefix or module.startswith(prefix + ".") for prefix in permitted)
    root = module.split(".")[0]
    if boundary == "domain":
        return root in {"datetime", "enum", "typing", "uuid", "urllib", "math", "pydantic"}
    libraries = {"pydantic"}
    if boundary == "workflows":
        libraries |= {"langgraph", "langsmith", "langchain_core"}
    if boundary == "tools":
        libraries |= {"langchain_core", "langsmith"}
    if boundary == "providers":
        libraries |= {
            "langchain_core",
            "langchain_openai",
            "langsmith",
            "openai",
            "pydantic_settings",
        }
    return root in (sys.stdlib_module_names - {"sqlite3", "socket", "subprocess"}) | libraries


@pytest.mark.parametrize("boundary", ["domain", "application", "workflows", "providers", "tools"])
def test_inward_dependency_boundaries(boundary: str) -> None:
    package_file = careerpilot.__file__
    assert package_file is not None
    for source in (Path(package_file).parent / boundary).rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(allowed_import(alias.name, boundary) for alias in node.names), source
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, source
                assert node.module is not None and allowed_import(node.module, boundary), source

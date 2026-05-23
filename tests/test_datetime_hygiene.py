"""Repository-wide datetime hygiene checks."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = ("scout", "scripts", "dashboard", "tests")


def _is_datetime_utcnow(node: ast.Attribute) -> bool:
    if node.attr != "utcnow":
        return False
    value = node.value
    if isinstance(value, ast.Name):
        return value.id == "datetime"
    if isinstance(value, ast.Attribute):
        return value.attr == "datetime"
    return False


def test_repo_python_does_not_use_deprecated_datetime_utcnow() -> None:
    offenders: list[str] = []

    for root_name in PYTHON_ROOTS:
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and _is_datetime_utcnow(node):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert offenders == []

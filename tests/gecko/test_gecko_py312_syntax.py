import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(
    p
    for tree in (ROOT / "gecko", ROOT / "tests" / "gecko")
    for p in tree.rglob("*.py")
    if "__pycache__" not in p.parts
)


def test_files_found():
    assert any(p.name == "dexscreener.py" for p in FILES)
    assert any(p.name == "test_gecko_py312_syntax.py" for p in FILES)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_parses_as_python_312(path):
    ast.parse(path.read_text(encoding="utf-8"), str(path), feature_version=(3, 12))

"""Guard: the retired LunarCrush integration (NAR-06) leaves no dangling refs.

Pure AST/filesystem checks — imports nothing from the package under test, so
this runs on Windows despite the aiohttp OPENSSL_Uplink constraint (INF-08).
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules deleted by NAR-06. A ".": prefix match also catches submodules
# (e.g. scout.social.lunarcrush.loop).
_REMOVED_MODULES = (
    "scout.social.models",
    "scout.social.baselines",
    "scout.social.lunarcrush",
)

_REMOVED_PATHS = (
    "scout/social/models.py",
    "scout/social/baselines.py",
    "scout/social/lunarcrush",
)

# tg_social (scout.social.telegram) is the surviving live tier and must stay.
_SURVIVING_PATH = "scout/social/telegram"

_SCAN_DIRS = ("scout", "scripts", "dashboard")


def _matches_removed(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in _REMOVED_MODULES)


def test_removed_paths_absent():
    for rel in _REMOVED_PATHS:
        assert not (_REPO_ROOT / rel).exists(), f"{rel} should have been removed"


def test_surviving_tg_social_tier_intact():
    assert (_REPO_ROOT / _SURVIVING_PATH).is_dir(), "tg_social tier must survive"


def test_no_imports_of_removed_modules():
    offenders: list[str] = []
    for scan_dir in _SCAN_DIRS:
        base = _REPO_ROOT / scan_dir
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if _matches_removed(node.module):
                        offenders.append(f"{py}: from {node.module} import ...")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _matches_removed(alias.name):
                            offenders.append(f"{py}: import {alias.name}")
    assert not offenders, "dangling imports of removed modules:\n" + "\n".join(
        offenders
    )

"""Factual-copy firewall for the What Changed panel.

Imports the shared Python BANNED_PATTERNS (the canonical scanner list) and
asserts every copy string emitted by whatChangedFacts.js is clean. We do NOT
re-declare an inline banned list and we do NOT quote banned literals inline
(see feedback_static_grep_self_referential_pattern.md) — the source of truth
is scripts/check_todays_focus_contract.py.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "dashboard" / "frontend"
FACTS = FRONTEND / "whatChangedFacts.js"
PANEL = FRONTEND / "components" / "WhatChangedPanel.jsx"
STORAGE = FRONTEND / "whatChangedStorage.js"
CONTRACT = REPO_ROOT / "scripts" / "check_todays_focus_contract.py"


def _load_banned_patterns():
    spec = importlib.util.spec_from_file_location("_tf_contract", CONTRACT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BANNED_PATTERNS


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_what_changed_facts_clean_against_shared_banned_list():
    patterns = _load_banned_patterns()
    text = _read(FACTS)
    findings = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            findings.append(match.group(0))
    assert not findings, f"banned advisory language in whatChangedFacts.js: {findings}"


def test_panel_and_storage_clean_against_shared_banned_list():
    patterns = _load_banned_patterns()
    for path in (PANEL, STORAGE):
        text = _read(path)
        findings = []
        for pattern in patterns:
            for match in pattern.finditer(text):
                findings.append(match.group(0))
        assert not findings, f"banned advisory language in {path.name}: {findings}"


def test_facts_reuses_shared_banned_patterns_not_a_subset():
    # The facts module must re-import the shared BANNED_PATTERNS rather than
    # declaring its own subset (single source of truth).
    text = _read(FACTS)
    assert (
        "BANNED_PATTERNS" in text
    ), "whatChangedFacts.js must reference the shared BANNED_PATTERNS"
    assert (
        "todayFocusFacts.js" in text
    ), "whatChangedFacts.js must import the shared list from todayFocusFacts.js"
    assert (
        "BANNED_PATTERN_SHARDS" not in text
    ), "whatChangedFacts.js must not redeclare the banned-pattern shards"

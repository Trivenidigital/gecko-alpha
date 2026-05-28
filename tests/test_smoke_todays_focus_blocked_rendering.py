"""Tests for the live-smoke script that verifies the readable-translation
path against production Today's Focus payloads.

The smoke is idempotent and reports DEFERRED / PASS / PASS_WITH_UNMAPPED.
These tests cover the classification logic and banned-substring scan
against synthetic payloads (no live HTTP)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location(
        "smoke_todays_focus_blocked_rendering",
        ROOT / "scripts" / "smoke_todays_focus_blocked_rendering.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_todays_focus_blocked_rendering"] = module
    spec.loader.exec_module(module)
    return module


def test_blocked_rows_filter_picks_only_block_cause_set(smoke):
    rows = [
        {"symbol": "A", "block_cause": "data_quality"},
        {"symbol": "B", "block_cause": None},
        {"symbol": "C"},  # missing key
        {"symbol": "D", "block_cause": "data_path"},
    ]
    blocked = smoke._blocked_rows(rows)
    assert [r["symbol"] for r in blocked] == ["A", "D"]


def test_scan_value_finds_banned_substrings_case_insensitive(smoke):
    assert smoke._scan_value("Operator should TRADE NOW") == ["trade now"]
    assert smoke._scan_value("Watch breakout above 50") == ["watch breakout"]
    assert smoke._scan_value("Just a factual entry note") == []


def test_known_reason_keys_match_helper_reason_labels(smoke):
    # Sanity check the hard-coded mirror against the helper file. Drift
    # here over-reports PASS_WITH_UNMAPPED; the test catches new keys
    # added to JS but not propagated here.
    facts_js = (ROOT / "dashboard" / "frontend" / "todayFocusFacts.js").read_text(
        encoding="utf-8"
    )
    for key in smoke.KNOWN_REASON_KEYS:
        # Match either key form: bare identifier or quoted property key.
        assert key in facts_js, f"KNOWN_REASON_KEYS contains {key!r} not present in facts.js"


def test_full_run_handles_zero_blocked_rows_payload_gracefully(smoke, monkeypatch):
    # Synthetic _fetch returning 5 unblocked focus rows.
    def fake_fetch(url, window_hours, timeout):
        return {
            "rows": [
                {"symbol": "OCT", "block_cause": None},
                {"symbol": "VIRTUAL", "block_cause": None},
            ]
        }

    monkeypatch.setattr(smoke, "_fetch", fake_fetch)
    monkeypatch.setattr(
        sys, "argv", ["smoke", "--url", "http://test", "--window-hours", "36", "--json"]
    )
    rc = smoke.main()
    assert rc == 0


def test_full_run_classifies_known_mapped_reason(smoke, monkeypatch, capsys):
    def fake_fetch(url, window_hours, timeout):
        return {
            "rows": [
                {
                    "symbol": "TST",
                    "block_cause": "data_quality",
                    "block_reason_primary": "NO_PRICE",
                }
            ]
        }

    monkeypatch.setattr(smoke, "_fetch", fake_fetch)
    monkeypatch.setattr(
        sys, "argv", ["smoke", "--url", "http://test", "--window-hours", "36", "--json"]
    )
    rc = smoke.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert '"status": "pass"' in out
    assert '"blocked_rows": 1' in out


def test_full_run_classifies_unmapped_reason(smoke, monkeypatch, capsys):
    def fake_fetch(url, window_hours, timeout):
        return {
            "rows": [
                {
                    "symbol": "NEW",
                    "block_cause": "data_quality",
                    "block_reason_primary": "BRAND_NEW_MACHINE_VALUE",
                }
            ]
        }

    monkeypatch.setattr(smoke, "_fetch", fake_fetch)
    monkeypatch.setattr(
        sys, "argv", ["smoke", "--url", "http://test", "--window-hours", "36", "--json"]
    )
    rc = smoke.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert '"status": "pass_with_unmapped"' in out
    assert "BRAND_NEW_MACHINE_VALUE" in out


def test_full_run_fails_on_banned_substring_in_block_field(smoke, monkeypatch, capsys):
    def fake_fetch(url, window_hours, timeout):
        return {
            "rows": [
                {
                    "symbol": "BAD",
                    "block_cause": "data_quality",
                    # Synthetic: a backend bug leaks "trade now" into the field.
                    "block_reason_primary": "operator should trade now",
                }
            ]
        }

    monkeypatch.setattr(smoke, "_fetch", fake_fetch)
    monkeypatch.setattr(
        sys, "argv", ["smoke", "--url", "http://test", "--window-hours", "36", "--json"]
    )
    rc = smoke.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert '"status": "fail"' in out

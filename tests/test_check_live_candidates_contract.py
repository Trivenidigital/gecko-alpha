"""Unit tests for scripts/check_live_candidates_contract.py.

Tests exercise the `validate_payload` function against fixture dicts; the
HTTP layer is bypassed so tests run pure-stdlib (no aiohttp, no DB).
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_live_candidates_contract",
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_live_candidates_contract.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["check_live_candidates_contract"] = _MOD
_SPEC.loader.exec_module(_MOD)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(**overrides):
    base = {
        "disclaimer": "read-only labels; not trading advice; triggers no actions",
        "token_id": "bitcoin",
        "symbol": "BTC",
        "name": "Bitcoin",
        "chain": "coingecko",
        "open_trade_ids": [1],
        "recent_trade_ids": [1],
        "surfaces": ["chain_completed"],
        "actionable": 1,
        "would_be_live": 1,
        "opened_at": _now_iso(),
        "entry_price": 100.0,
        "pct_from_entry": 0.5,
        "current_price": 100.5,
        "market_cap": 500_000.0,
        "price_change_24h": 1.2,
        "price_updated_at": _now_iso(),
        "price_is_stale": False,
        "narrative_fit_score": 50,
        "counter_risk_score": 20,
        "counter_flags": [],
        "latest_chain_match": None,
        "entry_quality": "fresh_entry",
        "verdict": "candidate_review",
        "inclusion_reasons": ["actionable=1", "would_be_live=1"],
        "risk_reasons": [],
    }
    base.update(overrides)
    return base


def _envelope(rows=None, **meta_overrides):
    rows = rows if rows is not None else []
    meta = {
        "read_only": True,
        "not_trade_advice": True,
        "experimental": True,
        "generated_at": _now_iso(),
        "window_hours": 36,
        "limit": 20,
        "open_trades_scanned": len(rows),
        "rows_returned": len(rows),
    }
    meta.update(meta_overrides)
    return {"meta": meta, "rows": rows}


def test_golden_path_clean():
    payload = _envelope(rows=[
        _row(token_id="bitcoin", verdict="candidate_review"),
        _row(token_id="eth", verdict="watch",
             actionable=1, would_be_live=0),
        _row(token_id="doge", verdict="blocked", actionable=0,
             risk_reasons=["not_actionable"]),
        _row(token_id="ada", verdict="data_insufficient",
             actionable=None,
             risk_reasons=["actionable_null_pre_cutover"]),
    ])
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_229_regression_rich_dict_counter_flags_accepted():
    payload = _envelope(rows=[
        _row(counter_flags=[
            {"flag": "dead_project", "severity": "high",
             "detail": "Zero commits in the last 4 weeks"},
            {"flag": "weak_community", "severity": "high",
             "detail": "Reddit subscribers (0) below 100"},
        ])
    ])
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_banned_language_critical_top_level_risk_reasons():
    payload = _envelope(rows=[_row(risk_reasons=["buy now"])])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("buy now" in c for c in result.criticals)


def test_banned_language_critical_nested_counter_flags_detail():
    payload = _envelope(rows=[_row(counter_flags=[
        {"flag": "x", "detail": "this is a moon shot, ape in now"}
    ])])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    # at least one of "moon" or "ape in" should match
    assert any(("moon" in c or "ape in" in c) for c in result.criticals)


def test_banned_language_critical_uniform_regardless_of_verdict():
    payload = _envelope(rows=[_row(
        verdict="data_insufficient",
        actionable=None,
        risk_reasons=["actionable_null_pre_cutover", "100x setup"],
    )])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("100x" in c for c in result.criticals)


def test_missing_meta_flag_critical():
    payload = _envelope()
    payload["meta"]["read_only"] = False
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("read_only" in c for c in result.criticals)


def test_not_trade_advice_false_no_escape_hatch():
    payload = _envelope()
    payload["meta"]["not_trade_advice"] = False
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any(
        "not_trade_advice" in c and "no promotion path" in c
        for c in result.criticals
    )


def test_unknown_verdict_critical():
    payload = _envelope(rows=[_row(verdict="candidate")])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("verdict" in c for c in result.criticals)


def test_unknown_entry_quality_critical():
    payload = _envelope(rows=[_row(entry_quality="high_conviction",
                                   verdict="watch")])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("entry_quality" in c for c in result.criticals)


def test_candidate_review_actionable_zero_critical():
    payload = _envelope(rows=[_row(verdict="candidate_review", actionable=0)])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("candidate_review" in c and "invariant" in c
               for c in result.criticals)


def test_candidate_review_actionable_null_critical():
    payload = _envelope(rows=[_row(verdict="candidate_review",
                                   actionable=None)])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("candidate_review" in c and "invariant" in c
               for c in result.criticals)


def test_envelope_missing_rows_critical():
    payload = {"meta": _envelope()["meta"]}
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("missing required keys" in c for c in result.criticals)


def test_envelope_unknown_top_level_key_warning_only():
    payload = _envelope()
    payload["warnings"] = []
    result = _MOD.validate_payload(payload)
    assert result.is_clean
    assert any("warnings" in w for w in result.warnings)


def test_counter_flags_none_item_critical():
    payload = _envelope(rows=[_row(counter_flags=[None])])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("counter_flags" in c and "229" in c for c in result.criticals)


def test_data_insufficient_no_matching_risk_reason_warning():
    payload = _envelope(rows=[_row(
        verdict="data_insufficient",
        actionable=None,
        # NULL actionable would normally append actionable_null_pre_cutover,
        # but suppose a different code path landed here without one
        risk_reasons=["some_other_reason"],
        entry_quality="fresh_entry",  # not too_stale / data_insufficient
    )])
    result = _MOD.validate_payload(payload)
    assert result.is_clean
    assert any("data_insufficient" in w for w in result.warnings)


def test_empty_rows_envelope_clean():
    payload = _envelope(rows=[])
    result = _MOD.validate_payload(payload)
    assert result.is_clean


def test_rows_returned_mismatch_critical():
    payload = _envelope(rows=[_row()])
    payload["meta"]["rows_returned"] = 5
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("rows_returned" in c for c in result.criticals)


def test_malformed_generated_at_critical():
    payload = _envelope()
    payload["meta"]["generated_at"] = "tomorrow"
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("generated_at" in c for c in result.criticals)


def test_kol_rank_field_firewall_critical():
    payload = _envelope(rows=[_row(**{"kol_rank": 1})])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("kol_rank" in c and "ranking" in c for c in result.criticals)


def test_source_score_field_firewall_critical():
    payload = _envelope(rows=[_row(**{"source_score": 0.8})])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("source_score" in c and "ranking" in c
               for c in result.criticals)


def test_counter_flags_severity_extreme_warning():
    payload = _envelope(rows=[_row(counter_flags=[
        {"flag": "x", "severity": "extreme", "detail": "test"}
    ])])
    result = _MOD.validate_payload(payload)
    assert result.is_clean
    assert any("severity" in w and "extreme" in w for w in result.warnings)


def test_disclaimer_alternate_phrasing_accepted():
    payload = _envelope(rows=[_row(
        disclaimer="informational only — not investment advice for any token"
    )])
    result = _MOD.validate_payload(payload)
    assert result.is_clean


def test_disclaimer_too_short_critical():
    payload = _envelope(rows=[_row(disclaimer="ok")])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("disclaimer" in c for c in result.criticals)


def test_normalize_text_catches_zwsp_bypass():
    # Zero-width space inside "buy now" must still be caught by NFKC + whitespace collapse
    sneaky = "b​uy now"  # b + zero-width-space + uy now
    payload = _envelope(rows=[_row(risk_reasons=[sneaky])])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("buy now" in c for c in result.criticals)


def test_normalize_text_collapses_extra_whitespace():
    sneaky = "buy  now"  # double space
    payload = _envelope(rows=[_row(risk_reasons=[sneaky])])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("buy now" in c for c in result.criticals)


def test_recursive_walk_skips_exempt_identifier_fields():
    # If "moon" appears in token_id / symbol / name (exempt fields), it
    # should NOT trigger banned-language. Tests skip-exemption logic.
    payload = _envelope(rows=[_row(
        token_id="moonbeam",
        symbol="MOON",
        name="Moonbeam Network",
        chain="ethereum",
        risk_reasons=[],
    )])
    result = _MOD.validate_payload(payload)
    assert result.is_clean


def test_meta_drift_more_than_60s_critical():
    payload = _envelope()
    payload["meta"]["generated_at"] = "2024-01-01T00:00:00+00:00"
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("drift" in c for c in result.criticals)


def test_price_is_stale_not_bool_critical():
    payload = _envelope(rows=[_row(price_is_stale="yes")])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("price_is_stale" in c for c in result.criticals)


def test_latest_chain_match_dict_accepted():
    payload = _envelope(rows=[_row(latest_chain_match={
        "pipeline": "p1", "pattern_name": "x",
        "token_id": "bitcoin", "completed_at": _now_iso(),
    })])
    result = _MOD.validate_payload(payload)
    assert result.is_clean


def test_latest_chain_match_string_critical():
    payload = _envelope(rows=[_row(latest_chain_match="not a dict")])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("latest_chain_match" in c for c in result.criticals)

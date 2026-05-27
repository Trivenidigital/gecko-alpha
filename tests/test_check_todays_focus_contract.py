"""Unit tests for scripts/check_todays_focus_contract.py."""

from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_todays_focus_contract",
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_todays_focus_contract.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["check_todays_focus_contract"] = _MOD
_SPEC.loader.exec_module(_MOD)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(**overrides):
    base = {
        "row_key": "paper:bitcoin",
        "token_id": "bitcoin",
        "symbol": "BTC",
        "name": "Bitcoin",
        "chain": "coingecko",
        "source_corpus": "paper",
        "trade_inbox_group": "act_now",
        "window_state": "open",
        "verdict": "candidate_review",
        "entry_quality": "fresh_entry",
        "surfaces": ["volume_spike"],
        "opened_at": _now_iso(),
        "opened_age_hours": 2.0,
        "current_price": 103.0,
        "market_cap": 50_000_000.0,
        "price_change_24h": 12.0,
        "price_updated_at": _now_iso(),
        "price_is_stale": False,
        "price_staleness_minutes": 1.0,
        "current_move_pct": 3.0,
        "move_basis": "paper_entry",
        "entry_quality_facts": ["Trade Inbox group: act_now"],
        "current_risk_facts": ["Price cache stale: false"],
        "counter_flag_facts": [],
        "inclusion_reasons": ["open_paper_trade"],
        "risk_reasons": [],
        "block_reason_primary": None,
    }
    base.update(overrides)
    return base


def _payload(rows=None, **meta_overrides):
    rows = list(rows or [])
    meta = {
        "read_only": True,
        "not_trade_advice": True,
        "visibility_only": True,
        "experimental": True,
        "not_for_alerting": True,
        "not_for_execution": True,
        "not_for_sizing": True,
        "not_for_source_ranking": True,
        "generated_at": _now_iso(),
        "source_endpoint": "/api/trade_inbox",
        "source_window_hours": 36,
        "source_limit_per_group": 20,
        "source_rows_considered": len(rows),
        "source_group_counts": {
            "act_now": len(rows),
            "watch": 0,
            "already_ran": 0,
            "blocked": 0,
        },
        "source_truncated": False,
        "tracker_source_truncated": False,
        "max_rows": 5,
        "paper_target": 3,
        "tracker_target": 2,
        "cache_ttl_minutes": 60,
        "curation_policy": "fixed_recipe_3_paper_2_tracker_no_score",
        "rows_returned": len(rows),
        "eligible_rows_considered": len(rows),
        "empty_state": "No eligible Trade Inbox rows are available for Today's Focus. Source window: 36h.",
    }
    meta.update(meta_overrides)
    return {"meta": meta, "rows": rows}


def test_clean_payload_passes():
    result = _MOD.validate_payload(_payload([_row()]))
    assert result.is_clean, result.criticals


def test_empty_payload_passes():
    result = _MOD.validate_payload(_payload())
    assert result.is_clean, result.criticals


def test_banned_copy_is_critical():
    payload = _payload([_row(entry_quality_facts=["consider buying"])])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("banned-language" in c for c in result.criticals)


def test_word_boundaries_avoid_buyer_and_buyback_false_positives():
    payload = _payload(
        [
            _row(
                entry_quality_facts=[
                    "Buyer count field unavailable",
                    "Buyback tax field unavailable",
                ]
            )
        ]
    )
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_source_acceptable_pullback_enum_is_allowlisted_not_copy_scanned():
    payload = _payload([_row(entry_quality="acceptable_pullback")])
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_forbidden_source_fields_are_critical():
    row = _row()
    row["trade_score"] = 100.0
    row["action_label"] = "WATCH_PULLBACK"
    result = _MOD.validate_payload(_payload([row]))
    assert not result.is_clean
    assert any("forbidden" in c and "trade_score" in c for c in result.criticals)
    assert any("forbidden" in c and "action_label" in c for c in result.criticals)


def test_missing_anti_scope_flag_is_critical():
    payload = _payload([_row()])
    del payload["meta"]["not_for_alerting"]
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("not_for_alerting" in c for c in result.criticals)


def test_duplicate_row_key_is_critical():
    row = _row()
    result = _MOD.validate_payload(_payload([row, copy.deepcopy(row)]))
    assert not result.is_clean
    assert any("duplicate row_key" in c for c in result.criticals)

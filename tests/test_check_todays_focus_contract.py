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
        "trade_inbox_group": "review",
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
        "entry_quality_facts": ["Trade Inbox bucket: review"],
        "current_risk_facts": ["Price cache stale: false"],
        "counter_flag_facts": [],
        "inclusion_reasons": ["open_paper_trade"],
        "risk_reasons": [],
        "block_reason_primary": None,
        "block_cause": None,
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


def test_action_language_variants_are_critical_in_copy():
    for phrase in (
        "act_now",
        "act now",
        "action_required",
        "acting",
        "now_tradeable",
        "tradeable_now",
    ):
        payload = _payload([_row(entry_quality_facts=[phrase])])
        result = _MOD.validate_payload(payload)
        assert not result.is_clean, phrase
        assert any("banned-language" in c for c in result.criticals), phrase


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


def test_diagnostic_alert_and_source_rank_values_are_critical():
    payload = _payload(
        [
            _row(
                surfaces=["urgent_alert", "source_rank_1"],
                inclusion_reasons=["source_score=1"],
                risk_reasons=["notify_candidate"],
            )
        ]
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("forbidden alert/ranking diagnostic" in c for c in result.criticals)


def test_action_language_variants_are_critical_in_diagnostics():
    payload = _payload([_row(surfaces=["action_required"], risk_reasons=["act_now"])])
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("forbidden alert/ranking diagnostic" in c for c in result.criticals)


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


def test_invalid_block_cause_is_critical():
    payload = _payload([_row(block_cause="urgent")])

    result = _MOD.validate_payload(payload)

    assert not result.is_clean
    assert any("block_cause" in c for c in result.criticals)


# PR-C: sparkline contract firewall tests.

_VALID_PAIRS = [[1_716_000_000 + i * 600, 100.0 + i * 0.1] for i in range(12)]


def test_valid_sparkline_payload_with_meta_flag_passes():
    row = _row(price_path_points=_VALID_PAIRS)
    payload = _payload([row], sparkline_is_visual_price_history_only=True)
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_sparkline_without_meta_flag_is_critical():
    row = _row(price_path_points=_VALID_PAIRS)
    payload = _payload([row])  # No flag in meta
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any(
        "sparkline_is_visual_price_history_only must be present" in c
        for c in result.criticals
    )


def test_sparkline_meta_flag_false_is_critical():
    row = _row(price_path_points=_VALID_PAIRS)
    payload = _payload([row], sparkline_is_visual_price_history_only=False)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("must be exactly" in c and "True" in c for c in result.criticals)


def test_sparkline_meta_flag_truthy_one_is_critical():
    """Strict identity (is True), not truthiness — reject `1`."""
    row = _row(price_path_points=_VALID_PAIRS)
    payload = _payload([row], sparkline_is_visual_price_history_only=1)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_sparkline_meta_flag_string_true_is_critical():
    row = _row(price_path_points=_VALID_PAIRS)
    payload = _payload([row], sparkline_is_visual_price_history_only="true")
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_meta_flag_present_without_any_sparkline_row_is_critical():
    payload = _payload([_row()], sparkline_is_visual_price_history_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("must be absent" in c for c in result.criticals)


def test_sparkline_field_absent_without_meta_flag_passes():
    """The standard cohort: no row has sparkline, no flag in meta. Clean."""
    result = _MOD.validate_payload(_payload([_row()]))
    assert result.is_clean, result.criticals


def test_sparkline_point_with_string_element_is_critical():
    row = _row(price_path_points=[[1_716_000_000, "not_a_number"]])
    payload = _payload([row], sparkline_is_visual_price_history_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("price_path_points" in c for c in result.criticals)


def test_sparkline_three_element_pair_is_critical():
    row = _row(price_path_points=[[1_716_000_000, 100.0, 999.0]])
    payload = _payload([row], sparkline_is_visual_price_history_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_sparkline_negative_price_is_critical():
    row = _row(price_path_points=[[1_716_000_000, -5.0]])
    payload = _payload([row], sparkline_is_visual_price_history_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_sparkline_zero_price_is_critical():
    row = _row(price_path_points=[[1_716_000_000, 0.0]])
    payload = _payload([row], sparkline_is_visual_price_history_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_sparkline_negative_ts_is_critical():
    row = _row(price_path_points=[[-1, 100.0]])
    payload = _payload([row], sparkline_is_visual_price_history_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_sparkline_unavailable_suffixed_string_is_banned():
    """Plan §3 banned: 'Sparkline unavailable:' or '-' suffixed variants
    leak interpretation. Test via empty-state which is copy-scanned.
    Critical message uses casefolded text, so assertion is lowercase."""
    payload = _payload(
        [_row()],
        empty_state="No rows — Sparkline unavailable: data thin in 24h window.",
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("sparkline unavailable" in c.lower() for c in result.criticals)


# PR-D: market_benchmarks contract firewall tests.


def test_valid_market_benchmarks_with_flag_passes():
    payload = _payload(
        [_row()],
        market_benchmarks={"btc_4h_pct": -0.5, "sol_4h_pct": 1.2},
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_market_benchmarks_with_only_btc_passes():
    """One benchmark missing is fine; the present key is sufficient."""
    payload = _payload(
        [_row()],
        market_benchmarks={"btc_4h_pct": 0.0},
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_market_benchmarks_without_flag_is_critical():
    payload = _payload([_row()], market_benchmarks={"btc_4h_pct": -0.5})
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_flag_false_is_critical():
    payload = _payload(
        [_row()],
        market_benchmarks={"btc_4h_pct": -0.5},
        market_benchmarks_is_visual_context_only=False,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_flag_truthy_one_is_critical():
    payload = _payload(
        [_row()],
        market_benchmarks={"btc_4h_pct": -0.5},
        market_benchmarks_is_visual_context_only=1,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_with_string_value_is_critical():
    payload = _payload(
        [_row()],
        market_benchmarks={"btc_4h_pct": "-0.5%"},
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_empty_dict_is_critical():
    """Empty dict + flag = no benchmarks; should not advertise via flag."""
    payload = _payload(
        [_row()],
        market_benchmarks={},
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_unknown_key_is_critical():
    payload = _payload(
        [_row()],
        market_benchmarks={"btc_4h_pct": -0.5, "eth_4h_pct": 1.0},
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_cohort_average_smuggle_is_critical():
    """Reviewer A B3 fold: pin the specific cohort-average key name.

    A future implementer reading the failure message sees exactly which
    interpretive aggregate they tried to introduce.
    """
    payload = _payload(
        [_row()],
        market_benchmarks={
            "btc_4h_pct": -0.5,
            "focus_rows_avg_24h_pct": 1.0,
        },
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any(
        "focus_rows_avg_24h_pct" in c for c in result.criticals
    ), result.criticals


def test_market_benchmarks_more_than_two_keys_is_critical():
    """Reviewer A N3 fold: defend the 2-benchmark pin against silent expansion."""
    payload = _payload(
        [_row()],
        market_benchmarks={
            "btc_4h_pct": -0.5,
            "sol_4h_pct": 1.0,
            "eth_4h_pct": 2.0,
        },
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_flag_without_benchmarks_field_is_critical():
    """Inverse-absence: flag set but benchmarks dict absent must fail."""
    payload = _payload([_row()], market_benchmarks_is_visual_context_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_market_benchmarks_both_absent_passes():
    """Standard cohort: no benchmarks, no flag — clean payload."""
    result = _MOD.validate_payload(_payload([_row()]))
    assert result.is_clean, result.criticals


def test_market_benchmarks_zero_delta_passes():
    """Reviewer B N6 fold: 0.0 and -0.0 are valid finite numbers."""
    payload = _payload(
        [_row()],
        market_benchmarks={"btc_4h_pct": 0.0, "sol_4h_pct": -0.0},
        market_benchmarks_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


# DASH-07 / SIG-09: trailing-7d per-trade PnL contract firewall tests.


def _trailing_block(**overrides):
    block = {
        "closed_trades": 6,
        "per_trade_usd": -32.5,
        "total_pnl_usd": -195.0,
        "display_threshold_usd": -10.0,
        "n_gate": 5,
        "hostile": True,
        "window_days": 7,
    }
    block.update(overrides)
    return block


def test_trailing_pnl_valid_with_flag_passes():
    payload = _payload(
        [_row()],
        trailing_7d_paper_pnl=_trailing_block(),
        trailing_7d_paper_pnl_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_trailing_pnl_both_absent_passes():
    result = _MOD.validate_payload(_payload([_row()]))
    assert result.is_clean, result.criticals


def test_trailing_pnl_without_flag_is_critical():
    payload = _payload([_row()], trailing_7d_paper_pnl=_trailing_block())
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_trailing_pnl_flag_without_block_is_critical():
    payload = _payload([_row()], trailing_7d_paper_pnl_is_visual_context_only=True)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_trailing_pnl_flag_truthy_one_is_critical():
    payload = _payload(
        [_row()],
        trailing_7d_paper_pnl=_trailing_block(),
        trailing_7d_paper_pnl_is_visual_context_only=1,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_trailing_pnl_unknown_subkey_is_critical():
    payload = _payload(
        [_row()],
        trailing_7d_paper_pnl=_trailing_block(recommend_size=1.0),
        trailing_7d_paper_pnl_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_trailing_pnl_string_value_is_critical():
    payload = _payload(
        [_row()],
        trailing_7d_paper_pnl=_trailing_block(per_trade_usd="-32.5"),
        trailing_7d_paper_pnl_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_trailing_pnl_hostile_non_bool_is_critical():
    payload = _payload(
        [_row()],
        trailing_7d_paper_pnl=_trailing_block(hostile=1),
        trailing_7d_paper_pnl_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_trailing_pnl_hostile_below_gate_is_critical():
    """hostile MUST be False below n_gate (display-only cue is gated)."""
    payload = _payload(
        [_row()],
        trailing_7d_paper_pnl=_trailing_block(closed_trades=3, hostile=True),
        trailing_7d_paper_pnl_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_trailing_pnl_zero_closed_trades_is_critical():
    payload = _payload(
        [_row()],
        trailing_7d_paper_pnl=_trailing_block(closed_trades=0, hostile=False),
        trailing_7d_paper_pnl_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


# SIG-08: earliness-vs-trending contract firewall tests.


def _earliness_block(**overrides):
    block = {
        "median_lead_time_min": 21938.0,
        "count_ok": 65,
        "count_no_reference": 99,
        "count_total": 164,
        "no_reference_pct": 60.4,
        "window_days": 30,
    }
    block.update(overrides)
    return block


def test_earliness_valid_with_flag_passes():
    payload = _payload(
        [_row()],
        earliness_vs_trending=_earliness_block(),
        earliness_vs_trending_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_earliness_null_median_when_no_ok_passes():
    payload = _payload(
        [_row()],
        earliness_vs_trending=_earliness_block(
            median_lead_time_min=None,
            count_ok=0,
            count_no_reference=5,
            count_total=5,
            no_reference_pct=100.0,
        ),
        earliness_vs_trending_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert result.is_clean, result.criticals


def test_earliness_without_flag_is_critical():
    payload = _payload([_row()], earliness_vs_trending=_earliness_block())
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_earliness_unknown_subkey_is_critical():
    payload = _payload(
        [_row()],
        earliness_vs_trending=_earliness_block(source_rank=1),
        earliness_vs_trending_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_earliness_null_median_with_positive_count_ok_is_critical():
    payload = _payload(
        [_row()],
        earliness_vs_trending=_earliness_block(median_lead_time_min=None),
        earliness_vs_trending_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_earliness_no_reference_pct_out_of_range_is_critical():
    payload = _payload(
        [_row()],
        earliness_vs_trending=_earliness_block(no_reference_pct=150.0),
        earliness_vs_trending_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean


def test_earliness_zero_count_total_is_critical():
    payload = _payload(
        [_row()],
        earliness_vs_trending=_earliness_block(
            count_total=0,
            count_ok=0,
            count_no_reference=0,
            median_lead_time_min=None,
            no_reference_pct=0.0,
        ),
        earliness_vs_trending_is_visual_context_only=True,
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean

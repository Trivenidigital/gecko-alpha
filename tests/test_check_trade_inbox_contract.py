"""Unit tests for scripts/check_trade_inbox_contract.py."""

from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_trade_inbox_contract",
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "check_trade_inbox_contract.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["check_trade_inbox_contract"] = _MOD
_SPEC.loader.exec_module(_MOD)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paper_row(**overrides):
    base = {
        "token_id": "bitcoin",
        "symbol": "BTC",
        "name": "Bitcoin",
        "chain": "coingecko",
        "source_corpus": "paper",
        "group": "act_now",
        "action_label": "REVIEW_NOW",
        "window_state": "open",
        "trade_score": 100.0,
        "sort_key": ["act_now", 0, "bitcoin"],
        "why_now": ["open_window", "window=open", "fresh_entry"],
        "inclusion_reasons": ["open_paper_trade", "actionable=1", "would_be_live=1"],
        "risk_reasons": [],
        "surfaces": ["volume_spike", "top_gainers_tracker"],
        "counter_risk_score": 42,
        "counter_flags": [],
        "counter_risk_predicted_at": _now_iso(),
        "open_trade_ids": [1],
        "recent_trade_ids": [1],
        "actionable": 1,
        "would_be_live": 1,
        "block_reason_primary": None,
        "opened_at": _now_iso(),
        "opened_age_hours": 2.0,
        "pct_from_entry": 3.0,
        "price_change_24h": 12.0,
        "market_cap": 50_000_000.0,
        "current_price": 103.0,
        "entry_quality": "fresh_entry",
        "verdict": "candidate_review",
        "price_updated_at": _now_iso(),
        "price_is_stale": False,
        "price_staleness_minutes": 1.0,
    }
    base.update(overrides)
    return base


def _tracker_row(**overrides):
    base = {
        "token_id": "toes",
        "symbol": "TOES",
        "name": "Toes",
        "chain": "coingecko",
        "source_corpus": "tracker",
        "group": "watch",
        "action_label": "WATCH_PULLBACK",
        "window_state": "open",
        "trade_score": 42.0,
        "sort_key": ["watch", 0, "toes"],
        "why_now": ["open_window", "window=open", "fresh_entry"],
        "inclusion_reasons": ["tracker_promotion", "top_gainers_tracker"],
        "risk_reasons": ["tracker_only_no_paper_trade"],
        "surfaces": ["top_gainers_tracker"],
        "counter_risk_score": None,
        "counter_flags": [],
        "counter_risk_predicted_at": None,
        "open_trade_ids": [],
        "recent_trade_ids": [],
        "actionable": None,
        "would_be_live": None,
        "block_reason_primary": None,
        "opened_at": _now_iso(),
        "opened_age_hours": 1.0,
        "pct_from_entry": 4.0,
        "price_change_24h": 24.0,
        "market_cap": 75_000_000.0,
        "current_price": 104.0,
        "entry_quality": "fresh_entry",
        "verdict": "watch",
        "price_updated_at": _now_iso(),
        "price_is_stale": False,
        "price_staleness_minutes": 1.0,
    }
    base.update(overrides)
    return base


def _empty_groups():
    return {"act_now": [], "watch": [], "already_ran": [], "blocked": []}


def _envelope(rows_by_group=None, **meta_overrides):
    rows_by_group = rows_by_group or _empty_groups()
    rows_returned = sum(len(v) for v in rows_by_group.values())
    group_counts = {g: len(rows_by_group[g]) for g in _MOD.EXPECTED_GROUPS}
    group_hidden_counts = {g: 0 for g in _MOD.EXPECTED_GROUPS}
    returned_paper = sum(
        1
        for rows in rows_by_group.values()
        for r in rows
        if r["source_corpus"] == "paper"
    )
    returned_tracker = sum(
        1
        for rows in rows_by_group.values()
        for r in rows
        if r["source_corpus"] == "tracker"
    )
    meta = {
        "read_only": True,
        "not_trade_advice": True,
        "experimental": True,
        "generated_at": _now_iso(),
        "window_hours": 36,
        "limit_per_group": 20,
        "rows_returned": rows_returned,
        "source_limit": 500,
        "source_rows_considered": rows_returned,
        "open_trades_scanned": returned_paper,
        "paper_rows_considered": returned_paper,
        "tracker_rows_considered": returned_tracker,
        "tracker_rows_promoted": returned_tracker,
        "tracker_source_truncated": False,
        "source_truncated": False,
        "group_counts": group_counts,
        "group_hidden_counts": group_hidden_counts,
        "block_reason_counts": {},
        "stale_warning_count": 0,
        "hard_stale_count": 0,
        "source": "live_candidates",
    }
    meta.update(meta_overrides)
    return {"meta": meta, "groups": rows_by_group}


def test_clean_mixed_payload_passes():
    groups = _empty_groups()
    groups["act_now"].append(_paper_row())
    groups["watch"].append(_tracker_row())
    result = _MOD.validate_payload(_envelope(groups))
    assert result.is_clean, result.criticals


def test_empty_envelope_passes():
    result = _MOD.validate_payload(_envelope())
    assert result.is_clean, result.criticals


def test_unknown_top_level_key_is_critical():
    payload = _envelope()
    payload["rows"] = []
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("unknown top-level keys" in c for c in result.criticals)


def test_limit_key_rejected_limit_per_group_required():
    payload = _envelope()
    payload["meta"]["limit"] = 20
    del payload["meta"]["limit_per_group"]
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("limit" in c for c in result.criticals)


def test_unknown_row_key_is_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(urgency_tier="trade_now"))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("unknown row keys" in c for c in result.criticals)


def test_counter_flags_accept_rich_dict_and_string_items():
    groups = _empty_groups()
    groups["act_now"].append(
        _paper_row(
            counter_flags=[
                "thin liquidity",
                {
                    "type": "holder_concentration",
                    "severity": "warning",
                    "detail": "top holders clustered",
                },
            ],
            counter_risk_score=77,
        )
    )
    result = _MOD.validate_payload(_envelope(groups))
    assert result.is_clean, result.criticals


def test_counter_flags_reject_garbage_items():
    groups = _empty_groups()
    groups["act_now"].append(_paper_row(counter_flags=[None]))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("counter_flags" in c for c in result.criticals)


def test_tracker_row_rejects_counter_risk_context():
    groups = _empty_groups()
    groups["watch"].append(
        _tracker_row(
            counter_risk_score=55,
            counter_flags=["paper-only context"],
            counter_risk_predicted_at=_now_iso(),
        )
    )
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any(
        "tracker row counter_risk_score must be None" in c for c in result.criticals
    )
    assert any("tracker row counter_flags must be []" in c for c in result.criticals)
    assert any(
        "tracker row counter_risk_predicted_at must be None" in c
        for c in result.criticals
    )


def test_nested_ranking_key_is_critical():
    groups = _empty_groups()
    row = _tracker_row()
    row["why_now"] = [{"source_score": 0.9}]
    groups["watch"].append(row)
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("source_score" in c for c in result.criticals)


def test_urgency_value_is_critical_in_non_exempt_string_list():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(why_now=["operator_priority_high"]))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("operator_priority" in c for c in result.criticals)


def test_banned_language_value_is_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(risk_reasons=["buy now"]))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("buy now" in c for c in result.criticals)


def test_banned_language_separator_variants_are_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(risk_reasons=["buy_now", "strong-buy"]))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("buy" in c and "now" in c for c in result.criticals)
    assert any("strong" in c and "buy" in c for c in result.criticals)


def test_identifier_fields_exempt_from_banned_language():
    groups = _empty_groups()
    groups["watch"].append(
        _tracker_row(token_id="moonbeam", symbol="MOON", name="Moonbeam")
    )
    result = _MOD.validate_payload(_envelope(groups))
    assert result.is_clean, result.criticals


def test_surfaces_are_identifier_values_not_banned_language():
    groups = _empty_groups()
    groups["act_now"].append(_paper_row(surfaces=["moonshot_signal"]))
    result = _MOD.validate_payload(_envelope(groups))
    assert result.is_clean, result.criticals


def test_surfaces_still_reject_forbidden_contract_tokens():
    groups = _empty_groups()
    groups["watch"].append(
        _tracker_row(
            surfaces=[
                "recommended_by_kol",
                "watch_breakout",
                "alert_level",
                "urgency_high",
                "priority_high",
                "alert_high",
                "source_rank_high",
            ]
        )
    )
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("recommended_by_kol" in c for c in result.criticals)
    assert any("watch_breakout" in c for c in result.criticals)
    assert any("alert_level" in c for c in result.criticals)
    assert any("urgency" in c for c in result.criticals)
    assert any("priority" in c for c in result.criticals)
    assert any("alert_high" in c for c in result.criticals)
    assert any("source" in c and "rank" in c for c in result.criticals)


def test_value_firewall_rejects_separator_variants():
    groups = _empty_groups()
    groups["watch"].append(
        _tracker_row(why_now=["trade-now", "watch breakout", "alert level high"])
    )
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("trade" in c and "now" in c for c in result.criticals)
    assert any("watch" in c and "breakout" in c for c in result.criticals)
    assert any("alert" in c and "level" in c for c in result.criticals)


def test_tracker_in_act_now_is_critical():
    groups = _empty_groups()
    groups["act_now"].append(
        _tracker_row(
            group="act_now", action_label="REVIEW_NOW", verdict="candidate_review"
        )
    )
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("tracker" in c and "act_now" in c for c in result.criticals)


def test_tracker_with_open_trade_ids_is_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(open_trade_ids=[1]))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("open_trade_ids" in c and "tracker" in c for c in result.criticals)


def test_tracker_with_actionable_is_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(actionable=1))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("actionable" in c and "tracker" in c for c in result.criticals)


def test_actionable_bool_values_are_critical():
    groups = _empty_groups()
    groups["act_now"].append(_paper_row(actionable=True, would_be_live=False))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("actionable" in c for c in result.criticals)
    assert any("would_be_live" in c for c in result.criticals)


def test_paper_without_open_trade_ids_is_critical():
    groups = _empty_groups()
    groups["act_now"].append(_paper_row(open_trade_ids=[]))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("open_trade_ids" in c and "paper" in c for c in result.criticals)


def test_paper_with_tracker_only_reason_is_critical():
    groups = _empty_groups()
    groups["act_now"].append(_paper_row(risk_reasons=["tracker_only_no_paper_trade"]))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("tracker_only_no_paper_trade" in c for c in result.criticals)


def test_row_group_mismatch_is_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(group="blocked"))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("enclosing group" in c for c in result.criticals)


def test_duplicate_token_id_across_corpora_is_critical():
    groups = _empty_groups()
    groups["act_now"].append(_paper_row(token_id="same"))
    groups["watch"].append(_tracker_row(token_id="same"))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("duplicate token_id" in c for c in result.criticals)


def test_counter_math_mismatch_is_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row())
    payload = _envelope(groups, tracker_rows_promoted=0)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("tracker_rows_promoted" in c for c in result.criticals)


def test_block_reason_counts_must_cover_returned_blocked_rows():
    groups = _empty_groups()
    groups["blocked"].append(
        _tracker_row(
            group="blocked",
            action_label="DATA_MISSING",
            block_reason_primary="NO_PRICE",
            risk_reasons=[
                "tracker_only_no_paper_trade",
                "no_price_snapshot_for_token_id",
            ],
        )
    )
    payload = _envelope(groups, block_reason_counts={})
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("block_reason_counts" in c for c in result.criticals)


def test_block_reason_counts_must_cover_returned_reasons_when_hidden_exists():
    groups = _empty_groups()
    groups["blocked"].append(
        _tracker_row(
            group="blocked",
            action_label="DATA_MISSING",
            block_reason_primary="NO_PRICE",
            risk_reasons=[
                "tracker_only_no_paper_trade",
                "no_price_snapshot_for_token_id",
            ],
        )
    )
    payload = _envelope(
        groups,
        group_counts={"act_now": 0, "watch": 0, "already_ran": 0, "blocked": 2},
        group_hidden_counts={"act_now": 0, "watch": 0, "already_ran": 0, "blocked": 1},
        source_rows_considered=2,
        tracker_rows_considered=2,
        tracker_rows_promoted=2,
        block_reason_counts={"NOT_ACTIONABLE": 1},
    )
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("NO_PRICE" in c for c in result.criticals)


def test_impossible_source_counters_are_critical():
    payload = _envelope(tracker_rows_considered=0, tracker_rows_promoted=1)
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("tracker_rows_considered" in c for c in result.criticals)


def test_group_counts_exact_keys_and_bool_rejected():
    payload = _envelope()
    payload["meta"]["group_counts"]["extra"] = 0
    payload["meta"]["group_counts"]["watch"] = True
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("group_counts" in c for c in result.criticals)


def test_type_matrix_bool_numeric_and_sort_key_object_rejected():
    groups = _empty_groups()
    groups["watch"].append(
        _tracker_row(trade_score=True, sort_key=["watch", {"bad": "object"}])
    )
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("trade_score" in c for c in result.criticals)
    assert any("sort_key" in c for c in result.criticals)


def test_malformed_timestamp_is_critical():
    groups = _empty_groups()
    groups["watch"].append(_tracker_row(opened_at="not-iso"))
    result = _MOD.validate_payload(_envelope(groups))
    assert not result.is_clean
    assert any("opened_at" in c for c in result.criticals)


def test_meta_source_value_pinned():
    payload = _envelope(source="trade_inbox")
    result = _MOD.validate_payload(payload)
    assert not result.is_clean
    assert any("meta.source" in c for c in result.criticals)


def test_cli_default_matches_endpoint_default_limit_per_group():
    assert _MOD._DEFAULT_LIMIT_PER_GROUP == 10


def test_fetch_target_uses_limit_per_group(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"meta":{},"groups":{}}'

    def fake_urlopen(url, timeout):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(_MOD.urllib.request, "urlopen", fake_urlopen)
    _MOD.fetch_and_validate(
        "http://example.test",
        timeout_sec=1,
        limit_per_group=7,
        window_hours=12,
    )
    assert "limit_per_group=7" in captured["url"]
    assert "limit=7" not in captured["url"]

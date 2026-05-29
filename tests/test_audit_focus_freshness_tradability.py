"""Tests for scripts/audit_focus_freshness_tradability.py (read-only diagnostic).

Mirrors tests/test_audit_price_path_coverage.py conventions: importlib module
load, module-level FIXED_NOW, dict rows, build_report(now=FIXED_NOW), and
monkeypatched mod._fetch_focus_rows for main() tests.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "audit_focus_freshness_tradability.py"
)
FIXED_NOW = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)

# Defaults adopted by the script (operator-specified thresholds).
STALE_HOURS = 24.0
MAX_AGE_HOURS = 12.0
MAX_MOVE_PCT = 150.0


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "audit_focus_freshness_tradability", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _row(**over: Any) -> dict:
    """A fully-evaluable focus row that survives every EVALUABLE gate by default.

    Note: chart_url and liquidity_usd are intentionally NOT in the base — those
    fields are absent from the real /api/todays_focus surface, so the two
    NOT-EVALUABLE gates must see them missing.
    """
    base = {
        "price_staleness_minutes": 10.0,
        "price_is_stale": False,
        "opened_age_hours": 2.0,
        "current_move_pct": 20.0,
    }
    base.update(over)
    return base


def _report(rows, **kw):
    params = {
        "stale_hours": STALE_HOURS,
        "max_age_hours": MAX_AGE_HOURS,
        "max_move_pct": MAX_MOVE_PCT,
    }
    params.update(kw)
    return mod.build_report(
        "http://x/api/todays_focus?window_hours=36",
        rows,
        FIXED_NOW,
        **params,
    )


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def test_rate_or_null_zero_denominator():
    assert mod._rate_or_null(0, 0) is None
    assert mod._rate_or_null(5, 0) is None
    assert mod._rate_or_null(1, 2) == 0.5


# --------------------------------------------------------------------------- #
# EVALUABLE gate: stale_price                                                  #
# --------------------------------------------------------------------------- #


def test_stale_price_bool_fallback_excluded_vs_kept():
    rows = [
        _row(price_staleness_minutes=None, price_is_stale=True),  # excluded
        _row(price_staleness_minutes=None, price_is_stale=False),  # kept
    ]
    report = _report(rows)
    gate = report["per_gate"]["stale_price"]
    assert gate["excluded_count"] == 1
    assert gate["evaluable_count"] == 2
    assert gate["excluded_rate"] == 0.5
    assert gate["status"] == "evaluable"


def test_stale_price_minutes_boundary_ge():
    # threshold = stale_hours * 60 = 1440 min; >= excludes.
    rows = [
        _row(price_staleness_minutes=STALE_HOURS * 60),  # == threshold -> excluded
        _row(price_staleness_minutes=STALE_HOURS * 60 - 1),  # one under -> kept
    ]
    report = _report(rows)
    gate = report["per_gate"]["stale_price"]
    assert gate["excluded_count"] == 1
    assert gate["evaluable_count"] == 2


def test_stale_price_field_absent_is_not_evaluable_for_row():
    # Neither minutes nor bool present -> None for that row.
    rows = [{"opened_age_hours": 1.0, "current_move_pct": 5.0}]
    report = _report(rows)
    gate = report["per_gate"]["stale_price"]
    assert gate["evaluable_count"] == 0
    assert gate["excluded_rate"] is None
    assert report["field_findings"]["stale_price"]["rows_missing_field"] == 1


# --------------------------------------------------------------------------- #
# EVALUABLE gate: missing_detected_price (current_move_pct is None proxy)      #
# --------------------------------------------------------------------------- #


def test_missing_detected_price_proxy_excluded_vs_kept():
    rows = [
        _row(current_move_pct=None),  # excluded (no detection price)
        _row(current_move_pct=12.5),  # kept
    ]
    report = _report(rows)
    gate = report["per_gate"]["missing_detected_price"]
    assert gate["excluded_count"] == 1
    assert gate["evaluable_count"] == 2
    assert gate["status"] == "evaluable"


def test_missing_detected_price_field_absent():
    rows = [{"price_staleness_minutes": 1.0, "opened_age_hours": 1.0}]
    report = _report(rows)
    gate = report["per_gate"]["missing_detected_price"]
    # current_move_pct key absent -> not evaluable for this row.
    assert gate["evaluable_count"] == 0
    assert gate["excluded_rate"] is None
    assert report["field_findings"]["missing_detected_price"]["rows_missing_field"] == 1


# --------------------------------------------------------------------------- #
# EVALUABLE gate: too_old_since_detection                                      #
# --------------------------------------------------------------------------- #


def test_too_old_boundary_strict_gt():
    rows = [
        _row(opened_age_hours=MAX_AGE_HOURS + 0.1),  # excluded
        _row(opened_age_hours=MAX_AGE_HOURS),  # == kept (strict >)
    ]
    report = _report(rows)
    gate = report["per_gate"]["too_old_since_detection"]
    assert gate["excluded_count"] == 1
    assert gate["evaluable_count"] == 2


def test_too_old_field_absent():
    rows = [{"price_staleness_minutes": 1.0, "current_move_pct": 5.0}]
    report = _report(rows)
    gate = report["per_gate"]["too_old_since_detection"]
    assert gate["evaluable_count"] == 0
    assert gate["excluded_rate"] is None
    assert (
        report["field_findings"]["too_old_since_detection"]["rows_missing_field"] == 1
    )


# --------------------------------------------------------------------------- #
# EVALUABLE gate: far_moved_from_detection                                     #
# --------------------------------------------------------------------------- #


def test_far_moved_boundary_and_abs():
    rows = [
        _row(current_move_pct=MAX_MOVE_PCT + 0.1),  # excluded
        _row(current_move_pct=-MAX_MOVE_PCT),  # == magnitude -> kept (strict >)
        _row(current_move_pct=-(MAX_MOVE_PCT + 0.1)),  # excluded via abs
    ]
    report = _report(rows)
    gate = report["per_gate"]["far_moved_from_detection"]
    assert gate["excluded_count"] == 2
    assert gate["evaluable_count"] == 3


def test_far_moved_none_is_not_evaluable_not_excluded():
    # current_move_pct None -> far_moved gate is None (not-evaluable),
    # NOT counted as excluded by far_moved (that None means missing_detected fires).
    rows = [_row(current_move_pct=None)]
    report = _report(rows)
    gate = report["per_gate"]["far_moved_from_detection"]
    assert gate["excluded_count"] == 0
    assert gate["evaluable_count"] == 0
    assert gate["excluded_rate"] is None
    assert (
        report["field_findings"]["far_moved_from_detection"]["rows_missing_field"] == 1
    )


# --------------------------------------------------------------------------- #
# NOT-EVALUABLE gates: no_venue_route + liquidity_unavailable                  #
# --------------------------------------------------------------------------- #


def test_no_venue_route_not_evaluable_on_real_surface():
    # Real focus rows carry no chart_url key -> gate not-evaluable on every row.
    rows = [_row(), _row(), _row()]
    report = _report(rows)
    gate = report["per_gate"]["no_venue_route"]
    assert gate["evaluable_count"] == 0
    assert gate["excluded_count"] == 0
    assert gate["excluded_rate"] is None
    assert gate["status"] == "not_evaluable"
    ff = report["field_findings"]["no_venue_route"]
    assert ff["rows_missing_field"] == 3
    assert ff["rows_missing_rate"] == 1.0
    # schema_findings line emitted
    assert any("no_venue_route" in line for line in report["schema_findings"])


def test_no_venue_route_forward_proof_when_field_present():
    # If the field is ever added, predicate works: empty/None -> excluded; URL -> kept.
    rows = [
        _row(chart_url=None),  # excluded
        _row(chart_url=""),  # excluded
        _row(chart_url="https://dexscreener.com/x"),  # kept
    ]
    report = _report(rows)
    gate = report["per_gate"]["no_venue_route"]
    assert gate["evaluable_count"] == 3
    assert gate["excluded_count"] == 2
    assert gate["status"] == "evaluable"


def test_liquidity_unavailable_not_evaluable_on_real_surface():
    rows = [_row(), _row()]
    report = _report(rows)
    gate = report["per_gate"]["liquidity_unavailable"]
    assert gate["evaluable_count"] == 0
    assert gate["excluded_count"] == 0
    assert gate["excluded_rate"] is None
    assert gate["status"] == "not_evaluable"
    ff = report["field_findings"]["liquidity_unavailable"]
    assert ff["rows_missing_field"] == 2
    assert ff["rows_missing_rate"] == 1.0


def test_liquidity_unavailable_forward_proof_when_field_present():
    rows = [
        _row(liquidity_usd=None),  # excluded (datum missing)
        _row(liquidity_usd=12345.0),  # kept
    ]
    report = _report(rows)
    gate = report["per_gate"]["liquidity_unavailable"]
    assert gate["evaluable_count"] == 2
    assert gate["excluded_count"] == 1


# --------------------------------------------------------------------------- #
# top-N removal counting                                                       #
# --------------------------------------------------------------------------- #


def test_topn_removal_counts_only_within_first_n_as_ordered():
    # 7 rows; rows[0] and rows[2] stale (within top-5); rows[5]/rows[6] stale too.
    rows = [
        _row(price_staleness_minutes=STALE_HOURS * 60 + 1),  # 0 stale (in top5)
        _row(),  # 1
        _row(price_staleness_minutes=STALE_HOURS * 60 + 1),  # 2 stale (in top5)
        _row(),  # 3
        _row(),  # 4
        _row(price_staleness_minutes=STALE_HOURS * 60 + 1),  # 5 stale (outside)
        _row(price_staleness_minutes=STALE_HOURS * 60 + 1),  # 6 stale (outside)
    ]
    report = _report(rows, top_n=5)
    gate = report["per_gate"]["stale_price"]
    assert gate["topN_removed"] == 2
    assert gate["topN_removed_rate"] == 0.4
    # full cohort still counts all four stale rows
    assert gate["excluded_count"] == 4


def test_topn_denominator_when_fewer_than_n_rows():
    rows = [
        _row(price_staleness_minutes=STALE_HOURS * 60 + 1),  # stale
        _row(),
        _row(),
    ]
    report = _report(rows, top_n=5)
    gate = report["per_gate"]["stale_price"]
    # denominator is min(5, 3) == 3
    assert gate["topN_removed"] == 1
    assert gate["topN_removed_rate"] == mod._rate_or_null(1, 3)


# --------------------------------------------------------------------------- #
# combined survivors (EVALUABLE gates only)                                    #
# --------------------------------------------------------------------------- #


def test_combined_survivors_over_evaluable_gates_only():
    rows = [
        _row(),  # survives all evaluable gates
        _row(opened_age_hours=MAX_AGE_HOURS + 5),  # dropped by too_old
        _row(
            opened_age_hours=MAX_AGE_HOURS + 5,
            current_move_pct=MAX_MOVE_PCT + 5,
        ),  # dropped by 2 gates -> counted once
    ]
    report = _report(rows)
    combined = report["combined"]
    assert combined["survivors_count"] == 1
    assert combined["dropped_count"] == 2
    assert combined["survivors_rate"] == mod._rate_or_null(1, 3)
    # not-evaluable gates (chart_url/liquidity) must NOT sink the survivor
    assert combined["survivors_count"] >= 1


def test_combined_unknown_rows_counts_missing_evaluable_field():
    # Row missing current_move_pct entirely -> far_moved/missing_detected unknown.
    rows = [
        _row(),  # survivor
        {"price_staleness_minutes": 1.0, "opened_age_hours": 1.0},  # missing move field
    ]
    report = _report(rows)
    combined = report["combined"]
    assert combined["unknown_rows"] == 1
    # the unknown row is NOT a survivor (conservative)
    assert combined["survivors_count"] == 1


# --------------------------------------------------------------------------- #
# empty cohort + rates null                                                    #
# --------------------------------------------------------------------------- #


def test_empty_cohort_rates_null():
    report = _report([])
    assert report["total_rows"] == 0
    for gate in report["per_gate"].values():
        assert gate["excluded_rate"] is None
        assert gate["topN_removed_rate"] is None
    assert report["combined"]["survivors_rate"] is None
    assert report["audited_at"] == FIXED_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# missing OTHER fields surfaced, not crashed                                   #
# --------------------------------------------------------------------------- #


def test_missing_field_surfaced_not_crashed():
    rows = [_row(), {"price_staleness_minutes": 1.0}]  # second row missing most fields
    report = _report(rows)  # must not raise
    assert report["total_rows"] == 2
    assert (
        report["field_findings"]["far_moved_from_detection"]["rows_missing_field"] == 1
    )


# --------------------------------------------------------------------------- #
# FOLD #1: output-key allow-list + forbidden-pattern recursion                 #
# --------------------------------------------------------------------------- #

ALLOWED_TOP_LEVEL = {
    "audited_at",
    "endpoint",
    "params",
    "total_rows",
    "top_n",
    "per_gate",
    "combined",
    "field_findings",
    "schema_findings",
}

FORBIDDEN = (
    "rank",
    "score",
    "order",
    "sort",
    "label",
    "urgency",
    "priority",
    "recommend",
    "alert",
    "why_now",
)


def _all_keys(obj):
    keys = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(k)
            keys.extend(_all_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_all_keys(item))
    return keys


def test_output_key_allow_list():
    rows = [_row(), _row(opened_age_hours=MAX_AGE_HOURS + 5)]
    report = _report(rows)
    assert set(report.keys()) <= ALLOWED_TOP_LEVEL
    for key in _all_keys(report):
        lowered = key.lower()
        for bad in FORBIDDEN:
            assert bad not in lowered, f"forbidden token {bad!r} in key {key!r}"


# --------------------------------------------------------------------------- #
# FOLD: offline banner present                                                 #
# --------------------------------------------------------------------------- #


def test_offline_banner_in_module_docstring():
    src = MODULE_PATH.read_text(encoding="utf-8")
    assert "OFFLINE" in src
    assert "DIAGNOSTIC" in src


# --------------------------------------------------------------------------- #
# main() exit-code paths                                                       #
# --------------------------------------------------------------------------- #


def test_main_manual_bad_window_hours_returns_2(capsys):
    rc = mod.main(["--window-hours", "0"])
    assert rc == 2
    assert "window-hours" in capsys.readouterr().err


def test_main_manual_bad_stale_hours_returns_2(capsys):
    rc = mod.main(["--stale-hours", "-1"])
    assert rc == 2


def test_main_manual_bad_max_age_returns_2(capsys):
    rc = mod.main(["--max-age-hours", "0"])
    assert rc == 2


def test_main_manual_bad_max_move_returns_2(capsys):
    rc = mod.main(["--max-move-pct", "-5"])
    assert rc == 2


def test_main_manual_bad_top_n_returns_2(capsys):
    rc = mod.main(["--top-n", "0"])
    assert rc == 2


# FOLD NIT#3: argparse-native exit 2 (type error, not manual range check)
def test_main_argparse_native_type_error_returns_2():
    with pytest.raises(SystemExit) as exc:
        mod.main(["--stale-hours", "notafloat"])
    assert exc.value.code == 2


def test_main_fetch_failure_returns_2(monkeypatch, capsys):
    import urllib.error

    def _boom(url, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(mod, "_fetch_focus_rows", _boom)
    rc = mod.main([])
    assert rc == 2
    assert "failed to fetch" in capsys.readouterr().err


# FOLD NIT#4: --json error envelope carries stage
def test_main_json_fetch_failure_envelope_stage_fetch(monkeypatch, capsys):
    import urllib.error

    def _boom(url, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(mod, "_fetch_focus_rows", _boom)
    rc = mod.main(["--json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["stage"] == "fetch"


def test_main_json_args_failure_envelope_stage_args(capsys):
    rc = mod.main(["--json", "--window-hours", "0"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["stage"] == "args"


def test_main_success_json(monkeypatch, capsys):
    rows = [_row(), _row(opened_age_hours=MAX_AGE_HOURS + 5)]
    monkeypatch.setattr(mod, "_fetch_focus_rows", lambda url, timeout: rows)
    rc = mod.main(["--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_rows"] == 2
    assert payload["endpoint"].endswith("window_hours=36")
    assert payload["params"]["stale_hours"] == STALE_HOURS
    assert payload["params"]["max_age_hours"] == MAX_AGE_HOURS
    assert payload["params"]["max_move_pct"] == MAX_MOVE_PCT
    assert payload["params"]["window_hours"] == 36
    assert payload["top_n"] == 5


def test_main_human_output(monkeypatch, capsys):
    rows = [_row()]
    monkeypatch.setattr(mod, "_fetch_focus_rows", lambda url, timeout: rows)
    rc = mod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OFFLINE" in out or "offline" in out
    assert "{" in out  # json block present


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", __file__]))

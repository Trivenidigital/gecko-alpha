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


# =========================================================================== #
# FOLD ROUND 2 (post code-review)                                              #
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Fold 1 [CRITICAL] — malformed payload shape -> exit 2 (fetch), but a genuine #
# {"rows": []} stays valid -> exit 0 total_rows 0.                             #
# --------------------------------------------------------------------------- #


def test_extract_rows_missing_rows_key_raises():
    # dict without a rows key (e.g. {"data":[...]} or an error envelope)
    with pytest.raises(ValueError):
        mod._extract_rows({})
    with pytest.raises(ValueError):
        mod._extract_rows({"data": [{"x": 1}]})


def test_extract_rows_rows_null_raises():
    with pytest.raises(ValueError):
        mod._extract_rows({"rows": None})


def test_extract_rows_rows_non_list_raises():
    with pytest.raises(ValueError):
        mod._extract_rows({"rows": "x"})


def test_extract_rows_non_object_payload_raises():
    # a bare list payload is no longer silently accepted as the rows list
    with pytest.raises(ValueError):
        mod._extract_rows([{"a": 1}])


def test_extract_rows_empty_list_is_valid():
    # the ONE valid empty case: key present AND value is an (empty) list
    assert mod._extract_rows({"rows": []}) == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"data": [{"x": 1}]}, {"rows": None}, {"rows": "x"}],
)
def test_main_malformed_payload_returns_2(monkeypatch, capsys, payload):
    def _fetch(url, timeout):
        return mod._extract_rows(payload)  # raises ValueError for malformed shapes

    monkeypatch.setattr(mod, "_fetch_focus_rows", _fetch)
    rc = mod.main(["--json"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"
    assert out["stage"] == "fetch"


def test_main_genuinely_empty_rows_list_returns_0(monkeypatch, capsys):
    def _fetch(url, timeout):
        return mod._extract_rows({"rows": []})

    monkeypatch.setattr(mod, "_fetch_focus_rows", _fetch)
    rc = mod.main(["--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_rows"] == 0


# --------------------------------------------------------------------------- #
# Fold 2 [IMPORTANT] — non-dict row element: surface-and-count, do not crash.  #
# --------------------------------------------------------------------------- #


def test_non_dict_row_surfaced_not_crashed():
    rows = [_row(), None, "garbage", 123]
    report = _report(rows)  # must not raise
    assert report["total_rows"] == 4
    assert report["combined"]["malformed_rows"] == 3
    # malformed rows are never survivors and are counted as unknown
    assert report["combined"]["survivors_count"] == 1
    assert report["combined"]["unknown_rows"] == 3
    # surfaced per-gate in field_findings + in schema_findings
    assert report["field_findings"]["stale_price"]["malformed_rows"] == 3
    assert any("malformed" in line for line in report["schema_findings"])


# --------------------------------------------------------------------------- #
# Fold 3 [IMPORTANT] — not-evaluable gates: topN_removed AND topN_removed_rate #
# are null (not 0 / 0.0).                                                      #
# --------------------------------------------------------------------------- #


def test_not_evaluable_gates_topn_null():
    rows = [_row(), _row(), _row()]  # no chart_url / liquidity_usd on the surface
    report = _report(rows)
    for name in ("no_venue_route", "liquidity_unavailable"):
        gate = report["per_gate"][name]
        assert gate["status"] == "not_evaluable"
        assert gate["topN_removed"] is None
        assert gate["topN_removed_rate"] is None


def test_evaluable_gate_topn_still_numeric():
    # regression guard: an EVALUABLE gate keeps numeric topN values.
    rows = [_row(price_staleness_minutes=STALE_HOURS * 60 + 1), _row()]
    report = _report(rows)
    gate = report["per_gate"]["stale_price"]
    assert gate["status"] == "evaluable"
    assert gate["topN_removed"] == 1
    assert gate["topN_removed_rate"] == 0.5


# --------------------------------------------------------------------------- #
# Fold 4 [IMPORTANT] — stale primary field absent but bool present: surface    #
# the missing primary field (so --stale-hours bypass is visible).             #
# --------------------------------------------------------------------------- #


def test_stale_primary_missing_bool_present_surfaced():
    rows = [
        {"price_is_stale": True, "opened_age_hours": 1.0, "current_move_pct": 5.0},
        {"price_is_stale": False, "opened_age_hours": 1.0, "current_move_pct": 5.0},
    ]
    report = _report(rows)
    gate = report["per_gate"]["stale_price"]
    # fallback still works: one stale (excluded), one not.
    assert gate["excluded_count"] == 1
    assert gate["evaluable_count"] == 2
    # the bypass is surfaced, not silent.
    ff = report["field_findings"]["stale_price"]
    assert ff["primary_field_missing_used_bool_fallback"] == 2
    assert any(
        "price_staleness_minutes" in line and "fallback" in line
        for line in report["schema_findings"]
    )


def test_stale_primary_present_no_fallback_finding():
    rows = [_row()]  # has price_staleness_minutes -> no fallback finding
    report = _report(rows)
    ff = report["field_findings"]["stale_price"]
    assert "primary_field_missing_used_bool_fallback" not in ff


# --------------------------------------------------------------------------- #
# Fold 5 [NIT] — non-numeric numeric-gate value treated as missing, no crash.  #
# --------------------------------------------------------------------------- #


def test_numeric_gates_non_numeric_value_not_crashed():
    rows = [
        {
            "opened_age_hours": "oops",
            "current_move_pct": "nan",
            "price_staleness_minutes": 1.0,
        }
    ]
    report = _report(rows)  # must not raise
    age = report["per_gate"]["too_old_since_detection"]
    move = report["per_gate"]["far_moved_from_detection"]
    # non-numeric -> not evaluable for that gate/row
    assert age["evaluable_count"] == 0
    assert move["evaluable_count"] == 0
    # surfaced distinctly
    assert report["field_findings"]["too_old_since_detection"]["rows_non_numeric"] == 1
    assert report["field_findings"]["far_moved_from_detection"]["rows_non_numeric"] == 1
    assert any("non-numeric" in line for line in report["schema_findings"])


# --------------------------------------------------------------------------- #
# Fold 6 [NIT] — anti-scope source-grep contract tests (lock §6).             #
# --------------------------------------------------------------------------- #

import re  # noqa: E402


def _source_body() -> str:
    """Module source with comment/docstring lines stripped.

    The docstring intentionally PARAPHRASES the banned tokens (per the
    self-referential-grep lesson); the executable body must contain none.
    """
    src = MODULE_PATH.read_text(encoding="utf-8")
    out = []
    in_doc = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # toggle for one-line or multi-line docstrings
            if stripped.count('"""') == 1 and stripped.count("'''") == 0:
                in_doc = not in_doc
            elif stripped.count("'''") == 1 and stripped.count('"""') == 0:
                in_doc = not in_doc
            continue
        if in_doc:
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_anti_scope_no_db_writes():
    body = _source_body()
    assert not re.search(r"\bINSERT\b", body)
    assert not re.search(r"\bUPDATE\b", body)
    assert not re.search(r"\bDELETE\b", body)
    assert ".commit(" not in body
    assert ".post(" not in body  # no requests.post / session.post
    # the only file open is read-only; no write-mode open
    assert not re.search(r"open\([^)]*['\"][wa]", body)


def test_anti_scope_no_dashboard_import():
    body = _source_body()
    assert "import dashboard" not in body
    assert "from dashboard" not in body


def test_anti_scope_no_web_framework_route():
    body = _source_body()
    assert "@app" not in body
    assert "@router" not in body
    assert "FastAPI" not in body
    assert "APIRouter" not in body


# --------------------------------------------------------------------------- #
# Fold 7 [NIT] — generalized topN keys + combined.topN_survivors value + 4dp.  #
# --------------------------------------------------------------------------- #


def test_combined_topn_survivors_value():
    # 6 rows, first 5 = top-N. rows[1] dropped (too old) -> 4 survivors in top-N.
    rows = [
        _row(),  # 0 survives
        _row(opened_age_hours=MAX_AGE_HOURS + 5),  # 1 dropped (too old)
        _row(),  # 2 survives
        _row(),  # 3 survives
        _row(),  # 4 survives
        _row(),  # 5 outside top-N
    ]
    report = _report(rows, top_n=5)
    assert report["combined"]["topN_survivors"] == 4
    assert report["combined"]["survivors_count"] == 5


def test_rate_rounded_to_4dp():
    # 1/3 -> 0.3333 (4dp), not 0.3333333...
    assert mod._rate_or_null(1, 3) == 0.3333
    assert mod._rate_or_null(2, 3) == 0.6667


# =========================================================================== #
# FOLD ROUND 3 (post code-review)                                              #
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Fold A [IMPORTANT] — primary price_staleness_minutes PRESENT but NON-NUMERIC #
# still falls back to the price_is_stale bool; that bypass must be SURFACED in #
# field_findings (not silent), distinctly from the absent/None fallback.       #
# --------------------------------------------------------------------------- #


def test_stale_primary_non_numeric_uses_bool_fallback_and_surfaced():
    rows = [
        {  # primary present-but-non-numeric -> fallback to bool (stale)
            "price_staleness_minutes": "oops",
            "price_is_stale": True,
            "opened_age_hours": 1.0,
            "current_move_pct": 5.0,
        },
        {  # primary present-but-non-numeric -> fallback to bool (not stale)
            "price_staleness_minutes": "oops",
            "price_is_stale": False,
            "opened_age_hours": 1.0,
            "current_move_pct": 5.0,
        },
    ]
    report = _report(rows)
    gate = report["per_gate"]["stale_price"]
    # fallback fired -> the gate is still evaluable (bool drove it).
    assert gate["evaluable_count"] == 2
    assert gate["excluded_count"] == 1
    # the bypass is SURFACED, not silent.
    ff = report["field_findings"]["stale_price"]
    assert ff["primary_field_non_numeric_used_bool_fallback"] == 2
    assert any(
        "price_staleness_minutes" in line
        and "non-numeric" in line
        and "fallback" in line
        for line in report["schema_findings"]
    )


def test_stale_absent_none_fallback_still_works_and_distinct():
    # regression: the existing absent/None fallback finding must keep working
    # and remain DISTINCT from the new non-numeric finding.
    rows = [
        {  # primary absent -> bool fallback
            "price_is_stale": True,
            "opened_age_hours": 1.0,
            "current_move_pct": 5.0,
        },
        {  # primary None -> bool fallback
            "price_staleness_minutes": None,
            "price_is_stale": False,
            "opened_age_hours": 1.0,
            "current_move_pct": 5.0,
        },
    ]
    report = _report(rows)
    ff = report["field_findings"]["stale_price"]
    assert ff["primary_field_missing_used_bool_fallback"] == 2
    # the non-numeric key is absent because no row was non-numeric.
    assert "primary_field_non_numeric_used_bool_fallback" not in ff


def test_stale_numeric_primary_no_fallback_findings():
    # a fully-numeric primary produces NEITHER fallback finding.
    rows = [_row()]
    report = _report(rows)
    ff = report["field_findings"]["stale_price"]
    assert "primary_field_missing_used_bool_fallback" not in ff
    assert "primary_field_non_numeric_used_bool_fallback" not in ff


# --------------------------------------------------------------------------- #
# Fold B [IMPORTANT] — top-N slice evaluability: when the first top_n rows are #
# ALL unevaluable for a gate, topN_removed AND topN_removed_rate are null even #
# if a later row makes the gate globally evaluable.                            #
# --------------------------------------------------------------------------- #


def test_topn_slice_unevaluable_nulls_topn_even_if_globally_evaluable():
    # rows[0:5] miss chart_url; rows[5] has it -> no_venue_route is GLOBALLY
    # evaluable (count>0) BUT its top-N slice (first 5) is all unevaluable.
    rows = [
        _row(),  # 0 no chart_url
        _row(),  # 1 no chart_url
        _row(),  # 2 no chart_url
        _row(),  # 3 no chart_url
        _row(),  # 4 no chart_url
        _row(chart_url="https://dexscreener.com/x"),  # 5 has it (kept)
    ]
    report = _report(rows, top_n=5)
    gate = report["per_gate"]["no_venue_route"]
    # globally evaluable because row 5 backs the field.
    assert gate["status"] == "evaluable"
    assert gate["evaluable_count"] == 1
    # but the top-N slice could not be looked at -> null, not 0/0.0.
    assert gate["topN_removed"] is None
    assert gate["topN_removed_rate"] is None


def test_topn_slice_partially_evaluable_keeps_numeric():
    # regression: if ANY of the top-N slice rows is evaluable, topN stays numeric.
    rows = [
        _row(chart_url=None),  # 0 evaluable -> excluded
        _row(),  # 1 not evaluable (no chart_url key)
        _row(),  # 2 not evaluable
        _row(),  # 3 not evaluable
        _row(),  # 4 not evaluable
        _row(chart_url="https://x"),  # 5 outside top-N, kept
    ]
    report = _report(rows, top_n=5)
    gate = report["per_gate"]["no_venue_route"]
    assert gate["status"] == "evaluable"
    # 1 of the 5 top-N rows is evaluable and excluded.
    assert gate["topN_removed"] == 1
    # denominator stays the slice size (min(top_n,total)=5), per existing
    # convention; rate = 1/5 = 0.2.
    assert gate["topN_removed_rate"] == 0.2


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", __file__]))

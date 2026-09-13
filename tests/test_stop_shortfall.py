"""Historical paper display must fail closed without hiding history."""

import math
import sqlite3

import pytest

from dashboard.stop_shortfall import classify_stop_shortfall
from dashboard.db import get_trading_history, get_trading_history_count

CUTOVER = "2026-07-03T00:32:14.634856+00:00"


def evidence(**changes):
    row = dict(status="closed_sl", exit_provenance="market", price_source="cg_lane",
               entry_snapshot_version="v1", sl_pct_at_entry=25.0,
               entry_price=100.0, exit_price=72.0, quantity=10.0,
               remaining_qty=10.0, amount_usd=1000.0, realized_pnl_usd=0.0,
               conviction_locked_at=None, leg_1_filled_at=None, leg_2_filled_at=None,
               closed_at="2026-07-10T00:00:00+00:00")
    return row | changes


def test_available_arithmetic_uses_prices_and_frozen_stop():
    result = classify_stop_shortfall(evidence(pnl_pct=-99, sl_pct=90), CUTOVER)
    assert result == dict(state="available", exclusion_reason=None,
                          entry_stop_pct=25.0, recorded_exit_return_pct=-28.000000000000004,
                          shortfall_pp=pytest.approx(3), basis="historical_paper_experimental")
    assert classify_stop_shortfall(evidence(exit_price=80), CUTOVER)["shortfall_pp"] == 0
    assert classify_stop_shortfall(evidence(exit_price=0), CUTOVER)["shortfall_pp"] == 75


@pytest.mark.parametrize("changes,reason", [
    ({"status": "closed_tp"}, "not_stop_exit"),
    ({"exit_provenance": "stop_gap_model"}, "modeled_exit"),
    ({"exit_provenance": None}, "exit_not_market"),
    ({"exit_provenance": "unknown"}, "exit_not_market"),
    ({"exit_provenance": "stale_snapshot"}, "exit_not_market"),
    ({"price_source": None}, "price_source_unverified"),
    ({"price_source": ""}, "price_source_unverified"),
    ({"price_source": "legacy"}, "price_source_unverified"),
    ({"entry_snapshot_version": "backfill_v1"}, "entry_snapshot_unverified"),
    ({"entry_snapshot_version": None}, "entry_snapshot_unverified"),
    ({"closed_at": "invalid"}, "timestamp_unverified"),
    ({"closed_at": "2026-07-10T00:00:00"}, "timestamp_unverified"),
    ({"closed_at": "2026-07-01T00:00:00Z"}, "pre_cutover"),
    ({"conviction_locked_at": "anything"}, "conviction_modified"),
    ({"leg_1_filled_at": "anything"}, "partial_exit"),
    ({"leg_2_filled_at": "anything"}, "partial_exit"),
    ({"remaining_qty": 9}, "partial_exit"),
    ({"realized_pnl_usd": -0.001}, "partial_exit"),
    ({"amount_usd": 999}, "inconsistent_notional"),
])
def test_exclusions_never_produce_numbers(changes, reason):
    result = classify_stop_shortfall(evidence(**changes), CUTOVER)
    assert result["exclusion_reason"] == reason
    assert result["state"] == ("modeled" if reason == "modeled_exit" else
                               "not_applicable" if reason == "not_stop_exit" else "unavailable")
    assert all(result[k] is None for k in ("entry_stop_pct", "recorded_exit_return_pct", "shortfall_pp"))


@pytest.mark.parametrize("field", ["entry_price", "exit_price", "sl_pct_at_entry",
                                  "quantity", "remaining_qty", "amount_usd", "realized_pnl_usd"])
@pytest.mark.parametrize("value", [None, True, "25", math.inf, math.nan, -math.inf])
def test_invalid_numeric_evidence(field, value):
    assert classify_stop_shortfall(evidence(**{field: value}), CUTOVER)["exclusion_reason"] == "invalid_numeric_evidence"


@pytest.mark.parametrize("changes", [{"entry_price": 0}, {"exit_price": -1},
                                    {"sl_pct_at_entry": 0}, {"sl_pct_at_entry": 101},
                                    {"quantity": 0}, {"remaining_qty": -1}, {"amount_usd": 0}])
def test_numeric_bounds(changes):
    assert classify_stop_shortfall(evidence(**changes), CUTOVER)["state"] == "unavailable"


def test_cutover_timezone_and_precision():
    assert classify_stop_shortfall(evidence(closed_at=CUTOVER), CUTOVER)["state"] == "available"
    assert classify_stop_shortfall(evidence(closed_at="2026-07-03T01:32:14.634856+01:00"), CUTOVER)["state"] == "available"
    assert classify_stop_shortfall(evidence(closed_at="2026-07-03T00:32:14.634855Z"), CUTOVER)["exclusion_reason"] == "pre_cutover"
    for cutover in (None, "invalid", "2026-07-03T00:00:00"):
        assert classify_stop_shortfall(evidence(), cutover)["exclusion_reason"] == "timestamp_unverified"


def history_db(tmp_path):
    path = tmp_path / "history.sqlite"
    c = sqlite3.connect(path)
    base = evidence()
    columns = dict(id="INTEGER PRIMARY KEY", **{k: "" for k in base},
                   token_id="", symbol="", name="", chain="", signal_type="", signal_data="",
                   pnl_usd="", pnl_pct="", exit_reason="", peak_price="", peak_pct="",
                   checkpoint_1h_pct="", checkpoint_6h_pct="", checkpoint_24h_pct="",
                   checkpoint_48h_pct="", opened_at="", would_be_live="", actionable="",
                   actionability_reason="", actionability_version="")
    # Snapshot fields must come from the snapshot table, never paper_trades.
    for key in ("entry_snapshot_version", "sl_pct_at_entry"):
        columns.pop(key)
    c.execute("CREATE TABLE paper_trades (" + ",".join(f"{k} {v}" for k, v in columns.items()) + ")")
    c.execute("CREATE TABLE paper_trade_entry_snapshots (paper_trade_id INTEGER PRIMARY KEY, entry_snapshot_version, sl_pct_at_entry)")
    c.execute("CREATE TABLE paper_migrations (name PRIMARY KEY, cutover_ts)")
    c.execute("INSERT INTO paper_migrations VALUES ('price_provenance_v1',?)", (CUTOVER,))
    for i in range(1, 4):
        r = {k: v for k, v in base.items() if k in columns}
        r.update(id=i, token_id=f"token{i}", actionable=i % 2,
                 closed_at=f"2026-07-{10+i}T00:00:00Z", pnl_pct=-99)
        if i == 2:
            r["exit_provenance"] = "stop_gap_model"
        c.execute(f"INSERT INTO paper_trades ({','.join(r)}) VALUES ({','.join('?' for _ in r)})", tuple(r.values()))
        c.execute("INSERT INTO paper_trade_entry_snapshots VALUES (?, 'v1', 25)", (i,))
    c.commit()
    c.close()
    return path


async def test_history_enrichment_preserves_rows_pagination_and_readonly(tmp_path):
    path = history_db(tmp_path)
    before = path.read_bytes()
    rows = await get_trading_history(str(path), limit=2)
    assert [r["id"] for r in rows] == [3, 2]
    assert rows[0]["stop_shortfall"]["shortfall_pp"] == pytest.approx(3)
    assert rows[1]["stop_shortfall"]["state"] == "modeled"
    assert rows[0]["pnl_pct"] == -99
    assert "remaining_qty" not in rows[0]  # internal evidence stays internal
    assert [r["id"] for r in await get_trading_history(str(path), 2, 1)] == [2, 1]
    assert [r["id"] for r in await get_trading_history(str(path), actionability="actionable")] == [3, 1]
    assert await get_trading_history_count(str(path), actionability="actionable") == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("ddl,reason", [
    ("DROP TABLE paper_trade_entry_snapshots", "evidence_schema_unavailable"),
    ("DROP TABLE paper_migrations", "evidence_schema_unavailable"),
    ("DELETE FROM paper_migrations", "timestamp_unverified"),
    ("ALTER TABLE paper_trade_entry_snapshots DROP COLUMN sl_pct_at_entry", "evidence_schema_unavailable"),
    ("ALTER TABLE paper_trades DROP COLUMN remaining_qty", "invalid_numeric_evidence"),
])
async def test_missing_evidence_keeps_history(tmp_path, ddl, reason):
    path = history_db(tmp_path)
    with sqlite3.connect(path) as c:
        c.execute(ddl)
    rows = await get_trading_history(str(path))
    assert [r["id"] for r in rows] == [3, 2, 1]
    assert rows[0]["stop_shortfall"]["exclusion_reason"] == reason
    assert rows[1]["stop_shortfall"]["state"] == "modeled"


@pytest.mark.parametrize("field", ["conviction_locked_at", "leg_1_filled_at", "leg_2_filled_at"])
def test_absent_modification_column_is_not_proof_of_no_modification(field):
    row = evidence()
    del row[field]
    assert classify_stop_shortfall(row, CUTOVER)["exclusion_reason"] == "invalid_numeric_evidence"


async def test_unexpected_enrichment_failure_is_visible_and_preserves_history(tmp_path, monkeypatch, capsys):
    import aiosqlite
    original = aiosqlite.Connection.execute

    def fail_evidence(self, sql, *args, **kwargs):
        if "SELECT p.*, s.entry_snapshot_version" in sql:
            raise aiosqlite.OperationalError("injected I/O failure")
        return original(self, sql, *args, **kwargs)

    path = history_db(tmp_path)
    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_evidence)
    rows = await get_trading_history(str(path))
    assert [r["id"] for r in rows] == [3, 2, 1]
    assert rows[0]["stop_shortfall"]["exclusion_reason"] == "evidence_query_failed"
    assert rows[1]["stop_shortfall"]["state"] == "modeled"
    assert "stop_shortfall_evidence_query_failed" in capsys.readouterr().out


def test_overflow_and_tolerances():
    assert classify_stop_shortfall(evidence(quantity=1e308, remaining_qty=1e308), CUTOVER)["exclusion_reason"] == "inconsistent_notional"
    assert classify_stop_shortfall(evidence(entry_price=1e-308, amount_usd=1e-307, exit_price=1e308), CUTOVER)["state"] == "unavailable"
    assert classify_stop_shortfall(evidence(remaining_qty=10 + 1e-10), CUTOVER)["state"] == "available"
    assert classify_stop_shortfall(evidence(remaining_qty=10 + 1e-7), CUTOVER)["exclusion_reason"] == "partial_exit"

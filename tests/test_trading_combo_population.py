"""D2 — canonical outcome population + parole-completion gate.

Population is defined by VALIDITY, not by an exit-status whitelist. The locked
spec (docs/superpowers/specs/2026-04-18-paper-trading-feedback-loop-design.md)
says "only closed trades count (status != 'open')"; the hand-maintained
`CLOSED_COUNTABLE_STATUSES` drifted away from that, hiding 106 real
`closed_time_death` closes from the feedback loop.

And a suppressed combo may not be judged until its CURRENT parole generation
has produced `FEEDBACK_PAROLE_RETEST_TRADES` RESOLVED outcomes. An exhausted
slot counter does not prove that: slots are spent at the admission gate, so the
counter can reach zero while trades are still open, and the D1 reservation
deliberately leaks slots on ambiguous outcomes.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh

_counter = itertools.count()


async def _insert_trade(
    db,
    combo_key: str,
    pnl_usd: float,
    pnl_pct: float,
    closed_at: datetime,
    status: str = "closed_tp",
    opened_at: datetime | None = None,
):
    opened = (opened_at or closed_at - timedelta(hours=1)).isoformat()
    token_id = f"tok_{combo_key}_{next(_counter)}"
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, pnl_usd, pnl_pct, opened_at, closed_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, ?, ?, ?, ?, ?, ?)",
        (token_id, status, pnl_usd, pnl_pct, opened, closed_at.isoformat(), combo_key),
    )
    await db._conn.commit()


async def _insert_non_close(db, combo_key: str, status: str):
    """A non-`open` row that is NOT a close: no closed_at, no pnl.

    Mirrors the real `kraken_pilot_anchor` / `solana_lane_anchor` rows in prod.
    Also used with status='open' for an admitted-but-unresolved trade.
    """
    token_id = f"tok_{combo_key}_{next(_counter)}"
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, pnl_usd, pnl_pct, opened_at, closed_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, ?, NULL, NULL, ?, NULL, ?)",
        (token_id, status, datetime.now(timezone.utc).isoformat(), combo_key),
    )
    await db._conn.commit()


async def _get_row(db, combo_key: str, window: str = "30d"):
    cur = await db._conn.execute(
        "SELECT * FROM combo_performance WHERE combo_key = ? AND window = ?",
        (combo_key, window),
    )
    return await cur.fetchone()


async def _seed_paroled(db, combo: str, parole_at: datetime, remaining: int = 0):
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', 25, 5, 20, -100.0, -2.0, 20.0, 1, ?, ?, ?, 0, ?)",
        (
            combo,
            (parole_at - timedelta(days=14)).isoformat(),
            parole_at.isoformat(),
            remaining,
            parole_at.isoformat(),
        ),
    )
    await db._conn.commit()


@pytest.mark.parametrize("resolved", [0, 1, 2, 3, 4])
async def test_parole_decision_held_until_retest_resolves(
    tmp_path, settings_factory, resolved
):
    """Exhausted slots + fewer than 5 resolved outcomes -> HOLD, do not decide.

    This is the `losers_contrarian` case: 3 valid closes would have cleared a
    suppression earned on 103 trades, because `parole_trades_remaining == 0`
    was taken as proof the 5-trade retest had happened.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    parole_at = now - timedelta(days=1)
    await _seed_paroled(db, "held", parole_at, remaining=0)
    # Winners — so that without the gate this WOULD clear.
    for _ in range(resolved):
        await _insert_trade(
            db,
            "held",
            10,
            5.0,
            now - timedelta(hours=1),
            opened_at=now - timedelta(hours=12),
        )
    await combo_refresh.refresh_combo(db, "held", s)
    row = await _get_row(db, "held")
    assert row["suppressed"] == 1
    assert row["parole_at"] == parole_at.isoformat(), "parole window must not reset"
    assert row["parole_trades_remaining"] == 0, "budget must not be re-armed"
    await db.close()


async def test_parole_decision_taken_at_exactly_five_resolved(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _seed_paroled(db, "exactly5", now - timedelta(days=1), remaining=0)
    for _ in range(s.FEEDBACK_PAROLE_RETEST_TRADES):
        await _insert_trade(
            db,
            "exactly5",
            10,
            5.0,
            now - timedelta(hours=1),
            opened_at=now - timedelta(hours=12),
        )
    await combo_refresh.refresh_combo(db, "exactly5", s)
    row = await _get_row(db, "exactly5")
    assert row["suppressed"] == 0, "5 resolved winners must clear"
    await db.close()


async def test_open_trades_do_not_count_toward_retest_completion(
    tmp_path, settings_factory
):
    """Slots are spent at the GATE, so the counter can hit 0 with trades open."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _seed_paroled(db, "stillopen", now - timedelta(days=1), remaining=0)
    for _ in range(3):
        await _insert_trade(
            db,
            "stillopen",
            10,
            5.0,
            now - timedelta(hours=1),
            opened_at=now - timedelta(hours=12),
        )
    for _ in range(2):  # admitted, slot spent, not yet resolved
        await _insert_non_close(db, "stillopen", "open")
    await combo_refresh.refresh_combo(db, "stillopen", s)
    row = await _get_row(db, "stillopen")
    assert row["suppressed"] == 1, "3 resolved + 2 open is not a completed retest"
    await db.close()


async def test_retest_cohort_ignores_outcomes_from_a_previous_generation(
    tmp_path, settings_factory
):
    """Trades opened BEFORE parole_at belong to the prior generation."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    parole_at = now - timedelta(days=1)
    await _seed_paroled(db, "priorgen", parole_at, remaining=0)
    for _ in range(6):  # closed inside the 30d window, opened before the parole
        await _insert_trade(
            db,
            "priorgen",
            10,
            5.0,
            now - timedelta(hours=1),
            opened_at=now - timedelta(days=5),
        )
    await combo_refresh.refresh_combo(db, "priorgen", s)
    row = await _get_row(db, "priorgen")
    assert row["suppressed"] == 1, "prior-generation closes are not retest evidence"
    await db.close()


async def test_sentinel_rows_never_enter_the_population(tmp_path, settings_factory):
    """`kraken_pilot_anchor` / `solana_lane_anchor` satisfy `status != 'open'`.

    They carry no close and no P&L, so a literal reading of the spec would put
    them in COUNT(*) contributing neither a win nor a loss — silently dragging
    win-rate toward the suppression threshold.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    for _ in range(10):
        await _insert_trade(db, "sent", 10, 5.0, now - timedelta(days=1))
    for st in ("kraken_pilot_anchor", "solana_lane_anchor"):
        await _insert_non_close(db, "sent", st)
    await combo_refresh.refresh_combo(db, "sent", s)
    row = await _get_row(db, "sent")
    assert row["trades"] == 10, "sentinel rows leaked into the denominator"
    assert abs(row["win_rate_pct"] - 100.0) < 0.01
    await db.close()


async def test_previously_drifted_statuses_now_count(tmp_path, settings_factory):
    """`closed_time_death` / `closed_floor` / `closed_peak_fade` are real outcomes.

    All three were invisible to the feedback loop under the whitelist;
    `closed_time_death` alone produced 106 real closes while unregistered.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    for st in ("closed_time_death", "closed_floor", "closed_peak_fade"):
        await _insert_trade(db, "drift", -5, -3, now - timedelta(days=1), status=st)
    await _insert_trade(db, "drift", 10, 5.0, now - timedelta(days=1))
    await combo_refresh.refresh_combo(db, "drift", s)
    row = await _get_row(db, "drift")
    assert row["trades"] == 4, "drifted statuses still excluded from the population"
    assert row["wins"] == 1
    await db.close()


async def test_closed_manual_stays_excluded(tmp_path, settings_factory):
    """NOT drift — a deliberate, spec-era exclusion.

    `closed_manual` is an OPERATOR-initiated force close: an operator action,
    not evidence about the signal, landing at ~0 pnl. It was excluded in #29 —
    the same PR that implemented the locked spec — and pinned there by
    `test_closed_manual_excluded_from_wr`. Unlike `closed_floor` (#48),
    `closed_peak_fade` (#50) and `closed_time_death` (#457), it carries a test
    and a stated rationale, so restoring the population must not sweep it in.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    for _ in range(10):
        await _insert_trade(db, "manual", 10, 5.0, now - timedelta(days=1))
    for _ in range(10):
        await _insert_trade(
            db, "manual", 0.0, 0.0, now - timedelta(days=1), status="closed_manual"
        )
    await combo_refresh.refresh_combo(db, "manual", s)
    row = await _get_row(db, "manual")
    assert row["trades"] == 10
    assert abs(row["win_rate_pct"] - 100.0) < 0.01
    await db.close()

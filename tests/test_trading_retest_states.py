"""D2 correction — retest state machine across three independent axes.

Calendar membership (`opened_at >= parole_at`), slot consumption
(`parole_trades_remaining`) and usable resolved evidence are DISTINCT facts.
Treating any one as proof of the others is what let three closes clear a
suppression earned on 103 trades.

States:
  waiting                 — under target, but slots or open trades remain
  terminal_incomplete     — slots exhausted, nothing open, under target:
                            can NEVER complete; page once, hold
  accounting_inconsistent — enough evidence but slots remain
  contaminated            — more committed trades than reservations spent
  complete                — target met AND slots exhausted -> WR decides
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh

_counter = itertools.count()


class _StubAlert:
    def __init__(self):
        self.calls = 0
        self.messages: list[str] = []

    async def __call__(self, message, settings):
        self.calls += 1
        self.messages.append(message)


async def _close(
    db,
    combo: str,
    parole_at: datetime,
    *,
    pnl: float = 10.0,
    status: str = "closed_tp",
    provenance: str | None = None,
):
    now = datetime.now(timezone.utc)
    token_id = f"tok_{combo}_{next(_counter)}"
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, pnl_usd, pnl_pct, opened_at, closed_at, signal_combo, "
        " exit_provenance) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, ?, ?, ?, ?, ?, ?, ?)",
        (
            token_id,
            status,
            pnl,
            pnl,
            (parole_at + timedelta(hours=1)).isoformat(),
            (now - timedelta(minutes=5)).isoformat(),
            combo,
            provenance,
        ),
    )
    await db._conn.commit()


async def _still_open(db, combo: str, parole_at: datetime):
    token_id = f"tok_{combo}_{next(_counter)}"
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, pnl_usd, pnl_pct, opened_at, closed_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'open', NULL, NULL, ?, NULL, ?)",
        (token_id, (parole_at + timedelta(hours=1)).isoformat(), combo),
    )
    await db._conn.commit()


async def _seed(db, combo: str, parole_at: datetime, remaining: int):
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


async def _row(db, combo: str):
    cur = await db._conn.execute(
        "SELECT * FROM combo_performance WHERE combo_key = ? AND window = '30d'",
        (combo,),
    )
    return await cur.fetchone()


async def _refresh(db, combo: str, s):
    """Refresh + run the post-lock alert pass, as refresh_all does.

    Delivery is deliberately NOT inside refresh_combo: that holds `_txn_lock`
    and sits mid-transaction, so a Telegram await would pin the trading lock
    and the marker commit would split the refresh transaction.
    """
    ok = await combo_refresh.refresh_combo(db, combo, s)
    await combo_refresh._process_retest_terminal_incomplete(db, s)
    return ok


async def _setup(tmp_path, settings_factory, monkeypatch, combo, remaining):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    stub = _StubAlert()
    monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", stub)
    parole_at = datetime.now(timezone.utc) - timedelta(days=2)
    await _seed(db, combo, parole_at, remaining)
    return db, s, stub, parole_at


async def test_enough_evidence_but_slots_remain_holds(
    tmp_path, settings_factory, monkeypatch
):
    """5 valid closes with remaining > 0 — the two ledgers disagree; do not decide."""
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "inconsistent", remaining=2
    )
    for _ in range(5):
        await _close(db, "inconsistent", parole_at)  # winners: would CLEAR
    await _refresh(db, "inconsistent", s)
    row = await _row(db, "inconsistent")
    assert row["suppressed"] == 1, "must not clear on inconsistent accounting"
    assert row["parole_trades_remaining"] == 2, "must not re-arm"
    await db.close()


async def test_more_committed_trades_than_slots_spent_is_contaminated(
    tmp_path, settings_factory, monkeypatch
):
    """A bypass path (e.g. tg_social) can add post-parole closes without a slot."""
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "bypassed", remaining=0
    )
    # spent = 5 - 0 = 5, but 7 trades committed in the generation.
    for _ in range(7):
        await _close(db, "bypassed", parole_at)
    await _refresh(db, "bypassed", s)
    row = await _row(db, "bypassed")
    assert row["suppressed"] == 1, "contaminated cohort must not decide"
    await db.close()


async def test_four_valid_plus_closed_manual_is_terminal_incomplete(
    tmp_path, settings_factory, monkeypatch
):
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "manualgap", remaining=0
    )
    for _ in range(4):
        await _close(db, "manualgap", parole_at)
    await _close(db, "manualgap", parole_at, pnl=0.0, status="closed_manual")
    await _refresh(db, "manualgap", s)
    row = await _row(db, "manualgap")
    assert row["suppressed"] == 1
    assert stub.calls == 1, "terminal-incomplete must page"
    assert row["retest_incomplete_alerted_at"] is not None
    await db.close()


async def test_four_valid_plus_invalid_provenance_is_terminal_incomplete(
    tmp_path, settings_factory, monkeypatch
):
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "provgap", remaining=0
    )
    for _ in range(4):
        await _close(db, "provgap", parole_at)
    await _close(db, "provgap", parole_at, pnl=0.0, provenance="entry_fallback")
    await _refresh(db, "provgap", s)
    row = await _row(db, "provgap")
    assert row["suppressed"] == 1
    assert stub.calls == 1
    await db.close()


async def test_three_valid_plus_two_open_is_waiting_not_terminal(
    tmp_path, settings_factory, monkeypatch
):
    """Still resolvable — must NOT page."""
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "waiting", remaining=0
    )
    for _ in range(3):
        await _close(db, "waiting", parole_at)
    for _ in range(2):
        await _still_open(db, "waiting", parole_at)
    await _refresh(db, "waiting", s)
    row = await _row(db, "waiting")
    assert row["suppressed"] == 1
    assert stub.calls == 0, "waiting must not page — it can still resolve"
    assert row["retest_incomplete_alerted_at"] is None
    await db.close()


async def test_three_valid_plus_leaked_slots_is_terminal_incomplete(
    tmp_path, settings_factory, monkeypatch
):
    """D1 leaks a slot on ambiguous outcomes; the retest can then never finish."""
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "leaked", remaining=0
    )
    for _ in range(3):
        await _close(db, "leaked", parole_at)
    await _refresh(db, "leaked", s)
    row = await _row(db, "leaked")
    assert row["suppressed"] == 1
    assert row["parole_trades_remaining"] == 0, "must NOT silently re-arm slots"
    assert stub.calls == 1
    await db.close()


async def test_terminal_incomplete_pages_only_once(
    tmp_path, settings_factory, monkeypatch
):
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "once", remaining=0
    )
    for _ in range(3):
        await _close(db, "once", parole_at)
    await _refresh(db, "once", s)
    await _refresh(db, "once", s)
    await _refresh(db, "once", s)
    assert stub.calls == 1, "nightly refresh must not re-page a stuck generation"
    await db.close()


async def test_alert_marker_not_set_when_send_fails(
    tmp_path, settings_factory, monkeypatch
):
    """A transient Telegram outage must re-attempt, not silently drop the page."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()

    async def _boom(message, settings):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", _boom)
    parole_at = datetime.now(timezone.utc) - timedelta(days=2)
    await _seed(db, "outage", parole_at, 0)
    for _ in range(3):
        await _close(db, "outage", parole_at)
    await _refresh(db, "outage", s)
    row = await _row(db, "outage")
    assert row["retest_incomplete_alerted_at"] is None, "marker set despite failure"
    assert row["suppressed"] == 1
    await db.close()


async def test_exactly_five_valid_and_slots_exhausted_decides(
    tmp_path, settings_factory, monkeypatch
):
    db, s, stub, parole_at = await _setup(
        tmp_path, settings_factory, monkeypatch, "decide", remaining=0
    )
    for _ in range(5):
        await _close(db, "decide", parole_at)  # winners
    await _refresh(db, "decide", s)
    row = await _row(db, "decide")
    assert row["suppressed"] == 0, "complete retest with WR>=30 must clear"
    assert stub.calls == 0
    await db.close()


def _acct(valid, invalid=0, open_now=0, cohort=None, spent=5):
    cohort = cohort if cohort is not None else valid + invalid + open_now
    return dict(
        valid_closed=valid,
        invalid_closed=invalid,
        open_now=open_now,
        cohort_total=cohort,
        spent=spent,
        contaminated=cohort > spent,
    )


def test_classify_retest_state_table():
    """Pins each label AND the ordering.

    `accounting_inconsistent` strictly implies `contaminated` (cohort >= target
    > spent), so it must be tested first or it becomes unreachable and the
    operator sees the less specific diagnosis.
    """
    T = 5
    c = combo_refresh._classify_retest
    # Enough evidence, slots remain -> the specific accounting diagnosis.
    assert c(_acct(5, spent=3), 2, T) == "accounting_inconsistent"
    # Under target, more committed than spent -> bypass contamination.
    assert c(_acct(3, cohort=7, spent=5), 0, T) == "contaminated"
    # Target met, slots exhausted -> decide.
    assert c(_acct(5, spent=5), 0, T) == "complete"
    # Under target, exhausted, nothing open -> can never finish.
    assert c(_acct(3, spent=5), 0, T) == "terminal_incomplete"
    # Under target, trades still open -> still resolvable.
    assert c(_acct(3, open_now=2, spent=5), 0, T) == "waiting"
    # Under target, slots still available -> still resolvable.
    assert c(_acct(1, spent=1), 4, T) == "waiting"


async def test_upgrade_adds_retest_marker_column_to_existing_table(tmp_path):
    """Upgrade-with-data vector: build the OLD shape, then migrate.

    A fresh `tmp_path` DB is created already-migrated and would never exercise
    this path. So create combo_performance WITHOUT the new column, insert a
    row, then run initialize() and assert the ALTER landed with existing data
    intact and the new column NULL (= "never alerted", the correct pre-cutover
    state).
    """
    import aiosqlite

    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(path)
    await conn.execute("""CREATE TABLE combo_performance (
               combo_key TEXT, window TEXT, trades INTEGER, wins INTEGER,
               losses INTEGER, total_pnl_usd REAL, avg_pnl_pct REAL,
               win_rate_pct REAL, suppressed INTEGER, suppressed_at TEXT,
               parole_at TEXT, parole_trades_remaining INTEGER,
               refresh_failures INTEGER, last_refreshed TEXT,
               perm_suppression_alerted_at TEXT,
               PRIMARY KEY (combo_key, window))""")
    await conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('legacy', '30d', 40, 8, 32, -9.0, -1.0, 20.0, 1, 0, 'x')"
    )
    await conn.commit()
    await conn.close()

    db = Database(path)
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(combo_performance)")
    cols = {r[1] for r in await cur.fetchall()}
    assert "retest_incomplete_alerted_at" in cols, "ALTER did not land"

    cur = await db._conn.execute(
        "SELECT trades, suppressed, retest_incomplete_alerted_at "
        "FROM combo_performance WHERE combo_key = 'legacy'"
    )
    row = await cur.fetchone()
    assert row[0] == 40, "pre-existing data lost"
    assert row[1] == 1, "pre-existing suppression state lost"
    assert row[2] is None, "new column must default to NULL (never alerted)"
    await db.close()


async def test_real_alerter_seam_passes_raise_on_failure(monkeypatch):
    """Contract test on the REAL seam, not the monkeypatched wrapper.

    The alerter defaults `raise_on_failure=False` and merely logs on a non-200
    or network error. If this call does not opt in, `_process_...` would log
    `..._delivered` and set the dedup marker for a page Telegram rejected —
    permanently suppressing every retry.
    """
    import scout.alerter as alerter_mod

    captured: dict = {}

    async def fake_send(message, session, settings, **kw):
        captured.update(kw)
        raise RuntimeError("telegram 400")

    monkeypatch.setattr(alerter_mod, "send_telegram_message", fake_send)

    with pytest.raises(RuntimeError):
        await combo_refresh._send_retest_incomplete_alert("body", object())

    assert captured.get("raise_on_failure") is True, "failure cannot propagate"
    assert captured.get("parse_mode") is None, "combo keys contain underscores"
    assert captured.get("source") == "combo_refresh_retest_terminal_incomplete"


async def test_marker_not_written_when_real_alerter_fails(
    tmp_path, settings_factory, monkeypatch
):
    """End-to-end through the real seam: a rejected page must stay un-marked."""
    import scout.alerter as alerter_mod

    async def fake_send(message, session, settings, **kw):
        raise RuntimeError("telegram 500")

    monkeypatch.setattr(alerter_mod, "send_telegram_message", fake_send)

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    parole_at = datetime.now(timezone.utc) - timedelta(days=2)
    await _seed(db, "realfail", parole_at, 0)
    for _ in range(3):
        await _close(db, "realfail", parole_at)

    await combo_refresh.refresh_combo(db, "realfail", s)
    await combo_refresh._process_retest_terminal_incomplete(db, s)

    row = await _row(db, "realfail")
    assert row["retest_incomplete_alerted_at"] is None
    assert row["suppressed"] == 1
    await db.close()


async def test_marker_write_is_generation_bound(
    tmp_path, settings_factory, monkeypatch
):
    """A slow send followed by a re-arm must not stamp the NEW generation.

    Same principle as the D1 reservation: a write that lands after the
    generation moved would mark a fresh parole as already-alerted, silencing
    the page it is entitled to.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    parole_at = datetime.now(timezone.utc) - timedelta(days=2)
    await _seed(db, "genshift", parole_at, 0)
    for _ in range(3):
        await _close(db, "genshift", parole_at)

    async def slow_send(message, settings):
        # combo_refresh re-arms a NEW parole generation mid-flight.
        await db._conn.execute(
            "UPDATE combo_performance SET suppressed_at = ?, parole_at = ?, "
            "parole_trades_remaining = 5 "
            "WHERE combo_key = 'genshift' AND window = '30d'",
            (
                datetime.now(timezone.utc).isoformat(),
                (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
            ),
        )
        await db._conn.commit()

    monkeypatch.setattr(combo_refresh, "_send_retest_incomplete_alert", slow_send)
    alerted = await combo_refresh._process_retest_terminal_incomplete(db, s)

    row = await _row(db, "genshift")
    assert (
        row["retest_incomplete_alerted_at"] is None
    ), "stale marker stamped the replacement generation"
    assert alerted == [], "must not report an alert against a dead generation"
    await db.close()

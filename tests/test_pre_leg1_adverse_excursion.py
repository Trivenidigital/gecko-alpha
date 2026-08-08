"""Pre-leg-1 adverse excursion — the stop-width measurement window.

The whole-life ``mae_pct`` cannot evaluate a tighter INITIAL stop. The initial
SL is gated ``if not floor_armed and ... current_price <= sl_price``; once leg 1
arms the floor, the downside rule becomes a breakeven floor at entry. ``mae_pct``
keeps deepening past that point, so a counterfactual built on it counts post-arm
dips as winners a tighter stop would have killed — which is false, because the
stop was no longer eligible to fire.

Measured on the only cohort carrying the column (2026-08): 7 of 47 floor-armed
closes dipped past -12% and ALL SEVEN were winners; 0 of the 19 not-yet-armed
trades that dipped past -12% won. The bias is material and runs in the direction
that makes tightening look worse than it is.

``pre_leg1_mae_pct`` freezes when leg 1 arms, so it answers: how far did this
position dip while the initial stop could still have fired?
"""

from __future__ import annotations

import pytest

from scout.db import Database


async def _open_trade(db: Database, **overrides) -> int:
    """Insert one open paper trade and return its id.

    `opened_at` defaults to the pre-leg-1 migration's own cutover_ts, so the
    default trade is post-cutover and therefore measurable. Hardcoding a literal
    date here would silently make every test a pre-cutover case the moment the
    provenance gate landed — which is exactly what happened on the first run.
    """
    conn = db._conn
    assert conn is not None
    cur_c = await conn.execute(
        "SELECT cutover_ts FROM paper_migrations "
        "WHERE name = 'bl_pre_leg1_adverse_excursion_v1'"
    )
    row_c = await cur_c.fetchone()
    default_opened_at = row_c[0] if row_c else "2026-08-08T00:00:00+00:00"
    cols = {
        "token_id": "tok-1",
        "symbol": "TOK",
        "name": "Token",
        "chain": "coingecko",
        "signal_type": "losers_contrarian",
        "signal_data": "{}",
        "entry_price": 100.0,
        "amount_usd": 150.0,
        "quantity": 1.5,
        "tp_pct": 50.0,
        "sl_pct": 25.0,
        "tp_price": 150.0,
        "sl_price": 75.0,
        "status": "open",
        "opened_at": default_opened_at,
        "created_at": default_opened_at,
    }
    cols.update(overrides)
    names = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    cur = await conn.execute(
        f"INSERT INTO paper_trades ({names}) VALUES ({marks})", tuple(cols.values())
    )
    await conn.commit()
    return int(cur.lastrowid)


async def _read(db: Database, trade_id: int) -> dict:
    conn = db._conn
    assert conn is not None
    cur = await conn.execute(
        "SELECT mae_pct, pre_leg1_mae_pct, pre_leg1_trough_price, floor_armed "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cur.fetchone()
    return {
        "mae_pct": row[0],
        "pre_leg1_mae_pct": row[1],
        "pre_leg1_trough_price": row[2],
        "floor_armed": row[3],
    }


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.initialize()
    yield d
    await d.close()


class TestMigration:
    async def test_columns_exist_and_are_nullable_with_no_default(self, db, settings_factory):
        """NULL must keep exactly one meaning: never measured.

        A DEFAULT 0 would silently assert "never dipped" for every historical
        row — the same trap the mae_pct migration avoided.
        """
        conn = db._conn
        cur = await conn.execute("PRAGMA table_info(paper_trades)")
        info = {r[1]: r for r in await cur.fetchall()}
        for col in ("pre_leg1_trough_price", "pre_leg1_mae_pct"):
            assert col in info, f"{col} missing"
            assert info[col][2] == "REAL"
            assert info[col][3] == 0, f"{col} must be nullable"
            assert info[col][4] is None, f"{col} must have NO DEFAULT"

    async def test_migration_is_idempotent(self, db, settings_factory):
        """Re-running must not raise — deploys re-run migrations."""
        await db._migrate_pre_leg1_adverse_excursion_v1()
        await db._migrate_pre_leg1_adverse_excursion_v1()
        conn = db._conn
        cur = await conn.execute("PRAGMA table_info(paper_trades)")
        names = [r[1] for r in await cur.fetchall()]
        assert names.count("pre_leg1_mae_pct") == 1

    async def test_no_backfill_existing_rows_stay_null(self, db, settings_factory):
        """The pre-arm price path of a closed trade is unrecoverable."""
        tid = await _open_trade(db)
        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] is None
        assert got["pre_leg1_trough_price"] is None


class TestMeasurementWindow:
    """*** THE POINT OF THE COLUMN. ***

    These two tests are the discriminating pair. If both pass, the counterfactual
    the pilot depends on is temporally valid; if either fails it is not.
    """

    async def test_dip_BEFORE_leg1_is_counted(self, db, settings_factory):
        """SL eligible -> the excursion belongs in the stop-width evidence."""
        from scout.trading import evaluator

        tid = await _open_trade(db, floor_armed=0)
        await _tick(db, evaluator, settings_factory(), tid, price=85.0)  # -15%, floor not armed

        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] is not None, "must be measured pre-arm"
        assert got["pre_leg1_mae_pct"] == pytest.approx(-15.0, abs=0.01)
        # A -12% candidate stop WOULD have fired here.
        assert got["pre_leg1_mae_pct"] <= -12.0

    async def test_dip_ONLY_AFTER_leg1_is_NOT_counted(self, db, settings_factory):
        """SL structurally ineligible -> must NOT appear as stop-width damage.

        This is the exact shape that contaminates whole-life mae_pct: a runner
        that arms the floor, dips hard, and still closes profitable.
        """
        from scout.trading import evaluator

        tid = await _open_trade(db, floor_armed=1)
        await _tick(db, evaluator, settings_factory(), tid, price=85.0)  # -15%, but floor IS armed

        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] is None, (
            "post-arm dip must not be recorded as pre-leg-1 excursion — "
            "the initial stop could not have fired"
        )

    async def test_value_FREEZES_at_arm_and_does_not_deepen(self, db, settings_factory):
        """Freeze, don't null: the pre-arm window was genuinely measured."""
        from scout.trading import evaluator

        tid = await _open_trade(db, floor_armed=0)
        await _tick(db, evaluator, settings_factory(), tid, price=92.0)  # -8% while SL eligible
        frozen = (await _read(db, tid))["pre_leg1_mae_pct"]
        assert frozen == pytest.approx(-8.0, abs=0.01)

        # Leg 1 arms, then the position dips far deeper.
        conn = db._conn
        await conn.execute(
            "UPDATE paper_trades SET floor_armed = 1 WHERE id = ?", (tid,)
        )
        await conn.commit()
        await _tick(db, evaluator, settings_factory(), tid, price=70.0)  # -30% post-arm

        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] == pytest.approx(frozen, abs=0.01), (
            "must freeze at arm time"
        )
        # The whole-life column DOES deepen — that asymmetry is the whole point.
        assert got["mae_pct"] < got["pre_leg1_mae_pct"]

    async def test_never_dipped_records_zero_not_null(self, db, settings_factory):
        """0.0 (never dipped while SL live) must be distinguishable from NULL."""
        from scout.trading import evaluator

        tid = await _open_trade(db, floor_armed=0)
        await _tick(db, evaluator, settings_factory(), tid, price=110.0)  # only ever rose

        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] == pytest.approx(0.0, abs=0.01)
        assert got["pre_leg1_mae_pct"] is not None


class TestCutoverProvenance:
    """*** THE THIRD STATE IS THE DANGEROUS ONE. ***

    NULL means never measured. A real value means fully measured. A trade that
    was ALREADY OPEN when this shipped would otherwise get a value covering only
    the post-deploy remainder of its pre-arm window — non-NULL, so it passes the
    `IS NOT NULL` eligibility filter, while silently missing everything before
    the deploy. That is worse than either other state, because the filter is
    precisely what every downstream analysis is told to trust.
    """

    @staticmethod
    async def _cutover(db) -> str:
        cur = await db._conn.execute(
            "SELECT cutover_ts FROM paper_migrations "
            "WHERE name = 'bl_pre_leg1_adverse_excursion_v1'"
        )
        return (await cur.fetchone())[0]

    async def test_a_trade_open_BEFORE_cutover_stays_NULL_forever(
        self, db, settings_factory
    ):
        from scout.trading import evaluator

        tid = await _open_trade(
            db, floor_armed=0, opened_at="2026-01-01T00:00:00+00:00"
        )
        # Several valid ticks, all dipping, all while the floor is unarmed.
        for px in (90.0, 80.0, 70.0):
            await _tick(db, evaluator, settings_factory(), tid, price=px)

        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] is None, (
            "a pre-cutover trade must never receive a partial pre-leg-1 "
            "measurement — its earlier adverse path is unrecoverable"
        )
        assert got["pre_leg1_trough_price"] is None
        # The whole-life column is unaffected: it was always partial for these
        # rows and is documented as such.
        assert got["mae_pct"] is not None

    async def test_a_trade_open_AFTER_cutover_DOES_populate(
        self, db, settings_factory
    ):
        """The control. Without it, the test above passes if the write is
        broken for every row, which is how the first version of this file
        passed while nothing was written at all."""
        from scout.trading import evaluator

        cutover = await self._cutover(db)
        tid = await _open_trade(db, floor_armed=0, opened_at=cutover)
        await _tick(db, evaluator, settings_factory(), tid, price=85.0)

        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] == pytest.approx(-15.0, abs=0.01)

    async def test_it_fails_CLOSED_when_the_cutover_row_is_missing(
        self, db, settings_factory
    ):
        """Unknown cutover must write nothing, and must do so BY THE GUARD.

        The NULL assertion alone is not enough. A mutant that drops the
        `is not None` check makes `opened_dt >= None` raise TypeError, which the
        per-trade `except Exception: ... continue` swallows -- so the column is
        still NULL and a NULL-only test passes while the evaluator has actually
        stopped processing that trade entirely (no TP, no SL, no expiry).

        Price is set below sl_price so the SL branch -- which lives AFTER the
        pre-leg-1 block -- must still fire. That distinguishes "the guard
        declined to write" from "the loop blew up before getting there".
        """
        from scout.trading import evaluator

        await db._conn.execute(
            "DELETE FROM paper_migrations "
            "WHERE name = 'bl_pre_leg1_adverse_excursion_v1'"
        )
        await db._conn.commit()
        tid = await _open_trade(db, floor_armed=0)
        await _tick(db, evaluator, settings_factory(), tid, price=70.0)  # < sl 75

        got = await _read(db, tid)
        assert got["pre_leg1_mae_pct"] is None
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (tid,)
        )
        assert (await cur.fetchone())[0] == "closed_sl", (
            "the rest of the tick must still run — a swallowed exception would "
            "leave this open and silently disable every downstream exit path"
        )


async def _tick(db, evaluator, settings, trade_id: int, *, price: float) -> None:
    """Run one evaluator pass with `price` in the price cache."""
    conn = db._conn
    await conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(coin_id) DO UPDATE SET current_price = excluded.current_price, "
        "updated_at = excluded.updated_at",
        ("tok-1", price),
    )
    await conn.commit()
    await evaluator.evaluate_paper_trades(db, settings)

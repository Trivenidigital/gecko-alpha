"""Maximum adverse excursion (`trough_price` / `mae_pct`) on paper_trades.

Why this column exists: the 2026-08-03 exit-mechanics analysis could not evaluate
stop width at all. For rows that closed at `stop_loss` the SAVING from a tighter
stop is computable, but for every other close the COST — trades a tighter stop
would newly convert into losses — is not, because nothing recorded how far a
position dipped before recovering. That asymmetry makes tightening look strictly
beneficial on the available data.
"""

from __future__ import annotations

import aiosqlite
import pytest

from scout.db import Database


async def _parent_shaped_db(tmp_path):
    """A paper_trades WITHOUT the two columns, so the ALTER actually runs.

    A fresh `Database.initialize()` creates the already-migrated shape, so the
    migration hits its `skip_exists` branch and the ALTER is never exercised —
    a green test covering none of the real behaviour.
    """
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute("""CREATE TABLE paper_trades (
                   id INTEGER PRIMARY KEY, token_id TEXT, entry_price REAL,
                   status TEXT, peak_price REAL, peak_pct REAL)""")
        await conn.execute(
            "INSERT INTO paper_trades (id, token_id, entry_price, status) "
            "VALUES (1, 'tok', 100.0, 'closed_sl')"
        )
        await conn.commit()
    return path


class TestMigration:
    async def test_the_alter_actually_runs_on_the_old_shape(self, tmp_path):
        path = await _parent_shaped_db(tmp_path)
        db = Database(path)
        db._conn = await aiosqlite.connect(path)
        try:
            await db._migrate_trade_adverse_excursion_v1()
            cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
            cols = {r[1] for r in await cur.fetchall()}
            assert {"trough_price", "mae_pct"} <= cols
        finally:
            await db._conn.close()

    async def test_existing_rows_stay_null_not_zero(self, tmp_path):
        """*** NULL AND 0.0 MUST NOT BE CONFLATED. ***

        A closed trade's low-water mark is unrecoverable — there is no price
        path anywhere. Defaulting to 0 would assert "never dipped" for every
        historical row, which is exactly the false-precision that would make a
        stop-width backtest look favourable.
        """
        path = await _parent_shaped_db(tmp_path)
        db = Database(path)
        db._conn = await aiosqlite.connect(path)
        try:
            await db._migrate_trade_adverse_excursion_v1()
            cur = await db._conn.execute(
                "SELECT trough_price, mae_pct FROM paper_trades WHERE id = 1"
            )
            assert tuple(await cur.fetchone()) == (None, None)
        finally:
            await db._conn.close()

    async def test_it_is_idempotent(self, tmp_path):
        path = await _parent_shaped_db(tmp_path)
        db = Database(path)
        db._conn = await aiosqlite.connect(path)
        try:
            await db._migrate_trade_adverse_excursion_v1()
            await db._migrate_trade_adverse_excursion_v1()  # must not raise
            cur = await db._conn.execute(
                "SELECT COUNT(*) FROM paper_migrations "
                "WHERE name = 'bl_trade_adverse_excursion_v1'"
            )
            assert (await cur.fetchone())[0] == 1
        finally:
            await db._conn.close()


class TestExcursionArithmetic:
    """The update rule, asserted directly. The evaluator applies exactly this."""

    @staticmethod
    def _tick(entry, trough, current):
        """Mirror of the evaluator block. Returns (new_trough, mae_pct|None)."""
        if trough is None:
            trough = min(entry, current)
            write = True
        elif current < trough:
            trough = current
            write = True
        else:
            write = False
        return trough, (((trough - entry) / entry) * 100 if write else None)

    def test_a_position_that_only_rises_records_zero_not_null(self):
        """*** THE DEFECT THIS SEEDING EXISTS TO PREVENT. ***

        With a bare `current < reference` comparison, a trade that never dips
        below entry never writes, leaving mae_pct NULL for its whole life —
        indistinguishable from "never measured". NULL must mean exactly one
        thing: closed before this shipped.
        """
        trough, mae = self._tick(entry=100.0, trough=None, current=115.0)
        assert trough == 100.0
        assert mae == 0.0

    def test_it_records_the_low_water_mark_not_the_last_price(self):
        trough, mae = self._tick(entry=100.0, trough=None, current=80.0)
        assert (trough, mae) == (80.0, -20.0)
        # recovers — the trough must NOT follow the price back up
        trough2, mae2 = self._tick(entry=100.0, trough=trough, current=105.0)
        assert trough2 == 80.0
        assert mae2 is None  # no write; the stored -20.0 stands

    def test_a_deeper_dip_replaces_a_shallower_one(self):
        trough, _ = self._tick(entry=100.0, trough=90.0, current=70.0)
        assert trough == 70.0

    def test_mae_is_never_positive(self):
        """<= 0 by construction: the trough can never exceed entry, because the
        first tick seeds it at min(entry, current)."""
        for current in (50.0, 100.0, 250.0):
            _, mae = self._tick(entry=100.0, trough=None, current=current)
            assert mae is not None and mae <= 0


class TestEvaluatorWiring:
    """Structural guards — the arithmetic above is worthless if the evaluator
    does not actually run it, or reads the wrong row index."""

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        import scout.trading.evaluator as ev

        return Path(ev.__file__).read_text("utf-8")

    def test_the_evaluator_writes_the_columns(self):
        assert "SET trough_price = ?, mae_pct = ?" in self._src()

    def test_the_columns_are_appended_last_in_the_select(self):
        """*** POSITIONAL READS BREAK SILENTLY. ***

        Every read is `row[N]`. Inserting the new columns mid-list shifts every
        index after it — the query still runs and the evaluator silently reads
        the wrong field. They must stay at the END of the SELECT.
        """
        src = self._src()
        sel_start = src.index("SELECT id, token_id, entry_price")
        sel_end = src.index("FROM paper_trades", sel_start)
        select_body = src[sel_start:sel_end]
        assert "trough_price, mae_pct" in select_body
        tail = select_body.rsplit("mae_pct", 1)[1]
        assert tail.strip() in ("", ","), (
            "trough_price/mae_pct must be the LAST columns in the SELECT; "
            f"found {tail.strip()!r} after them"
        )
        assert "row[32]" in src, "trough_price must be read at its appended index"

    def test_the_seeding_branch_is_present(self):
        """Guard on the guard: `if trough_price is None` is what makes
        'never dipped' record as 0.0 instead of NULL."""
        assert "if trough_price is None:" in self._src()

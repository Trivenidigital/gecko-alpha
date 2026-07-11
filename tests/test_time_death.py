"""SIG-04 absolute time-death dry-run exit lane tests.

Evidence base: tasks/findings_w3_analysis_gates_2026_07_11.md §SIG-04. On the
clean cohort (expired, peak below leg 1 / NULL, all-time n=323), trades still
FLAT (<=0%) at the 24h checkpoint whose running peak never reached ladder leg 1
are dead capital — exiting them at 24h nets +$1,842 (bootstrap 95% CI
[+$1,339,+$2,379], all-positive) against a false-trigger cost of only 18
winners/-$438. Distinct band from momentum_death / peak_fade (which gate on a
*sustained fade from a recorded peak*): this lane gates on *absolute flatness at
24h for a peak that never reached leg 1*. Ships DRY-RUN first (records a
would-fire observation, never closes) because the backtest running-peak is
checkpoint-proxied and must be validated against the LIVE peak before any real
close.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings

_REQUIRED = dict(
    TELEGRAM_BOT_TOKEN="t",
    TELEGRAM_CHAT_ID="c",
    ANTHROPIC_API_KEY="k",
    _env_file=None,
)


async def _open_flat_trade(
    db,
    trader,
    *,
    token_id: str,
    cp_24h_pct: float,
    cp_1h_pct: float | None = 1.0,
    cp_6h_pct: float | None = 0.5,
    peak_pct: float = 2.0,
    leg_1_filled: bool = False,
    elapsed_hours: float = 25.0,
    current_price: float = 1.01,
    entry_price: float = 1.0,
):
    """Open a BL-061 trade positioned for the time-death band: aged past the
    24h checkpoint, flat at 24h, peak below leg 1, leg 1 unfilled.

    The checkpoint *price* columns are pre-seeded non-NULL so the evaluator's
    own checkpoint-recording pass does not overwrite the *pct* columns we set
    here (keeps the fixture deterministic across two eval passes). Current price
    stays above entry + stop so the SL / floor exits do not pre-empt the lane.
    """
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id,
        chain="solana",
        signal_type="gainers_early",
        signal_data={},
        current_price=entry_price,
        amount_usd=100.0,
        tp_pct=200.0,
        sl_pct=20.0,
        slippage_bps=0,
        signal_combo="gainers_early",
    )
    peak_price = entry_price * (1 + peak_pct / 100.0)
    now = datetime.now(timezone.utc)
    opened_iso = (now - timedelta(hours=elapsed_hours)).isoformat()
    now_iso = now.isoformat()
    leg_1_val = now_iso if leg_1_filled else None
    floor_val = 1 if leg_1_filled else 0
    cp_price = current_price  # non-NULL: suppress the auto-checkpoint overwrite
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = ?, peak_price = ?, peak_pct = ?, "
        "floor_armed = ?, leg_1_filled_at = ?, leg_2_filled_at = NULL, "
        "remaining_qty = quantity, "
        "checkpoint_1h_price = ?, checkpoint_6h_price = ?, checkpoint_24h_price = ?, "
        "checkpoint_1h_pct = ?, checkpoint_6h_pct = ?, checkpoint_24h_pct = ? "
        "WHERE id = ?",
        (
            opened_iso,
            peak_price,
            peak_pct,
            floor_val,
            leg_1_val,
            cp_price,
            cp_price,
            cp_price,
            cp_1h_pct,
            cp_6h_pct,
            cp_24h_pct,
            trade_id,
        ),
    )
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, current_price, now_iso),
    )
    await db._conn.commit()
    return trade_id


async def _time_death_rows(db, trade_id):
    cur = await db._conn.execute(
        "SELECT decision, reason, source_module FROM trade_decision_events "
        "WHERE paper_trade_id = ? AND reason = 'time_death_would_fire'",
        (trade_id,),
    )
    return await cur.fetchall()


async def _momentum_death_rows(db, trade_id):
    cur = await db._conn.execute(
        "SELECT decision, reason FROM trade_decision_events "
        "WHERE paper_trade_id = ? AND reason = 'momentum_death_would_fire'",
        (trade_id,),
    )
    return await cur.fetchall()


class TestConfigDefaults:
    def test_master_kill_switch_defaults_off(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_TIME_DEATH_ENABLED is False

    def test_dry_run_defaults_on(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_TIME_DEATH_DRY_RUN is True

    def test_default_flat_pct_is_zero(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_TIME_DEATH_FLAT_PCT == 0.0

    def test_default_checkpoint_h_is_24(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_TIME_DEATH_CHECKPOINT_H == 24


class TestConfigValidators:
    def test_checkpoint_h_must_be_positive(self):
        with pytest.raises(ValueError, match="must be > 0"):
            Settings(**_REQUIRED, PAPER_TIME_DEATH_CHECKPOINT_H=0)
        with pytest.raises(ValueError, match="must be > 0"):
            Settings(**_REQUIRED, PAPER_TIME_DEATH_CHECKPOINT_H=-1)

    def test_flat_pct_out_of_sane_range_rejected(self):
        with pytest.raises(ValueError, match="must be in"):
            Settings(**_REQUIRED, PAPER_TIME_DEATH_FLAT_PCT=150.0)
        with pytest.raises(ValueError, match="must be in"):
            Settings(**_REQUIRED, PAPER_TIME_DEATH_FLAT_PCT=-150.0)

    def test_flat_pct_zero_and_swept_values_accepted(self):
        # Default 0.0 is the backtested operating point; a small positive sweep
        # value (the soak may sweep FLAT<=0 -> FLAT<=3) is still accepted.
        assert Settings(**_REQUIRED, PAPER_TIME_DEATH_FLAT_PCT=0.0)
        assert (
            Settings(
                **_REQUIRED, PAPER_TIME_DEATH_FLAT_PCT=3.0
            ).PAPER_TIME_DEATH_FLAT_PCT
            == 3.0
        )


class TestFireCondition:
    @pytest.mark.asyncio
    async def test_flat_sub_leg1_fires(self, tmp_path, settings_factory):
        """Aged past 24h, flat at 24h (cp_24h<=0), peak below leg 1, leg 1
        unfilled -> records exactly one would-fire observation."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_flat", cp_24h_pct=-1.0
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        rows = await _time_death_rows(db, trade_id)
        assert len(rows) == 1, "flat sub-leg-1 trade should record one would-fire row"
        assert rows[0][0] == "observed"
        assert rows[0][1] == "time_death_would_fire"
        assert rows[0][2] == "scout.trading.evaluator"
        await db.close()

    @pytest.mark.asyncio
    async def test_not_flat_at_24h_does_not_fire(self, tmp_path, settings_factory):
        """cp_24h_pct above FLAT_PCT (0.0) means the trade is not flat/dead -> no fire."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(db, trader, token_id="tok_up", cp_24h_pct=5.0)

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        assert await _time_death_rows(db, trade_id) == []
        await db.close()

    @pytest.mark.asyncio
    async def test_elapsed_below_checkpoint_does_not_fire(
        self, tmp_path, settings_factory
    ):
        """elapsed < CHECKPOINT_H (24h) -> not yet a time-death candidate."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_young", cp_24h_pct=-1.0, elapsed_hours=10.0
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        assert await _time_death_rows(db, trade_id) == []
        await db.close()

    @pytest.mark.asyncio
    async def test_peak_reached_leg1_does_not_fire(self, tmp_path, settings_factory):
        """A checkpoint at/above PAPER_LADDER_LEG_1_PCT (25) means the token DID
        reach leg 1 territory — outside the sub-leg-1 mandate -> no fire."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        # cp_1h hit 30% (>= leg 1 25%) even though it is flat/negative now.
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_ranleg1", cp_24h_pct=-1.0, cp_1h_pct=30.0
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        assert await _time_death_rows(db, trade_id) == []
        await db.close()

    @pytest.mark.asyncio
    async def test_leg_1_filled_does_not_fire(self, tmp_path, settings_factory):
        """leg_1_filled_at IS NOT NULL -> the ladder already sold leg 1; outside
        the sub-leg-1 mandate -> no fire (mutual exclusion with the ladder)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_leg1", cp_24h_pct=-1.0, leg_1_filled=True
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        assert await _time_death_rows(db, trade_id) == []
        await db.close()


class TestDryRunNeverCloses:
    @pytest.mark.asyncio
    async def test_dry_run_leaves_trade_open_and_unexited(
        self, tmp_path, settings_factory
    ):
        """DRY_RUN=True records the observation but never sets exit fields."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_dry", cp_24h_pct=-1.0
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason, exit_price FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        status, exit_reason, exit_price = await cur.fetchone()
        assert status == "open", f"dry-run must not close; got {status}"
        assert exit_reason is None
        assert exit_price is None
        assert len(await _time_death_rows(db, trade_id)) == 1
        await db.close()


class TestFireOncePerTrade:
    @pytest.mark.asyncio
    async def test_would_fire_recorded_at_most_once(self, tmp_path, settings_factory):
        """Two eval passes over the same still-flat trade record exactly one
        would-fire row (dedup via prior-row existence check)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_once", cp_24h_pct=-1.0
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)
        await evaluate_paper_trades(db, settings)

        assert len(await _time_death_rows(db, trade_id)) == 1
        await db.close()


class TestMasterKillSwitch:
    @pytest.mark.asyncio
    async def test_disabled_is_fully_inert(self, tmp_path, settings_factory):
        """ENABLED=False: no row written even when every fire condition is met."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_off", cp_24h_pct=-1.0
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=False,  # master off (default)
        )
        await evaluate_paper_trades(db, settings)

        assert await _time_death_rows(db, trade_id) == []
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        assert (await cur.fetchone())[0] == "open"
        await db.close()


class TestLiveCloseModeUnreachableUntilFlip:
    @pytest.mark.asyncio
    async def test_dry_run_false_closes_via_time_death(
        self, tmp_path, settings_factory
    ):
        """Pins the flip behavior: DRY_RUN=False closes the trade via the
        time_death reason (the path unreachable until a future flip PR)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_flat_trade(
            db, trader, token_id="tok_live", cp_24h_pct=-1.0
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=False,  # flip: real close
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        status, exit_reason = await cur.fetchone()
        assert status == "closed_time_death"
        assert exit_reason == "time_death"
        # the close path does NOT write a would-fire observation
        assert await _time_death_rows(db, trade_id) == []
        await db.close()


class TestPeakFadeRegression:
    @pytest.mark.asyncio
    async def test_peak_fade_path_untouched(self, tmp_path, settings_factory):
        """Regression pin: with time-death enabled, a peak >= 10 trade in a
        sustained fade still closes via the existing peak_fade lane, and no
        time_death observation is written."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        # peak 12%, checkpoints at 4% (< 0.7 * 12 = 8.4) -> peak_fade fires.
        # leg 1 filled + young so time_death cannot fire on its own.
        trade_id = await _open_flat_trade(
            db,
            trader,
            token_id="tok_pf",
            cp_24h_pct=4.0,
            cp_1h_pct=4.0,
            cp_6h_pct=4.0,
            peak_pct=12.0,
            leg_1_filled=True,
            elapsed_hours=5.0,
        )

        settings = settings_factory(
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
            PEAK_FADE_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        status, exit_reason = await cur.fetchone()
        assert status == "closed_peak_fade"
        assert exit_reason == "peak_fade"
        assert await _time_death_rows(db, trade_id) == []
        await db.close()


class TestMomentumDeathRegression:
    @pytest.mark.asyncio
    async def test_momentum_death_path_untouched(self, tmp_path, settings_factory):
        """Regression pin: with time-death enabled, a peak-in-band [5,10) fader
        still records its momentum_death observation, and time_death stays silent
        (leg 1 filled -> outside the sub-leg-1 mandate). Demonstrates the two
        dry-run lanes are mutually exclusive on one trade."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        # peak 6% (in [5,10) band), checkpoints at 2% -> momentum_death fires.
        trade_id = await _open_flat_trade(
            db,
            trader,
            token_id="tok_md",
            cp_24h_pct=2.0,
            cp_1h_pct=2.0,
            cp_6h_pct=2.0,
            peak_pct=6.0,
            leg_1_filled=True,
            elapsed_hours=5.0,
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=True,
            PAPER_TIME_DEATH_ENABLED=True,
            PAPER_TIME_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        assert len(await _momentum_death_rows(db, trade_id)) == 1
        assert await _time_death_rows(db, trade_id) == []
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        assert (await cur.fetchone())[0] == "open"
        await db.close()

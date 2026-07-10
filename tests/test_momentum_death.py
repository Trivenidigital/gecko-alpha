"""Momentum-death dry-run exit lane (BL-NEW-MOMENTUM-DEATH) tests.

Evidence base: tasks/findings_expired_lane_backtest_2026_07_10.md — 92/93
expired paper trades peaked BELOW the 10% PEAK_FADE arming floor, so the
peak_fade lane was structurally unreachable for them. This lane catches the
[PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT, PEAK_FADE_MIN_PEAK_PCT) band using the
same sustained-fade shape, DRY-RUN first (records would-fire, never closes).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.config import Settings

_REQUIRED = dict(
    TELEGRAM_BOT_TOKEN="t",
    TELEGRAM_CHAT_ID="c",
    ANTHROPIC_API_KEY="k",
    _env_file=None,
)


async def _open_faded_trade(
    db,
    trader,
    *,
    token_id: str,
    peak_pct: float,
    cp_pct: float,
    current_price: float = 1.01,
    entry_price: float = 1.0,
):
    """Open a BL-061 trade that has already faded: peak recorded at *peak_pct*,
    both 6h/24h checkpoints at *cp_pct*, both legs pre-filled + floor armed so
    the ladder elif chain falls through to the momentum-death / peak-fade gates
    without an early `continue`. Current price stays above entry so the floor
    exit does not pre-empt.
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
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "UPDATE paper_trades SET peak_price = ?, peak_pct = ?, "
        "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
        "remaining_qty = quantity * 0.5, "
        "checkpoint_6h_pct = ?, checkpoint_24h_pct = ? "
        "WHERE id = ?",
        (peak_price, peak_pct, now_iso, now_iso, cp_pct, cp_pct, trade_id),
    )
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, current_price, now_iso),
    )
    await db._conn.commit()
    return trade_id


async def _momentum_death_rows(db, trade_id):
    cur = await db._conn.execute(
        "SELECT decision, reason, source_module FROM trade_decision_events "
        "WHERE paper_trade_id = ? AND reason = 'momentum_death_would_fire'",
        (trade_id,),
    )
    return await cur.fetchall()


class TestConfigDefaults:
    def test_master_kill_switch_defaults_off(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_MOMENTUM_DEATH_ENABLED is False

    def test_default_min_peak_pct_is_5(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT == 5.0

    def test_dry_run_defaults_on(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_MOMENTUM_DEATH_DRY_RUN is True

    def test_default_floor_below_peak_fade_floor(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT < s.PEAK_FADE_MIN_PEAK_PCT


class TestConfigValidators:
    def test_min_peak_pct_must_be_positive(self):
        with pytest.raises(ValueError, match="must be > 0"):
            Settings(**_REQUIRED, PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT=0.0)
        with pytest.raises(ValueError, match="must be > 0"):
            Settings(**_REQUIRED, PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT=-1.0)

    def test_min_peak_pct_not_hard_coupled_to_peak_fade(self):
        # A value ABOVE the peak_fade floor is still accepted (not hard-coupled).
        s = Settings(**_REQUIRED, PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT=15.0)
        assert s.PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT == 15.0


class TestBandLogic:
    @pytest.mark.asyncio
    async def test_peak_in_band_fires(self, tmp_path, settings_factory):
        """peak 6% (in [5, 10) band) with a sustained fade records one
        would-fire observation."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_band", peak_pct=6.0, cp_pct=2.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        rows = await _momentum_death_rows(db, trade_id)
        assert len(rows) == 1, "peak 6% in band should record one would-fire row"
        assert rows[0][0] == "observed"
        assert rows[0][1] == "momentum_death_would_fire"
        assert rows[0][2] == "scout.trading.evaluator"
        await db.close()

    @pytest.mark.asyncio
    async def test_peak_below_band_does_not_fire(self, tmp_path, settings_factory):
        """peak 4% (below MIN_PEAK_PCT 5) must not fire."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_low", peak_pct=4.0, cp_pct=1.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)

        assert await _momentum_death_rows(db, trade_id) == []
        await db.close()

    @pytest.mark.asyncio
    async def test_peak_above_band_is_peak_fade_territory(
        self, tmp_path, settings_factory
    ):
        """peak 12% (>= PEAK_FADE_MIN_PEAK_PCT 10) is peak_fade's territory;
        momentum-death must stay silent even with peak_fade disabled."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_high", peak_pct=12.0, cp_pct=4.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=True,
            PEAK_FADE_ENABLED=False,  # isolate: nothing else may close it
        )
        await evaluate_paper_trades(db, settings)

        assert await _momentum_death_rows(db, trade_id) == []
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        assert (await cur.fetchone())[0] == "open"
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
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_dry", peak_pct=6.0, cp_pct=2.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=True,
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
        # but the would-fire observation was still recorded
        assert len(await _momentum_death_rows(db, trade_id)) == 1
        await db.close()


class TestFireOncePerTrade:
    @pytest.mark.asyncio
    async def test_would_fire_recorded_at_most_once(self, tmp_path, settings_factory):
        """Two eval passes over the same still-faded trade record exactly one
        would-fire row (dedup via prior-row existence check)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_once", peak_pct=6.0, cp_pct=2.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=True,
        )
        await evaluate_paper_trades(db, settings)
        await evaluate_paper_trades(db, settings)

        assert len(await _momentum_death_rows(db, trade_id)) == 1
        await db.close()


class TestMasterKillSwitch:
    @pytest.mark.asyncio
    async def test_disabled_is_fully_inert(self, tmp_path, settings_factory):
        """ENABLED=False: no row written even when the band + fade conditions
        are all met."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_off", peak_pct=6.0, cp_pct=2.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=False,  # master off (default)
        )
        await evaluate_paper_trades(db, settings)

        assert await _momentum_death_rows(db, trade_id) == []
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        assert (await cur.fetchone())[0] == "open"
        await db.close()


class TestLiveCloseModeUnreachableUntilFlip:
    @pytest.mark.asyncio
    async def test_dry_run_false_closes_via_momentum_death(
        self, tmp_path, settings_factory
    ):
        """Pins the flip behavior: DRY_RUN=False closes the trade via the
        momentum_death reason (the path unreachable until a future flip PR)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_live", peak_pct=6.0, cp_pct=2.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=False,  # flip: real close
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        status, exit_reason = await cur.fetchone()
        assert status == "closed_momentum_death"
        assert exit_reason == "momentum_death"
        # the close path does NOT write a would-fire observation
        assert await _momentum_death_rows(db, trade_id) == []
        await db.close()


class TestPeakFadeRegression:
    @pytest.mark.asyncio
    async def test_peak_fade_path_untouched(self, tmp_path, settings_factory):
        """Regression pin: with momentum-death enabled, a peak >= 10 trade in a
        sustained fade still closes via the existing peak_fade lane, and no
        momentum_death observation is written."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        clear_cache_for_tests()

        trader = PaperTrader()
        # peak 12%, checkpoints at 4% (< 0.7 * 12 = 8.4) → peak_fade fires.
        trade_id = await _open_faded_trade(
            db, trader, token_id="tok_pf", peak_pct=12.0, cp_pct=4.0
        )

        settings = settings_factory(
            PAPER_MOMENTUM_DEATH_ENABLED=True,
            PAPER_MOMENTUM_DEATH_DRY_RUN=True,
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
        assert await _momentum_death_rows(db, trade_id) == []
        await db.close()

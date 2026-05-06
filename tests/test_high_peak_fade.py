"""High-peak fade gate (BL-NEW-HPF) tests."""

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


class TestConfigDefaults:
    def test_master_kill_switch_defaults_off(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_HIGH_PEAK_FADE_ENABLED is False

    def test_default_min_peak_pct_is_60(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT == 60.0

    def test_default_retrace_pct_is_15(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_HIGH_PEAK_FADE_RETRACE_PCT == 15.0

    def test_dry_run_defaults_on(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_HIGH_PEAK_FADE_DRY_RUN is True

    def test_per_signal_opt_in_defaults_on(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN is True


class TestConfigValidators:
    def test_min_peak_pct_must_exceed_moonshot_threshold(self):
        with pytest.raises(ValueError, match="must be > PAPER_MOONSHOT_THRESHOLD_PCT"):
            Settings(
                **_REQUIRED,
                PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT=30.0,  # below moonshot 40
            )

    def test_retrace_pct_must_be_in_open_unit_interval(self):
        with pytest.raises(ValueError, match="must be in \\(0, 100\\)"):
            Settings(**_REQUIRED, PAPER_HIGH_PEAK_FADE_RETRACE_PCT=0.0)
        with pytest.raises(ValueError, match="must be in \\(0, 100\\)"):
            Settings(**_REQUIRED, PAPER_HIGH_PEAK_FADE_RETRACE_PCT=100.0)

    def test_retrace_pct_must_be_tighter_than_moonshot_trail(self):
        with pytest.raises(
            ValueError, match="must be < PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT"
        ):
            Settings(
                **_REQUIRED,
                PAPER_HIGH_PEAK_FADE_RETRACE_PCT=35.0,  # >= moonshot 30
            )


class TestMigration:
    @pytest.mark.asyncio
    async def test_signal_params_has_high_peak_fade_enabled_column(self, tmp_path):
        from scout.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        cur = await db._conn.execute("PRAGMA table_info(signal_params)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "high_peak_fade_enabled" in cols
        await db.close()

    @pytest.mark.asyncio
    async def test_high_peak_fade_audit_table_exists(self, tmp_path):
        from scout.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='high_peak_fade_audit'"
        )
        row = await cur.fetchone()
        assert row is not None
        await db.close()

    @pytest.mark.asyncio
    async def test_high_peak_fade_audit_table_has_required_columns(self, tmp_path):
        from scout.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        cur = await db._conn.execute("PRAGMA table_info(high_peak_fade_audit)")
        cols = {row[1] for row in await cur.fetchall()}
        # MUST contain: id, trade_id, token_id, signal_type, peak_pct,
        # peak_price, current_price, threshold_pct, retrace_pct,
        # fired_at, dry_run (1=would_fire, 0=real_fire)
        for required in (
            "id",
            "trade_id",
            "token_id",
            "signal_type",
            "peak_pct",
            "peak_price",
            "current_price",
            "threshold_pct",
            "retrace_pct",
            "fired_at",
            "dry_run",
        ):
            assert required in cols, f"missing column: {required}"
        await db.close()

    @pytest.mark.asyncio
    async def test_high_peak_fade_audit_table_has_unique_constraint(self, tmp_path):
        """Verify UNIQUE(trade_id, threshold_pct, dry_run) prevents duplicate rows."""
        from scout.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # Temporarily disable FK to insert audit rows without a real paper_trades row
        await db._conn.execute("PRAGMA foreign_keys=OFF")
        # Insert a row
        await db._conn.execute(
            "INSERT INTO high_peak_fade_audit "
            "(trade_id, token_id, signal_type, peak_pct, peak_price, "
            " current_price, threshold_pct, retrace_pct, fired_at, dry_run) "
            "VALUES (1, 'tok', 'gainers_early', 80.0, 1.80, 1.50, 60.0, "
            "16.6667, '2026-05-05T00:00:00', 1)"
        )
        # Re-insert with same key — should INSERT OR IGNORE silently skip
        await db._conn.execute(
            "INSERT OR IGNORE INTO high_peak_fade_audit "
            "(trade_id, token_id, signal_type, peak_pct, peak_price, "
            " current_price, threshold_pct, retrace_pct, fired_at, dry_run) "
            "VALUES (1, 'tok', 'gainers_early', 85.0, 1.85, 1.55, 60.0, "
            "16.2162, '2026-05-05T00:30:00', 1)"
        )
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = 1"
        )
        assert (await cur.fetchone())[
            0
        ] == 1, "UNIQUE constraint should prevent duplicate"
        await db.close()

    @pytest.mark.asyncio
    async def test_existing_signal_params_rows_default_disabled(self, tmp_path):
        from scout.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # initialize should populate default rows for known signal types
        # (per existing _populate_default_signal_params); each new row's
        # high_peak_fade_enabled must default to 0
        cur = await db._conn.execute(
            "SELECT signal_type, high_peak_fade_enabled FROM signal_params"
        )
        rows = await cur.fetchall()
        assert len(rows) > 0
        for sig_type, opt_in in rows:
            assert opt_in == 0, f"{sig_type} should default to 0, got {opt_in}"
        await db.close()


class TestSignalParamsField:
    @pytest.mark.asyncio
    async def test_signal_params_has_high_peak_fade_enabled_field(
        self, tmp_path, settings_factory
    ):
        from scout.db import Database
        from scout.trading.params import params_for_signal

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
        sp = await params_for_signal(db, "first_signal", settings)
        assert hasattr(sp, "high_peak_fade_enabled")
        assert sp.high_peak_fade_enabled is False  # default 0 == False
        await db.close()

    @pytest.mark.asyncio
    async def test_signal_params_reads_opt_in_from_row(
        self, tmp_path, settings_factory
    ):
        from scout.db import Database
        from scout.trading.params import params_for_signal, clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()
        settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)
        sp = await params_for_signal(db, "gainers_early", settings)
        assert sp.high_peak_fade_enabled is True
        await db.close()


class TestEvaluatorGateDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_close_trade(self, tmp_path, settings_factory):
        """In dry-run mode, the gate logs would-fire but does NOT set close_reason."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # opt in gainers_early; widen trail_pct so the trailing-stop branch
        # does NOT pre-empt the new high-peak fade gate. With peak=$1.80 and
        # current=$1.50, a trail_pct of 20% gives trail_threshold=$1.44 (no
        # pre-empt). The default 12% would pre-empt at $1.584.
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, "
            "trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        # open a trade with a high peak (entry $1, peak $1.80 = +80%)
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok1",
            symbol="TOK",
            name="Tok",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        # simulate peak having been recorded at $1.80, both legs already
        # fired (so the elif chain falls past leg-1/leg-2 to the new gate
        # without triggering an early `continue`).
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        # current price at $1.50 (16.7% retrace from peak)
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok1", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        # Trade should still be open
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        row = await cur.fetchone()
        assert row[0] == "open", f"dry-run should not close; got status={row[0]}"

        # Audit-table should have a dry-run row
        cur = await db._conn.execute(
            "SELECT dry_run, peak_pct, current_price FROM high_peak_fade_audit "
            "WHERE trade_id = ?",
            (trade_id,),
        )
        audit = await cur.fetchone()
        assert audit is not None, "audit row should exist"
        assert audit[0] == 1, "dry_run flag should be 1"
        assert abs(audit[1] - 80.0) < 0.01
        assert abs(audit[2] - 1.50) < 0.01

        await db.close()


class TestConvictionLockDefer:
    @pytest.mark.asyncio
    async def test_gate_skips_when_conviction_locked(self, tmp_path, settings_factory):
        """When conviction_locked_at IS NOT NULL, gate must NOT fire even
        in live mode (DRY_RUN=False)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, "
            "trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok2",
            symbol="TOK2",
            name="Tok2",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        # high peak + conviction locked; both legs pre-filled so elif chain
        # doesn't pre-empt with a leg-2 continue
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5, "
            "conviction_locked_at = ?, conviction_locked_stack = 3 "
            "WHERE id = ?",
            (now_iso, now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok2", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=False,  # live mode
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        # Trade must remain open AND no audit row written.
        cur = await db._conn.execute(
            "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        assert (await cur.fetchone())[0] == "open"

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()


class TestPerSignalOptIn:
    @pytest.mark.asyncio
    async def test_gate_skips_when_signal_not_opted_in(
        self, tmp_path, settings_factory
    ):
        """When PER_SIGNAL_OPT_IN=True and signal_params.high_peak_fade_enabled=0,
        gate must NOT fire."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # gainers_early NOT opted in (default 0); widen trail_pct to prevent
        # trailing-stop pre-emption (so the HPF gate is the only candidate)
        await db._conn.execute(
            "UPDATE signal_params SET trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok3",
            symbol="TOK3",
            name="Tok3",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok3", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()

    @pytest.mark.asyncio
    async def test_per_signal_opt_in_off_bypasses_signal_check(
        self, tmp_path, settings_factory
    ):
        """When PER_SIGNAL_OPT_IN=False, signal_params.high_peak_fade_enabled is
        bypassed — gate fires regardless of per-signal flag (operator override path)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        # gainers_early NOT opted in (default 0 = high_peak_fade_enabled off)
        # widen trail_pct so trailing-stop does NOT pre-empt the HPF gate
        await db._conn.execute(
            "UPDATE signal_params SET trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok_bypass",
            symbol="BYPASS",
            name="Bypass",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok_bypass", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        # PER_SIGNAL_OPT_IN=False → bypass signal-level check; gate fires
        # even though high_peak_fade_enabled=0 in signal_params
        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN=False,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[
            0
        ] == 1, (
            "audit row should exist when PER_SIGNAL_OPT_IN=False bypasses signal check"
        )
        await db.close()


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_null_peak_pct_does_not_fire(self, tmp_path, settings_factory):
        """Trade that never went into profit (peak_pct IS NULL) cannot trigger gate."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok_null_peak",
            symbol="NULLPK",
            name="NullPeak",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        # Do NOT update peak_price / peak_pct — they remain NULL
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET floor_armed = 1, "
            "leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok_null_peak", 0.90, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[
            0
        ] == 0, "gate skipped via peak_pct is not None guard"
        await db.close()

    @pytest.mark.asyncio
    async def test_null_peak_price_does_not_fire(self, tmp_path, settings_factory):
        """Trade with peak_price IS NULL cannot trigger gate (defensive guard)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok_null_price",
            symbol="NULLPR",
            name="NullPrice",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        # Set peak_pct to trigger threshold but peak_price to NULL (schema allows NULL)
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_pct = 80.0, peak_price = NULL, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok_null_price", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[
            0
        ] == 0, "gate skipped via peak_price is not None guard"
        await db.close()


class TestCascadeOrdering:
    @pytest.mark.asyncio
    async def test_trailing_stop_pre_empts_high_peak_fade(
        self, tmp_path, settings_factory
    ):
        """When current_price is below the moonshot trail threshold, the
        trailing_stop branch fires first; gate should NOT also fire."""
        # Setup: peak 80%, moonshot trail 30% from peak → threshold=$1.26.
        # current=$1.17 is 35% retrace from peak — below moonshot 30% trail.
        # That triggers trailing_stop/closed_moonshot_trail. The HPF gate at
        # 15% retrace would also be eligible, but trailing_stop fires first
        # and sets close_reason, so HPF's `close_reason is None` guard blocks it.
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, "
            "trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok4",
            symbol="TOK4",
            name="Tok4",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        # peak $1.80, current $1.17 = 35% retrace from peak (below moonshot 30% trail)
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5, moonshot_armed_at = ? "
            "WHERE id = ?",
            (now_iso, now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok4", 1.17, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=False,
            PAPER_MOONSHOT_ENABLED=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cur.fetchone()
        # Should close via moonshot trail, NOT high_peak_fade
        assert row[0] in ("closed_trailing_stop", "closed_moonshot_trail")
        assert row[1] == "trailing_stop"
        await db.close()

    @pytest.mark.asyncio
    async def test_high_peak_fade_pre_empts_bl062_peak_fade(
        self, tmp_path, settings_factory
    ):
        """When BOTH high_peak_fade and BL-062 peak_fade conditions are met
        on the same pass, high_peak_fade fires FIRST (it's ordered earlier)."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, "
            "trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok5",
            symbol="TOK5",
            name="Tok5",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        # peak 80%, current at 17% retrace = $1.49 (HPF threshold: 15% → $1.53)
        # cp_6h_pct and cp_24h_pct both at 40%, below peak * 0.7 = 56%
        # → BL-062 conditions also met; HPF must win because it runs first.
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5, "
            "checkpoint_6h_pct = 40.0, checkpoint_24h_pct = 40.0 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok5", 1.49, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=False,
            PEAK_FADE_ENABLED=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cur.fetchone()
        assert row[0] == "closed_high_peak_fade"
        assert row[1] == "high_peak_fade"
        await db.close()

    @pytest.mark.asyncio
    async def test_below_60_peak_does_not_fire(self, tmp_path, settings_factory):
        """Below MIN_PEAK_PCT (60), gate stays silent; existing trail handles it."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, "
            "trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok6",
            symbol="TOK6",
            name="Tok6",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        # peak 50% (BELOW threshold 60); current=$1.40 = ~7% retrace from peak $1.50
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.50, peak_pct = 50.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok6", 1.40, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=True,
            PAPER_HIGH_PEAK_FADE_DRY_RUN=True,
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()


class TestMasterKillSwitch:
    @pytest.mark.asyncio
    async def test_master_disabled_no_fire(self, tmp_path, settings_factory):
        """When PAPER_HIGH_PEAK_FADE_ENABLED=False (default), gate is dead
        regardless of all other conditions. Audit table stays empty."""
        from scout.db import Database
        from scout.trading.evaluator import evaluate_paper_trades
        from scout.trading.paper import PaperTrader
        from scout.trading.params import clear_cache_for_tests

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()
        await db._conn.execute(
            "UPDATE signal_params SET high_peak_fade_enabled = 1, "
            "trail_pct = 20.0 "
            "WHERE signal_type = 'gainers_early'"
        )
        await db._conn.commit()
        clear_cache_for_tests()

        trader = PaperTrader()
        trade_id = await trader.execute_buy(
            db=db,
            token_id="tok7",
            symbol="TOK7",
            name="Tok7",
            chain="solana",
            signal_type="gainers_early",
            signal_data={},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=200.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="gainers_early",
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET peak_price = 1.80, peak_pct = 80.0, "
            "floor_armed = 1, leg_1_filled_at = ?, leg_2_filled_at = ?, "
            "remaining_qty = quantity * 0.5 "
            "WHERE id = ?",
            (now_iso, now_iso, trade_id),
        )
        await db._conn.execute(
            "INSERT INTO price_cache (coin_id, current_price, updated_at) "
            "VALUES (?, ?, ?)",
            ("tok7", 1.50, datetime.now(timezone.utc).isoformat()),
        )
        await db._conn.commit()

        settings = settings_factory(
            PAPER_HIGH_PEAK_FADE_ENABLED=False,  # MASTER OFF
            SIGNAL_PARAMS_ENABLED=True,
        )
        await evaluate_paper_trades(db, settings)

        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM high_peak_fade_audit WHERE trade_id = ?",
            (trade_id,),
        )
        assert (await cur.fetchone())[0] == 0
        await db.close()

"""High-peak fade gate (BL-NEW-HPF) tests."""

from __future__ import annotations

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

    def test_default_min_peak_pct_is_75(self):
        s = Settings(**_REQUIRED)
        assert s.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT == 75.0

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
        # peak_price, current_price, fired_at, dry_run (1=would_fire, 0=real_fire)
        for required in (
            "id",
            "trade_id",
            "token_id",
            "signal_type",
            "peak_pct",
            "peak_price",
            "current_price",
            "fired_at",
            "dry_run",
        ):
            assert required in cols, f"missing column: {required}"
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

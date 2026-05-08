"""BL-NEW-LIVE-HYBRID M1 v2.1: operator-in-loop threshold gate tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.live.approval_thresholds import (
    NEW_VENUE_FILL_THRESHOLD,
    RATE_LIMIT_CAUTION_PCT,
    should_require_approval,
)

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings():
    from scout.config import Settings

    return Settings(_env_file=None, **_REQUIRED)


@pytest.mark.asyncio
async def test_new_venue_gate_fires_below_threshold(tmp_path):
    """No row in signal_venue_correction_count → fills=0 → gate fires."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    require, gate = await should_require_approval(
        db=db,
        settings=_settings(),
        signal_type="first_signal",
        venue="binance",
        size_usd=50.0,
    )
    assert require is True
    assert gate == "new_venue_gate"
    await db.close()


@pytest.mark.asyncio
async def test_new_venue_gate_clears_at_threshold(tmp_path):
    """consecutive_no_correction = 30 → new-venue gate clears."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        """INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES (?, ?, ?, ?)""",
        (
            "first_signal",
            "binance",
            NEW_VENUE_FILL_THRESHOLD,
            "2026-05-08T00:00:00+00:00",
        ),
    )
    await db._conn.commit()
    require, gate = await should_require_approval(
        db=db,
        settings=_settings(),
        signal_type="first_signal",
        venue="binance",
        size_usd=50.0,
    )
    assert require is False, f"expected clear; got gate={gate}"
    assert gate is None
    await db.close()


@pytest.mark.asyncio
async def test_trade_size_gate_fires_above_2x_median(tmp_path):
    """30 closed fills at $50 (median = 50). Size $150 (3×) → gate fires."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # paper_trades parent rows (live_trades.paper_trade_id is FK NOT NULL)
    for i in range(30):
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_price, sl_price,
                status, opened_at)
               VALUES (?, 'BTC', 'btc', 'ethereum', 'first_signal', '{}',
                       100, 50, 0.5, 120, 80, 'closed_tp',
                       '2026-05-08T00:00:00+00:00')""",
            (f"tok_{i}",),
        )
    cur = await db._conn.execute("SELECT id FROM paper_trades ORDER BY id")
    paper_ids = [row[0] for row in await cur.fetchall()]
    for i, paper_id in enumerate(paper_ids):
        await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, created_at)
               VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                       '50.0', 'closed_tp', ?)""",
            (paper_id, f"2026-05-{(i % 9) + 1:02d}T00:00:00+00:00"),
        )
    await db._conn.execute(
        """INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES (?, ?, ?, ?)""",
        ("first_signal", "binance", 30, "2026-05-08T00:00:00+00:00"),
    )
    await db._conn.commit()
    require, gate = await should_require_approval(
        db=db,
        settings=_settings(),
        signal_type="first_signal",
        venue="binance",
        size_usd=150.0,
    )
    assert require is True
    assert gate == "trade_size_gate"
    await db.close()


@pytest.mark.asyncio
async def test_venue_health_gate_fires_when_auth_failed(tmp_path):
    """auth_ok=0 in past 24h → health gate fires."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute("""INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES ('first_signal', 'binance', 30, '2026-05-08T00:00:00+00:00')""")
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO venue_health
           (venue, probe_at, rest_responsive, ws_connected,
            auth_ok, last_balance_fetch_ok)
           VALUES ('binance', ?, 1, 1, 0, 1)""",
        (now_iso,),
    )
    await db._conn.commit()
    require, gate = await should_require_approval(
        db=db,
        settings=_settings(),
        signal_type="first_signal",
        venue="binance",
        size_usd=50.0,
    )
    assert require is True
    assert gate == "venue_health_gate"
    await db.close()


@pytest.mark.asyncio
async def test_venue_health_gate_fires_when_rate_limit_caution(tmp_path):
    """rate_limit_headroom_pct = 20 (< 30) → health gate fires."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute("""INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES ('first_signal', 'binance', 30, '2026-05-08T00:00:00+00:00')""")
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO venue_health
           (venue, probe_at, rest_responsive, ws_connected,
            auth_ok, last_balance_fetch_ok, rate_limit_headroom_pct)
           VALUES ('binance', ?, 1, 1, 1, 1, 20.0)""",
        (now_iso,),
    )
    await db._conn.commit()
    require, gate = await should_require_approval(
        db=db,
        settings=_settings(),
        signal_type="first_signal",
        venue="binance",
        size_usd=50.0,
    )
    assert require is True
    assert gate == "venue_health_gate"
    await db.close()


@pytest.mark.asyncio
async def test_operator_flag_gate_fires_when_approval_required_set(tmp_path):
    """Operator-set approval_required override → operator_flag gate fires."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute("""INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES ('first_signal', 'binance', 30, '2026-05-08T00:00:00+00:00')""")
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO live_operator_overrides
           (override_type, venue, set_at, expires_at)
           VALUES ('approval_required', 'binance', ?, ?)""",
        (now_iso, expires),
    )
    await db._conn.commit()
    require, gate = await should_require_approval(
        db=db,
        settings=_settings(),
        signal_type="first_signal",
        venue="binance",
        size_usd=50.0,
    )
    assert require is True
    assert gate == "operator_flag"
    await db.close()


@pytest.mark.asyncio
async def test_all_gates_clear_returns_false_none(tmp_path):
    """No gate fires → returns (False, None) — autonomous execution."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute("""INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES ('first_signal', 'binance', 30, '2026-05-08T00:00:00+00:00')""")
    # Healthy probe in past 24h
    now_iso = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO venue_health
           (venue, probe_at, rest_responsive, ws_connected,
            auth_ok, last_balance_fetch_ok, rate_limit_headroom_pct)
           VALUES ('binance', ?, 1, 1, 1, 1, 80.0)""",
        (now_iso,),
    )
    await db._conn.commit()
    require, gate = await should_require_approval(
        db=db,
        settings=_settings(),
        signal_type="first_signal",
        venue="binance",
        size_usd=50.0,
    )
    assert require is False
    assert gate is None
    await db.close()

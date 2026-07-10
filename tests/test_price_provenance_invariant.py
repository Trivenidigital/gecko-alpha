"""Phase 6 slices 2+3 — price-source invariant + exit provenance + stale-onset exit.

Invariant under test: a position cannot be OPENED without a registered
price source; a close cannot be RECORDED without provenance.

Builds on GA-01 (#404), which added the fail-closed dispatch gate + expiry
alerts + stats exclusion. This slice makes the GA-01 class ("caller passes
a non-market price") unrepresentable-unlabeled:

  (A) paper_trades.price_source stamped at open ('cg_lane' |
      'price_cache_row'); migration backfills 'legacy'. Belt and
      suspenders: the engine gate AND a Pydantic app-boundary model
      (PaperTradeOpen) both refuse unresolvable sources.
  (B) paper_trades.exit_provenance stamped at every close
      ('market' | 'stale_snapshot' | 'entry_fallback').
  (C) stale-onset exit: price feed stopped > STALE_ONSET_EXIT_HOURS →
      exit NOW at the last-good cached price with mark provenance
      (stale_age_seconds_at_exit / last_good_price_at / liquidity_at_exit)
      + §12b operator alert.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
import structlog

from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading.evaluator import evaluate_paper_trades
from scout.trading.paper import PaperTrader

DEX_TOKEN = "dex:solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_seq = [0]


@pytest.fixture(autouse=True)
def _wipe_params_cache():
    from scout.trading.params import clear_cache_for_tests

    _seq[0] = 0
    clear_cache_for_tests()
    yield
    clear_cache_for_tests()


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def engine_settings(settings_factory):
    return settings_factory(
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_MAX_EXPOSURE_USD=1_000_000,
    )


def _install_fake_alerter(monkeypatch, capture: list, *, raise_on_send=False):
    """Replace ``scout.alerter`` for local-import call sites (see
    tests/test_unpriceable_position_safety.py for the two-pronged rationale)."""
    import scout

    async def _capture_send(text, session, settings, **kwargs):
        if raise_on_send:
            raise RuntimeError("telegram unavailable")
        capture.append({"text": text, **kwargs})

    fake = types.ModuleType("scout.alerter")
    fake.send_telegram_message = _capture_send
    monkeypatch.setitem(sys.modules, "scout.alerter", fake)
    monkeypatch.setattr(scout, "alerter", fake, raising=False)


async def _seed_price_cache(db, coin_id, price, age_seconds=0):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d,
            market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, ts.isoformat()),
    )
    await db._conn.commit()
    return ts.isoformat()


async def _open_trade(
    db,
    *,
    token_id: str,
    opened_hours_ago: float = 0.0,
    sl_pct: float = 10.0,
    price_source: str | None = None,
) -> int:
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol="TOK",
        name="Token",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=1.0,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=sl_pct,
        slippage_bps=0,
        signal_combo="volume_spike",
        price_source=price_source,
    )
    assert trade_id is not None
    if opened_hours_ago:
        backdated = (
            datetime.now(timezone.utc) - timedelta(hours=opened_hours_ago)
        ).isoformat()
        await db._conn.execute(
            "UPDATE paper_trades SET opened_at = ?, created_at = ? WHERE id = ?",
            (backdated, backdated, trade_id),
        )
        await db._conn.commit()
    return trade_id


# ---------------------------------------------------------------------------
# Registry (scout.price_sources)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token_id", "has_row", "expected"),
    [
        ("bitcoin", False, "cg_lane"),
        ("bitcoin", True, "cg_lane"),  # CG shape wins over cache-row
        (DEX_TOKEN, True, "price_cache_row"),
        (DEX_TOKEN, False, None),
        ("0x1234567890abcdef1234567890abcdef12345678", False, None),
        (None, False, None),
        ("", True, None),  # empty id can't be priced by any lane
    ],
)
def test_resolve_price_source(token_id, has_row, expected):
    from scout.price_sources import resolve_price_source

    assert resolve_price_source(token_id, has_row) == expected


def test_registered_price_sources_exclude_legacy():
    """'legacy' is a migration-only label — it must NOT be openable."""
    from scout.price_sources import (
        PRICE_SOURCE_LEGACY,
        REGISTERED_PRICE_SOURCES,
    )

    assert PRICE_SOURCE_LEGACY not in REGISTERED_PRICE_SOURCES
    assert REGISTERED_PRICE_SOURCES == {"cg_lane", "price_cache_row"}


def test_exit_provenances_registry():
    from scout.price_sources import EXIT_PROVENANCES

    assert EXIT_PROVENANCES == {
        "market",
        "stale_snapshot",
        "entry_fallback",
        "stop_gap_model",
    }


# ---------------------------------------------------------------------------
# (A) App-boundary model — PaperTradeOpen
# ---------------------------------------------------------------------------


def _open_model_kwargs(**overrides):
    base = dict(
        token_id="bitcoin",
        signal_type="volume_spike",
        signal_combo="volume_spike",
        price_source="cg_lane",
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("source", ["cg_lane", "price_cache_row"])
def test_paper_trade_open_model_accepts_registered_sources(source):
    from scout.trading.models import PaperTradeOpen

    m = PaperTradeOpen(**_open_model_kwargs(price_source=source))
    assert m.price_source == source


@pytest.mark.parametrize("source", [None, "legacy", "bogus", ""])
def test_paper_trade_open_model_rejects_unregistered_sources(source):
    from pydantic import ValidationError

    from scout.trading.models import PaperTradeOpen

    with pytest.raises(ValidationError):
        PaperTradeOpen(**_open_model_kwargs(price_source=source))


# ---------------------------------------------------------------------------
# (A) Open boundary — price_source stamped at open
# ---------------------------------------------------------------------------


async def _fetch_price_source(db, trade_id: int) -> str | None:
    cur = await db._conn.execute(
        "SELECT price_source FROM paper_trades WHERE id = ?", (trade_id,)
    )
    return (await cur.fetchone())[0]


async def test_engine_open_stamps_cg_lane(db, engine_settings, monkeypatch):
    _install_fake_alerter(monkeypatch, [])
    engine = TradingEngine(mode="paper", db=db, settings=engine_settings)
    trade_id = await engine.open_trade(
        token_id="trending-coin",
        symbol="TREND",
        name="TrendCoin",
        chain="coingecko",
        signal_type="trending_catch",
        signal_data={"source": "trending_snapshot"},
        entry_price=0.0042,
        signal_combo="trending_catch",
    )
    assert trade_id is not None
    assert await _fetch_price_source(db, trade_id) == "cg_lane"


async def test_engine_open_stamps_price_cache_row(db, engine_settings, monkeypatch):
    """Non-CG-shaped token WITH a price_cache row → 'price_cache_row'."""
    _install_fake_alerter(monkeypatch, [])
    await _seed_price_cache(db, DEX_TOKEN, 0.5, age_seconds=60)
    engine = TradingEngine(mode="paper", db=db, settings=engine_settings)
    trade_id = await engine.open_trade(
        token_id=DEX_TOKEN,
        symbol="USDC",
        name="Dex Fallback",
        chain="solana",
        signal_type="volume_spike",
        signal_data={},
        entry_price=0.5,
        signal_combo="volume_spike",
    )
    assert trade_id is not None
    assert await _fetch_price_source(db, trade_id) == "price_cache_row"


async def test_unresolvable_open_blocked_by_gate(db, engine_settings):
    """Gate path (flag on, the default): blocked with a decision event."""
    engine = TradingEngine(mode="paper", db=db, settings=engine_settings)
    trade_id = await engine.open_trade(
        token_id=DEX_TOKEN,
        symbol="USDC",
        name="Dex Fallback",
        chain="solana",
        signal_type="volume_spike",
        signal_data={},
        entry_price=0.5,
        signal_combo="volume_spike",
    )
    assert trade_id is None
    cur = await db._conn.execute(
        """SELECT decision, reason FROM trade_decision_events
           WHERE token_id = ? ORDER BY id DESC LIMIT 1""",
        (DEX_TOKEN,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert (row[0], row[1]) == ("blocked", "unpriceable_token_id")


async def test_unresolvable_open_blocked_by_model_even_with_gate_off(
    db, settings_factory, monkeypatch
):
    """Belt and suspenders: even with the GA-01 gate flag OFF, the
    app-boundary model refuses to open without a registered price source.
    The unresolvable-open state is unrepresentable, not merely gated."""
    _install_fake_alerter(monkeypatch, [])
    settings = settings_factory(
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_MAX_EXPOSURE_USD=1_000_000,
        PAPER_REQUIRE_PRICEABLE_TOKEN_ID=False,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    with structlog.testing.capture_logs() as log_events:
        trade_id = await engine.open_trade(
            token_id=DEX_TOKEN,
            symbol="USDC",
            name="Dex Fallback",
            chain="solana",
            signal_type="volume_spike",
            signal_data={},
            entry_price=0.5,
            signal_combo="volume_spike",
        )
    assert trade_id is None
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (DEX_TOKEN,)
    )
    assert (await cur.fetchone())[0] == 0
    events = {e["event"] for e in log_events}
    assert "paper_trade_rejected_unregistered_price_source" in events


async def test_direct_execute_buy_refuses_unresolvable_source(db):
    """PaperTrader.execute_buy is itself a hard boundary — callers that
    bypass the engine still cannot open an unpriceable position."""
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id=DEX_TOKEN,
        symbol="USDC",
        name="Dex Fallback",
        chain="solana",
        signal_type="volume_spike",
        signal_data={},
        current_price=0.5,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="volume_spike",
    )
    assert trade_id is None
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (DEX_TOKEN,)
    )
    assert (await cur.fetchone())[0] == 0


async def test_direct_execute_buy_self_resolves_source(db):
    """Callers that don't pass price_source get it resolved from the
    registry (CG-shaped → cg_lane; cache-row → price_cache_row)."""
    trade_id = await _open_trade(db, token_id="bitcoin")
    assert await _fetch_price_source(db, trade_id) == "cg_lane"

    await _seed_price_cache(db, DEX_TOKEN, 0.5)
    trade_id2 = await _open_trade(db, token_id=DEX_TOKEN)
    assert await _fetch_price_source(db, trade_id2) == "price_cache_row"


# ---------------------------------------------------------------------------
# (B) Close boundary — exit_provenance on every close
# ---------------------------------------------------------------------------


async def _fetch_close(db, trade_id: int):
    cur = await db._conn.execute(
        "SELECT status, exit_reason, exit_provenance FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    return await cur.fetchone()


async def test_market_close_writes_market_provenance(db, settings_factory):
    """Normal evaluator exit (stop loss on fresh price) → 'market'."""
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    trade_id = await _open_trade(db, token_id="sl-coin", opened_hours_ago=2)
    await _seed_price_cache(db, "sl-coin", 0.5)  # far below sl_price=0.9

    await evaluate_paper_trades(db, settings)

    status, exit_reason, prov = await _fetch_close(db, trade_id)
    assert status == "closed_sl"
    assert exit_reason == "stop_loss"
    assert prov == "market"


async def test_no_price_expiry_writes_entry_fallback(db, settings_factory, monkeypatch):
    """expired_stale_no_price close → 'entry_fallback'."""
    _install_fake_alerter(monkeypatch, [])
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    # Open while a price_cache row exists (invariant), then simulate the
    # token dropping from price_cache entirely (the prod zombie shape).
    await _seed_price_cache(db, DEX_TOKEN, 0.5)
    trade_id = await _open_trade(db, token_id=DEX_TOKEN, opened_hours_ago=72)
    await db._conn.execute("DELETE FROM price_cache WHERE coin_id = ?", (DEX_TOKEN,))
    await db._conn.commit()

    await evaluate_paper_trades(db, settings, session=object())

    status, exit_reason, prov = await _fetch_close(db, trade_id)
    assert status == "closed_expired"
    assert exit_reason == "expired_stale_no_price"
    assert prov == "entry_fallback"


async def test_stale_expiry_writes_stale_snapshot(db, settings_factory, monkeypatch):
    """expired_stale_price close → 'stale_snapshot'."""
    _install_fake_alerter(monkeypatch, [])
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_trade(db, token_id="stale-zombie", opened_hours_ago=72)
    await _seed_price_cache(db, "stale-zombie", 0.6, age_seconds=24 * 3600)

    await evaluate_paper_trades(db, settings, session=object())

    status, exit_reason, prov = await _fetch_close(db, trade_id)
    assert status == "closed_expired"
    assert exit_reason == "expired_stale_price"
    assert prov == "stale_snapshot"


async def test_execute_sell_rejects_unregistered_provenance(db):
    trade_id = await _open_trade(db, token_id="bad-prov-coin")
    trader = PaperTrader()
    with pytest.raises(ValueError):
        await trader.execute_sell(
            db=db,
            trade_id=trade_id,
            current_price=1.0,
            reason="manual",
            price_provenance="vibes",
        )
    # Trade untouched by the rejected call.
    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == "open"


async def test_execute_partial_sell_rejects_unregistered_provenance(db):
    trade_id = await _open_trade(db, token_id="bad-prov-leg")
    trader = PaperTrader()
    with pytest.raises(ValueError):
        await trader.execute_partial_sell(
            db=db,
            trade_id=trade_id,
            leg=1,
            sell_qty_frac=0.3,
            current_price=1.3,
            price_provenance="vibes",
        )


async def test_execute_sell_default_provenance_is_market(db):
    """Callers that don't pass price_provenance record 'market'."""
    trade_id = await _open_trade(db, token_id="default-prov-coin")
    trader = PaperTrader()
    closed = await trader.execute_sell(
        db=db, trade_id=trade_id, current_price=1.1, reason="manual"
    )
    assert closed is True
    _, _, prov = await _fetch_close(db, trade_id)
    assert prov == "market"


# ---------------------------------------------------------------------------
# (C) Stale-onset exit
# ---------------------------------------------------------------------------


async def _seed_candidate_liquidity(db, token_id, liquidity_usd):
    await db._conn.execute(
        """INSERT OR REPLACE INTO candidates
           (contract_address, chain, token_name, ticker,
            liquidity_usd, first_seen_at)
           VALUES (?, 'coingecko', 'Token', 'TOK', ?, ?)""",
        (token_id, liquidity_usd, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def test_stale_onset_exit_fires_with_mark_provenance_and_alert(
    db, settings_factory, monkeypatch
):
    """Price feed stopped > STALE_ONSET_EXIT_HOURS (trade NOT at
    max_duration) → exit NOW at last-good cached price, mark provenance
    recorded, §12b alert fired."""
    captured: list[dict] = []
    _install_fake_alerter(monkeypatch, captured)
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    assert settings.STALE_ONSET_EXIT_HOURS == 6.0  # operator-approved default

    trade_id = await _open_trade(db, token_id="onset-coin", opened_hours_ago=12)
    last_good_ts = await _seed_price_cache(db, "onset-coin", 1.4, age_seconds=7 * 3600)
    await _seed_candidate_liquidity(db, "onset-coin", 123_456.0)

    with structlog.testing.capture_logs() as log_events:
        await evaluate_paper_trades(db, settings, session=object())

    cur = await db._conn.execute(
        """SELECT status, exit_reason, exit_provenance, exit_price,
                  stale_age_seconds_at_exit, last_good_price_at,
                  liquidity_at_exit
           FROM paper_trades WHERE id = ?""",
        (trade_id,),
    )
    (
        status,
        exit_reason,
        prov,
        exit_price,
        stale_age,
        last_good_at,
        liq,
    ) = await cur.fetchone()
    assert status == "closed_stale_onset"
    assert exit_reason == "stale_onset_exit"
    assert prov == "stale_snapshot"
    assert exit_price == pytest.approx(1.4)  # slippage_bps=0: exact mark
    assert stale_age == pytest.approx(7 * 3600, abs=60)
    assert last_good_at == last_good_ts
    assert liq == pytest.approx(123_456.0)

    assert len(captured) == 1
    payload = captured[0]
    assert payload.get("parse_mode") is None
    assert payload.get("source") == "trade_expiry_anomaly"
    assert "stale_onset_exit" in payload["text"]
    events = {e["event"] for e in log_events}
    assert "trade_expiry_anomaly_alert_dispatched" in events
    assert "trade_expiry_anomaly_alert_delivered" in events
    assert "trade_eval_stale_onset_exit" in events


async def test_stale_onset_liquidity_null_when_never_observed(
    db, settings_factory, monkeypatch
):
    """No candidates row → liquidity_at_exit NULL = 'could not verify
    exitability' (leaving the tracked universe often means liquidity
    death; the ledger must represent that distinctly)."""
    _install_fake_alerter(monkeypatch, [])
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    trade_id = await _open_trade(db, token_id="onset-noliq", opened_hours_ago=12)
    await _seed_price_cache(db, "onset-noliq", 0.8, age_seconds=8 * 3600)

    await evaluate_paper_trades(db, settings, session=object())

    cur = await db._conn.execute(
        "SELECT status, exit_provenance, liquidity_at_exit "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    status, prov, liq = await cur.fetchone()
    assert status == "closed_stale_onset"
    assert prov == "stale_snapshot"
    assert liq is None


async def test_stale_onset_does_not_fire_on_fresh_price(db, settings_factory):
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    trade_id = await _open_trade(db, token_id="fresh-coin", opened_hours_ago=12)
    await _seed_price_cache(db, "fresh-coin", 1.0, age_seconds=60)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == "open"


async def test_stale_onset_does_not_fire_below_threshold(db, settings_factory):
    """Stale (>1h, evaluator skips) but below the onset threshold → the
    pre-existing skip behavior is preserved."""
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    trade_id = await _open_trade(db, token_id="mild-stale", opened_hours_ago=12)
    await _seed_price_cache(db, "mild-stale", 1.0, age_seconds=2 * 3600)

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == "open"


async def test_stale_onset_cannot_fire_without_cache_row(db, settings_factory):
    """No price_cache row at all → no mark to exit at. The no-price branch
    is unchanged: trade stays open within max_duration."""
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=168)
    await _seed_price_cache(db, "vanished-coin", 1.0)
    trade_id = await _open_trade(db, token_id="vanished-coin", opened_hours_ago=12)
    await db._conn.execute(
        "DELETE FROM price_cache WHERE coin_id = ?", ("vanished-coin",)
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == "open"


async def test_past_max_duration_takes_expiry_path_not_onset(
    db, settings_factory, monkeypatch
):
    """At/after max_duration the pre-existing expired_stale_price close
    wins — stale-onset only covers the NOT-at-max-duration window."""
    _install_fake_alerter(monkeypatch, [])
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_trade(db, token_id="old-stale", opened_hours_ago=72)
    await _seed_price_cache(db, "old-stale", 0.6, age_seconds=10 * 3600)

    await evaluate_paper_trades(db, settings, session=object())

    status, exit_reason, prov = await _fetch_close(db, trade_id)
    assert status == "closed_expired"
    assert exit_reason == "expired_stale_price"
    assert prov == "stale_snapshot"


async def test_stale_onset_threshold_configurable(db, settings_factory, monkeypatch):
    _install_fake_alerter(monkeypatch, [])
    settings = settings_factory(
        PAPER_MAX_DURATION_HOURS=168, STALE_ONSET_EXIT_HOURS=1.0
    )
    trade_id = await _open_trade(db, token_id="onset-fast", opened_hours_ago=12)
    await _seed_price_cache(db, "onset-fast", 1.0, age_seconds=2 * 3600)

    await evaluate_paper_trades(db, settings, session=object())

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == "closed_stale_onset"


def test_stale_onset_hours_setting_floor(settings_factory):
    """ge=1: sub-hour onset thresholds are configuration errors."""
    with pytest.raises(Exception):
        settings_factory(STALE_ONSET_EXIT_HOURS=0.5)


# ---------------------------------------------------------------------------
# Migration backfill correctness
# ---------------------------------------------------------------------------


async def _insert_raw_trade(
    db,
    *,
    token_id: str,
    status: str,
    exit_reason: str | None,
    pnl_usd: float = 0.0,
    signal_type: str = "volume_spike",
    signal_combo: str | None = None,
) -> int:
    _seq[0] += 1
    seq = _seq[0]
    opened = datetime.now(timezone.utc) - timedelta(days=1, seconds=seq)
    closed = None if status == "open" else (opened + timedelta(hours=6)).isoformat()
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_combo,
            signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct,
            tp_price, sl_price, status, exit_price, exit_reason,
            pnl_usd, pnl_pct, opened_at, closed_at)
           VALUES (?, 'TOK', 'T', 'coingecko', ?, ?, '{}', 1.0, 100.0, 100.0,
                   20.0, 10.0, 1.2, 0.9, ?, 1.0, ?, ?, ?, ?, ?)""",
        (
            token_id,
            signal_type,
            signal_combo or signal_type,
            status,
            exit_reason,
            pnl_usd,
            pnl_usd,
            opened.isoformat(),
            closed,
        ),
    )
    return cur.lastrowid


async def _rerun_price_provenance_migration(db):
    await db._conn.execute(
        "DELETE FROM paper_migrations WHERE name = 'price_provenance_v1'"
    )
    await db._conn.execute("DELETE FROM schema_version WHERE version = 20260705")
    await db._conn.commit()
    await db._migrate_price_provenance_v1()


async def test_migration_backfills_synthetic_rows(db):
    """Backfill: price_source → 'legacy' for all pre-existing rows;
    exit_provenance keyed off exit_reason for closed rows; open rows keep
    exit_provenance NULL (stamped at close)."""
    a = await _insert_raw_trade(
        db, token_id="bf-a", status="closed_sl", exit_reason="stop_loss"
    )
    b = await _insert_raw_trade(
        db,
        token_id="bf-b",
        status="closed_expired",
        exit_reason="expired_stale_no_price",
    )
    c = await _insert_raw_trade(
        db,
        token_id="bf-c",
        status="closed_expired",
        exit_reason="expired_stale_price",
    )
    d = await _insert_raw_trade(db, token_id="bf-d", status="open", exit_reason=None)
    await db._conn.commit()

    await _rerun_price_provenance_migration(db)

    async def fetch(tid):
        cur = await db._conn.execute(
            "SELECT price_source, exit_provenance FROM paper_trades WHERE id = ?",
            (tid,),
        )
        return await cur.fetchone()

    assert tuple(await fetch(a)) == ("legacy", "market")
    assert tuple(await fetch(b)) == ("legacy", "entry_fallback")
    assert tuple(await fetch(c)) == ("legacy", "stale_snapshot")
    assert tuple(await fetch(d)) == ("legacy", None)


async def test_migration_is_idempotent(db):
    """Re-running the migration must not clobber writer-stamped values."""
    trade_id = await _open_trade(db, token_id="idem-coin")
    await _rerun_price_provenance_migration(db)
    await _rerun_price_provenance_migration(db)
    assert await _fetch_price_source(db, trade_id) == "cg_lane"


# ---------------------------------------------------------------------------
# Stats predicates — keyed on exit_provenance with exit_reason OR-fallback
# ---------------------------------------------------------------------------


async def _set_provenance(db, trade_id, provenance):
    await db._conn.execute(
        "UPDATE paper_trades SET exit_provenance = ? WHERE id = ?",
        (provenance, trade_id),
    )
    await db._conn.commit()


async def test_rolling_stats_excludes_entry_fallback_provenance(db):
    """Exclusion keys on exit_provenance — even when exit_reason differs."""
    from scout.trading.auto_suspend import _rolling_stats

    await _insert_raw_trade(
        db,
        token_id="rs-real",
        status="closed_sl",
        exit_reason="stop_loss",
        pnl_usd=-100.0,
    )
    tid = await _insert_raw_trade(
        db,
        token_id="rs-fab",
        status="closed_expired",
        exit_reason="expired",  # reason alone would NOT exclude
        pnl_usd=0.0,
    )
    await db._conn.commit()
    await _set_provenance(db, tid, "entry_fallback")

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    n, net, _ = await _rolling_stats(db._conn, "volume_spike", since)
    assert n == 1
    assert net == pytest.approx(-100.0)


async def test_rolling_stats_reason_or_fallback_still_excludes(db):
    """Legacy rows with NULL provenance but the GA-01 exit_reason remain
    excluded (OR-fallback for safety)."""
    from scout.trading.auto_suspend import _rolling_stats

    await _insert_raw_trade(
        db,
        token_id="rs-real2",
        status="closed_tp",
        exit_reason="take_profit",
        pnl_usd=50.0,
    )
    await _insert_raw_trade(
        db,
        token_id="rs-fab2",
        status="closed_expired",
        exit_reason="expired_stale_no_price",
        pnl_usd=0.0,
    )
    await db._conn.commit()

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    n, net, _ = await _rolling_stats(db._conn, "volume_spike", since)
    assert n == 1
    assert net == pytest.approx(50.0)


async def test_rolling_stats_counts_stale_onset_rows(db):
    """'stale_snapshot' provenance rows are excluded from NOTHING — they
    carry real-ish marks."""
    from scout.trading.auto_suspend import _rolling_stats

    tid = await _insert_raw_trade(
        db,
        token_id="rs-onset",
        status="closed_stale_onset",
        exit_reason="stale_onset_exit",
        pnl_usd=-30.0,
    )
    await db._conn.commit()
    await _set_provenance(db, tid, "stale_snapshot")

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    n, net, _ = await _rolling_stats(db._conn, "volume_spike", since)
    assert n == 1
    assert net == pytest.approx(-30.0)


async def test_calibrate_stats_exclude_entry_fallback_provenance(db):
    from scout.trading.calibrate import _stats_for_signal

    await _insert_raw_trade(
        db,
        token_id="cal-real",
        status="closed_tp",
        exit_reason="take_profit",
        pnl_usd=30.0,
    )
    tid = await _insert_raw_trade(
        db,
        token_id="cal-fab",
        status="closed_expired",
        exit_reason="expired",
        pnl_usd=0.0,
    )
    await db._conn.commit()
    await _set_provenance(db, tid, "entry_fallback")

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    stats = await _stats_for_signal(db._conn, "volume_spike", since)
    assert stats.n_trades == 1
    assert stats.win_rate_pct == pytest.approx(100.0)


async def test_combo_refresh_excludes_entry_fallback_provenance(db, settings_factory):
    from scout.trading.combo_refresh import refresh_combo

    await _insert_raw_trade(
        db,
        token_id="cmb-real",
        status="closed_sl",
        exit_reason="stop_loss",
        pnl_usd=-40.0,
    )
    tid = await _insert_raw_trade(
        db,
        token_id="cmb-fab",
        status="closed_expired",
        exit_reason="expired",
        pnl_usd=0.0,
    )
    await db._conn.commit()
    await _set_provenance(db, tid, "entry_fallback")

    ok = await refresh_combo(db, "volume_spike", settings_factory())
    assert ok is True
    cur = await db._conn.execute(
        "SELECT trades, total_pnl_usd FROM combo_performance "
        "WHERE combo_key = 'volume_spike' AND window = '30d'"
    )
    trades, total_pnl = await cur.fetchone()
    assert trades == 1
    assert total_pnl == pytest.approx(-40.0)


async def test_combo_refresh_counts_stale_onset_status(db, settings_factory):
    """closed_stale_onset is in CLOSED_COUNTABLE_STATUSES → combo rollups
    count it (excluded from nothing by default)."""
    from scout.trading.combo_refresh import refresh_combo
    from scout.trading.paper import CLOSED_COUNTABLE_STATUSES

    assert "closed_stale_onset" in CLOSED_COUNTABLE_STATUSES

    tid = await _insert_raw_trade(
        db,
        token_id="cmb-onset",
        status="closed_stale_onset",
        exit_reason="stale_onset_exit",
        pnl_usd=-15.0,
    )
    await db._conn.commit()
    await _set_provenance(db, tid, "stale_snapshot")

    ok = await refresh_combo(db, "volume_spike", settings_factory())
    assert ok is True
    cur = await db._conn.execute(
        "SELECT trades, total_pnl_usd FROM combo_performance "
        "WHERE combo_key = 'volume_spike' AND window = '30d'"
    )
    trades, total_pnl = await cur.fetchone()
    assert trades == 1
    assert total_pnl == pytest.approx(-15.0)

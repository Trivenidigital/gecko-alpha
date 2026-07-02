"""GA-01 unpriceable-position safety (P0).

Root cause under test: the TG-social resolver mints `dex:{chain}:{address}`
token_ids (scout/social/telegram/resolver.py) for DexScreener-fallback
tokens. NO price_cache writer serves that namespace, and dispatch passes
entry_price=token.price_usd, which bypasses the price_cache lookup in
TradingEngine.open_trade. The evaluator therefore NEVER resolves a price
for those trades; the only terminal state is expiry at entry_price with
slippage 0 → pnl_usd exactly 0 (exit_reason='expired_stale_no_price').
Prod evidence: 12/12 historical `dex:` closes were fabricated $0 at exactly
max_duration; those zeros dilute auto_suspend._rolling_stats.

Three fixes under test:
  (a) fail-closed dispatch gate (PAPER_REQUIRE_PRICEABLE_TOKEN_ID)
  (b) §12b operator alert on fabricated force-closes
  (c) fabricated rows excluded from auto-suspend / calibration /
      combo_performance stats
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
    """Replace ``scout.alerter`` for local-import call sites.

    Two-pronged patch (sys.modules + scout attribute) — same rationale as
    tests/test_signal_params_auto_suspend.py::_install_fake_alerter: the
    attribute path wins on Linux CI where scout.alerter is already loaded;
    the sys.modules path wins on Windows where a real import would crash
    on aiohttp's OpenSSL Applink load.
    """
    import scout  # parent package is safe; doesn't pull aiohttp

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


# ---------------------------------------------------------------------------
# Shared helper extraction (no duplication)
# ---------------------------------------------------------------------------


def test_is_cg_coin_id_is_shared_single_source():
    """The CG-id heuristic lives in scout.token_ids; held_position_prices
    re-exports the SAME object (no forked copy to drift)."""
    from scout.ingestion.held_position_prices import _is_cg_coin_id
    from scout.token_ids import is_cg_coin_id

    assert _is_cg_coin_id is is_cg_coin_id


@pytest.mark.parametrize(
    ("token_id", "expected"),
    [
        ("bitcoin", True),
        ("wrapped-staked-eth", True),
        (DEX_TOKEN, False),  # `:` namespace separator → not CG-shaped
        ("0x1234567890abcdef1234567890abcdef12345678", False),
        ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", False),
        (None, False),
        ("", False),
    ],
)
def test_is_cg_coin_id_heuristic_shared(token_id, expected):
    from scout.token_ids import is_cg_coin_id

    assert is_cg_coin_id(token_id) is expected


# ---------------------------------------------------------------------------
# (a) Dispatch gate — fail closed
# ---------------------------------------------------------------------------


async def test_dex_token_open_blocked_when_flag_on(db, engine_settings):
    """dex:-namespace token with caller-supplied entry_price is BLOCKED when
    PAPER_REQUIRE_PRICEABLE_TOKEN_ID is on (the fail-closed default)."""
    assert engine_settings.PAPER_REQUIRE_PRICEABLE_TOKEN_ID is True

    engine = TradingEngine(mode="paper", db=db, settings=engine_settings)
    trade_id = await engine.open_trade(
        token_id=DEX_TOKEN,
        symbol="USDC",
        name="Dex Fallback",
        chain="solana",
        signal_type="volume_spike",
        signal_data={},
        entry_price=0.5,  # the exact bypass that produced the prod zombies
        signal_combo="volume_spike",
    )
    assert trade_id is None

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (DEX_TOKEN,)
    )
    assert (await cur.fetchone())[0] == 0

    # Blocked-event symmetry with the other engine gates.
    cur = await db._conn.execute(
        """SELECT decision, reason FROM trade_decision_events
           WHERE token_id = ? ORDER BY id DESC LIMIT 1""",
        (DEX_TOKEN,),
    )
    row = await cur.fetchone()
    assert row is not None, "blocked open must emit a trade_decision event"
    assert row[0] == "blocked"
    assert row[1] == "unpriceable_token_id"


async def test_dex_token_open_still_blocked_when_flag_off(
    db, settings_factory, monkeypatch
):
    """Phase 6 slice 2 supersedes the GA-01 kill switch for unpriceable
    opens: with the gate flag OFF the engine gate stands down, but the
    PaperTradeOpen boundary model in execute_buy still refuses to open
    without a registered price source. The unpriceable-open state is
    unrepresentable, not merely gated. (Pre-slice-2, flag-off restored
    the pre-GA-01 open-proceeds behavior.)"""
    _install_fake_alerter(monkeypatch, [])
    settings = settings_factory(
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_MAX_EXPOSURE_USD=1_000_000,
        PAPER_REQUIRE_PRICEABLE_TOKEN_ID=False,
    )
    engine = TradingEngine(mode="paper", db=db, settings=settings)
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


async def test_dex_token_with_price_cache_row_is_refreshable(
    db, engine_settings, monkeypatch
):
    """A non-CG-shaped token_id that DOES have a price_cache row is
    refreshable → gate lets it through even with the flag on."""
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


async def test_cg_token_open_unaffected_by_gate(db, engine_settings, monkeypatch):
    """Regression: CG-shaped ids sail through the gate — with entry_price
    supplied AND with the price_cache fallback path."""
    _install_fake_alerter(monkeypatch, [])
    engine = TradingEngine(mode="paper", db=db, settings=engine_settings)

    # entry_price path, no price_cache row (trending/gainers pattern)
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

    # price_cache path
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    trade_id2 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        signal_combo="volume_spike",
    )
    assert trade_id2 is not None


# ---------------------------------------------------------------------------
# (b) Operator alert on fabricated close
# ---------------------------------------------------------------------------


async def _open_backdated_trade(db, *, token_id: str, opened_hours_ago: float) -> int:
    # Phase 6 slice 2: opens now require a registered price source, so the
    # zombie state must be constructed the way prod produced it — a writer
    # that served the token at open time and then stopped. Seed a
    # price_cache row for the open, then delete it (token drops from the
    # tracked universe entirely).
    await _seed_price_cache(db, token_id, 0.5, age_seconds=60)
    trader = PaperTrader()
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol="ZMB",
        name="Zombie",
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
    assert trade_id is not None
    await db._conn.execute("DELETE FROM price_cache WHERE coin_id = ?", (token_id,))
    backdated = (
        datetime.now(timezone.utc) - timedelta(hours=opened_hours_ago)
    ).isoformat()
    await db._conn.execute(
        "UPDATE paper_trades SET opened_at = ?, created_at = ? WHERE id = ?",
        (backdated, backdated, trade_id),
    )
    await db._conn.commit()
    return trade_id


async def test_fabricated_close_fires_exactly_one_alert(
    db, settings_factory, monkeypatch
):
    """expired_stale_no_price force-close fires ONE plain-text operator alert
    with parse_mode=None + dispatched/delivered structured logs (§12b)."""
    captured: list[dict] = []
    _install_fake_alerter(monkeypatch, captured)
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_backdated_trade(db, token_id=DEX_TOKEN, opened_hours_ago=72)

    with structlog.testing.capture_logs() as log_events:
        await evaluate_paper_trades(db, settings, session=object())

    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_expired"
    assert exit_reason == "expired_stale_no_price"

    assert (
        len(captured) == 1
    ), f"expected exactly 1 send_telegram_message call; got {len(captured)}"
    payload = captured[0]
    assert payload.get("parse_mode") is None, (
        "parse_mode MUST be None — token_ids/signal names contain characters "
        "Telegram MarkdownV1 silently mangles (§12b / Class-3)"
    )
    assert payload.get("source") == "trade_expiry_anomaly"
    assert str(trade_id) in payload["text"]
    assert DEX_TOKEN in payload["text"]
    assert "volume_spike" in payload["text"]
    assert "3.0" in payload["text"]  # 72h = 3.0 days held
    assert "UNRELIABLE" in payload["text"]

    events = {e["event"] for e in log_events}
    assert "trade_expiry_anomaly_alert_dispatched" in events
    assert "trade_expiry_anomaly_alert_delivered" in events


async def test_stale_price_close_fires_alert(db, settings_factory, monkeypatch):
    """expired_stale_price (stale-snapshot close) also alerts — the recorded
    exit price is a best-effort stale value, not a market fill."""
    captured: list[dict] = []
    _install_fake_alerter(monkeypatch, captured)
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_backdated_trade(
        db, token_id="stale-zombie", opened_hours_ago=72
    )
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("stale-zombie", 0.6, stale_ts),
    )
    await db._conn.commit()

    await evaluate_paper_trades(db, settings, session=object())

    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_expired"
    assert exit_reason == "expired_stale_price"
    assert len(captured) == 1
    assert captured[0].get("source") == "trade_expiry_anomaly"
    assert "expired_stale_price" in captured[0]["text"]


async def test_alert_failure_does_not_break_close(db, settings_factory, monkeypatch):
    """Telegram failure is a side effect — the close must still commit."""
    _install_fake_alerter(monkeypatch, [], raise_on_send=True)
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_backdated_trade(db, token_id=DEX_TOKEN, opened_hours_ago=72)

    with structlog.testing.capture_logs() as log_events:
        await evaluate_paper_trades(db, settings, session=object())

    cur = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,)
    )
    status, exit_reason = await cur.fetchone()
    assert status == "closed_expired"
    assert exit_reason == "expired_stale_no_price"

    events = {e["event"] for e in log_events}
    assert "trade_expiry_anomaly_alert_failed" in events
    assert "trade_expiry_anomaly_alert_delivered" not in events


async def test_no_session_close_still_works(db, settings_factory):
    """Back-compat: callers that don't pass a session still get the close;
    the alert is skipped (logged), never raises."""
    settings = settings_factory(PAPER_MAX_DURATION_HOURS=24)
    trade_id = await _open_backdated_trade(db, token_id=DEX_TOKEN, opened_hours_ago=72)

    await evaluate_paper_trades(db, settings)  # no session kwarg

    cur = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == "closed_expired"


# ---------------------------------------------------------------------------
# (c) Stats integrity — fabricated rows excluded
# ---------------------------------------------------------------------------


async def _insert_closed_trade(
    db,
    *,
    signal_type: str,
    pnl_usd: float,
    exit_reason: str = "stop_loss",
    status: str = "closed_sl",
    signal_combo: str | None = None,
    token_id: str | None = None,
    days_ago: int = 1,
):
    _seq[0] += 1
    seq = _seq[0]
    opened = datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=seq)
    closed = datetime.now(timezone.utc) - timedelta(
        days=days_ago, hours=-1, seconds=seq
    )
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_combo,
            signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct,
            tp_price, sl_price, status, exit_price, exit_reason,
            pnl_usd, pnl_pct, peak_pct, opened_at, closed_at)
           VALUES (?, 'TOK', 'T', 'coingecko', ?, ?, '{}', 1.0, 100.0, 100.0,
                   20.0, 15.0, 1.2, 0.85, ?, 1.0, ?, ?, ?, 5.0, ?, ?)""",
        (
            token_id or f"tok-{seq}",
            signal_type,
            signal_combo or signal_type,
            status,
            exit_reason,
            pnl_usd,
            pnl_usd,
            opened.isoformat(),
            closed.isoformat(),
        ),
    )


async def test_rolling_stats_excludes_fabricated_rows(db):
    """auto_suspend._rolling_stats must not count expired_stale_no_price
    rows — their pnl_usd=0 is fabricated and dilutes drawdown/net-pnl,
    making a bleeding signal look healthier (un-suspendable)."""
    from scout.trading.auto_suspend import _rolling_stats

    for _ in range(4):
        await _insert_closed_trade(db, signal_type="volume_spike", pnl_usd=-100.0)
    for _ in range(12):
        await _insert_closed_trade(
            db,
            signal_type="volume_spike",
            pnl_usd=0.0,
            exit_reason="expired_stale_no_price",
            status="closed_expired",
            token_id=None,
        )
    await db._conn.commit()

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    n, net, drawdown = await _rolling_stats(db._conn, "volume_spike", since)
    assert n == 4, f"fabricated rows must be excluded; got n={n}"
    assert net == pytest.approx(-400.0)
    assert drawdown == pytest.approx(-400.0)


async def test_rolling_stats_includes_real_closes(db):
    """Real closes — including genuine 'expired' and 'expired_stale_price'
    (stale-snapshot, still market-derived) — remain counted."""
    from scout.trading.auto_suspend import _rolling_stats

    await _insert_closed_trade(
        db,
        signal_type="volume_spike",
        pnl_usd=50.0,
        exit_reason="take_profit",
        status="closed_tp",
    )
    await _insert_closed_trade(
        db,
        signal_type="volume_spike",
        pnl_usd=-20.0,
        exit_reason="expired",
        status="closed_expired",
    )
    await _insert_closed_trade(
        db,
        signal_type="volume_spike",
        pnl_usd=-10.0,
        exit_reason="expired_stale_price",
        status="closed_expired",
    )
    await db._conn.commit()

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    n, net, _ = await _rolling_stats(db._conn, "volume_spike", since)
    assert n == 3
    assert net == pytest.approx(20.0)


async def test_calibrate_stats_exclude_fabricated_rows(db):
    """calibrate._stats_for_signal consumes pnl_usd of closed rows — the
    fabricated $0 rows would depress win-rate and inflate expired_pct."""
    from scout.trading.calibrate import _stats_for_signal

    for _ in range(2):
        await _insert_closed_trade(
            db,
            signal_type="volume_spike",
            pnl_usd=30.0,
            exit_reason="take_profit",
            status="closed_tp",
        )
    for _ in range(6):
        await _insert_closed_trade(
            db,
            signal_type="volume_spike",
            pnl_usd=0.0,
            exit_reason="expired_stale_no_price",
            status="closed_expired",
        )
    await db._conn.commit()

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    stats = await _stats_for_signal(db._conn, "volume_spike", since)
    assert stats.n_trades == 2
    assert stats.win_rate_pct == pytest.approx(100.0)
    assert stats.expired_pct == pytest.approx(0.0)


async def test_combo_refresh_excludes_fabricated_rows(db, settings_factory):
    """combo_performance rollups consume pnl_usd of closed rows — exclude."""
    from scout.trading.combo_refresh import refresh_combo

    await _insert_closed_trade(
        db,
        signal_type="volume_spike",
        pnl_usd=-40.0,
        signal_combo="volume_spike",
    )
    for _ in range(5):
        await _insert_closed_trade(
            db,
            signal_type="volume_spike",
            pnl_usd=0.0,
            exit_reason="expired_stale_no_price",
            status="closed_expired",
            signal_combo="volume_spike",
        )
    await db._conn.commit()

    settings = settings_factory()
    ok = await refresh_combo(db, "volume_spike", settings)
    assert ok is True

    cur = await db._conn.execute(
        "SELECT trades, total_pnl_usd FROM combo_performance "
        "WHERE combo_key = 'volume_spike' AND window = '30d'"
    )
    trades, total_pnl = await cur.fetchone()
    assert trades == 1, f"fabricated rows must be excluded; got trades={trades}"
    assert total_pnl == pytest.approx(-40.0)

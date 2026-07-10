"""ALR-01 alert-body-v2 + ALR-09 dashboard deep-link.

Golden-file/snapshot coverage of ``format_paper_trade_alert`` per
signal_type for the v2 card (entry / SL-in-price / invalidation /
liquidity / earliness / dashboard deep-link), plus wiring coverage in
``notify_paper_trade_opened`` (sl_pct from signal_params, lead_time from
paper_trades, liquidity from candidates, deep-link from settings).

Model: tests/test_tg_alert_dispatch.py. parse_mode=None is asserted by the
existing dispatch suite; here the concern is body content.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.tg_alert_dispatch import (
    _build_deep_link,
    _fmt_earliness,
    format_paper_trade_alert,
    notify_paper_trade_opened,
)

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}
_BASE_URL = "http://89.167.116.187:8000"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


# ---------- DASHBOARD_BASE_URL setting (ALR-09) ----------


def test_dashboard_base_url_default():
    assert _settings().DASHBOARD_BASE_URL == "http://89.167.116.187:8000"


def test_dashboard_base_url_env_overridable():
    s = _settings(DASHBOARD_BASE_URL="https://dash.example.com")
    assert s.DASHBOARD_BASE_URL == "https://dash.example.com"


def test_dashboard_base_url_empty_allowed():
    """Empty disables the deep-link line (clean off-switch)."""
    assert _settings(DASHBOARD_BASE_URL="").DASHBOARD_BASE_URL == ""


def test_dashboard_base_url_rejects_schemeless():
    with pytest.raises(Exception):
        _settings(DASHBOARD_BASE_URL="89.167.116.187:8000")


# ---------- _build_deep_link (ALR-09) ----------


def test_build_deep_link_hash_route():
    assert _build_deep_link(_BASE_URL, 42) == "http://89.167.116.187:8000/#/trade/42"


def test_build_deep_link_strips_trailing_slash():
    assert (
        _build_deep_link(_BASE_URL + "/", 7) == "http://89.167.116.187:8000/#/trade/7"
    )


def test_build_deep_link_none_when_base_empty():
    assert _build_deep_link("", 42) is None


def test_build_deep_link_none_when_no_trade_id():
    assert _build_deep_link(_BASE_URL, None) is None


# ---------- _fmt_earliness (ALR-01) ----------


def test_earliness_before_when_negative():
    assert _fmt_earliness(-12.0, "ok") == "12 min before CG trending"


def test_earliness_after_when_positive():
    assert _fmt_earliness(45.0, "ok") == "45 min after CG trending"


def test_earliness_no_reference_status():
    assert _fmt_earliness(None, "no_reference") == "no trending reference"


def test_earliness_error_status_reads_no_reference():
    assert _fmt_earliness(None, "error") == "no trending reference"


# ---------- format_paper_trade_alert v2 golden snapshots ----------


def test_golden_gainers_early_full_card():
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
        sl_pct=10.0,
        lead_time_min=-12.0,
        lead_time_status="ok",
        paper_trade_id=42,
        dashboard_base_url=_BASE_URL,
        liquidity_usd_enriched=120_000.0,
    )
    assert body == (
        "📈 GAINERS EARLY · BTC · $50000.00 · $100\n"
        "24h: +36.9% · mcap $5.5M\n"
        "Entry: $50000.00\n"
        "SL: $45000.00 (-10.0% before slippage)\n"
        "Invalid below $45000.00\n"
        "Liq: $120.0K\n"
        "12 min before CG trending\n"
        "coingecko.com/en/coins/bitcoin\n"
        "Dashboard: http://89.167.116.187:8000/#/trade/42"
    )


def test_golden_losers_contrarian_full_card():
    body = format_paper_trade_alert(
        signal_type="losers_contrarian",
        symbol="MOON",
        coin_id="moon",
        entry_price=2.5,
        amount_usd=100.0,
        signal_data={"price_change_24h": -22.5, "mcap": 8_000_000},
        sl_pct=12.5,
        lead_time_min=-3.0,
        lead_time_status="ok",
        paper_trade_id=5,
        dashboard_base_url=_BASE_URL,
        liquidity_usd_enriched=75_000.0,
    )
    assert body == (
        "📉 LOSERS CONTRARIAN · MOON · $2.50 · $100\n"
        "24h: -22.5% · mcap $8.0M\n"
        "Entry: $2.50\n"
        "SL: $2.19 (-12.5% before slippage)\n"
        "Invalid below $2.19\n"
        "Liq: $75.0K\n"
        "3 min before CG trending\n"
        "coingecko.com/en/coins/moon\n"
        "Dashboard: http://89.167.116.187:8000/#/trade/5"
    )


def test_golden_volume_spike_no_liq_no_trending():
    body = format_paper_trade_alert(
        signal_type="volume_spike",
        symbol="PEPE",
        coin_id="pepe",
        entry_price=0.0001,
        amount_usd=100.0,
        signal_data={"spike_ratio": 8.3},
        sl_pct=10.0,
        lead_time_min=None,
        lead_time_status="no_reference",
        paper_trade_id=99,
        dashboard_base_url=_BASE_URL,
        liquidity_usd_enriched=None,
    )
    assert body == (
        "⚡ VOLUME SPIKE · PEPE · $0.000100 · $100\n"
        "vol×8.3\n"
        "Entry: $0.000100\n"
        "SL: $0.00009000 (-10.0% before slippage)\n"
        "Invalid below $0.00009000\n"
        "no trending reference\n"
        "coingecko.com/en/coins/pepe\n"
        "Dashboard: http://89.167.116.187:8000/#/trade/99"
    )


def test_golden_narrative_prediction_trending_after():
    body = format_paper_trade_alert(
        signal_type="narrative_prediction",
        symbol="DOGE",
        coin_id="dogecoin",
        entry_price=0.15,
        amount_usd=100.0,
        signal_data={"fit": 87, "category": "memecoin", "mcap": 20_000_000_000},
        sl_pct=10.0,
        lead_time_min=45.0,
        lead_time_status="ok",
        paper_trade_id=7,
        dashboard_base_url=_BASE_URL,
        liquidity_usd_enriched=None,
    )
    assert body == (
        "🪙 NARRATIVE PREDICTION · DOGE · $0.1500 · $100\n"
        "memecoin · fit 87 · mcap $20.0B\n"
        "Entry: $0.1500\n"
        "SL: $0.1350 (-10.0% before slippage)\n"
        "Invalid below $0.1350\n"
        "45 min after CG trending\n"
        "coingecko.com/en/coins/dogecoin\n"
        "Dashboard: http://89.167.116.187:8000/#/trade/7"
    )


def test_v2_card_states_before_slippage_on_risk_line():
    """Risk line must never imply the realized fill — configured SL is
    pre-slippage (realized fills have averaged worse). CLAUDE.md §12b spirit."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 10.0, "mcap": 5_000_000},
        sl_pct=10.0,
        paper_trade_id=1,
        dashboard_base_url=_BASE_URL,
    )
    sl_line = next(line for line in body.split("\n") if line.startswith("SL:"))
    assert "before slippage" in sl_line


def test_v2_card_backcompat_no_new_args_is_legacy_body():
    """Old callers (no v2 args) get the pre-v2 body verbatim — the whole
    risk/earliness/deep-link block is opt-in via the new kwargs."""
    body = format_paper_trade_alert(
        signal_type="gainers_early",
        symbol="BTC",
        coin_id="bitcoin",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    assert body == (
        "📈 GAINERS EARLY · BTC · $50000.00 · $100\n"
        "24h: +36.9% · mcap $5.5M\n"
        "coingecko.com/en/coins/bitcoin"
    )


# ---------- notify_paper_trade_opened wiring ----------


async def _insert_paper_trade_with_lead_time(
    db: Database,
    *,
    trade_id: int,
    token_id: str,
    lead_time_min: float | None,
    lead_time_status: str,
) -> None:
    await db._conn.execute(
        """INSERT INTO paper_trades
           (id, token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price,
            sl_price, status, opened_at,
            lead_time_vs_trending_min, lead_time_vs_trending_status)
           VALUES (?, ?, 'TST', 'Test', 'coingecko', 'gainers_early',
                   ?, 50000.0, 100.0, 0.1, 20.0, 10.0, 120.0, 90.0,
                   'open', ?, ?, ?)""",
        (
            trade_id,
            token_id,
            json.dumps({}),
            datetime.now(timezone.utc).isoformat(),
            lead_time_min,
            lead_time_status,
        ),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_notify_body_wires_sl_lead_time_liquidity_and_deep_link(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    # Pin sl_pct so the price math is deterministic.
    await db._conn.execute(
        "UPDATE signal_params SET sl_pct=10.0 WHERE signal_type='gainers_early'"
    )
    # Enriched liquidity for the token (candidates PK = contract_address).
    await db._conn.execute(
        "INSERT INTO candidates (contract_address, chain, token_name, ticker, "
        "first_seen_at, liquidity_usd_enriched) "
        "VALUES ('bitcoin', 'coingecko', 'Bitcoin', 'BTC', ?, 120000.0)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await db._conn.commit()
    await _insert_paper_trade_with_lead_time(
        db,
        trade_id=42,
        token_id="bitcoin",
        lead_time_min=-12.0,
        lead_time_status="ok",
    )
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append(text)

    async def _no_minara(*args, **kwargs):
        return None

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    monkeypatch.setattr("scout.trading.minara_alert.maybe_minara_command", _no_minara)

    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=42,
        signal_type="gainers_early",
        token_id="bitcoin",
        symbol="BTC",
        entry_price=50000.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 36.92, "mcap": 5_500_000},
    )
    assert len(sent) == 1
    body = sent[0]
    assert "Entry: $50000.00" in body
    assert "SL: $45000.00 (-10.0% before slippage)" in body
    assert "Invalid below $45000.00" in body
    assert "Liq: $120.0K" in body
    assert "12 min before CG trending" in body
    assert "Dashboard: http://89.167.116.187:8000/#/trade/42" in body
    await db.close()


@pytest.mark.asyncio
async def test_notify_body_omits_liquidity_when_not_enriched(tmp_path, monkeypatch):
    """#382 not yet activated for the token → no candidates enriched row →
    the Liq line is skipped silently (not rendered blank)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings()
    await _insert_paper_trade_with_lead_time(
        db,
        trade_id=43,
        token_id="freshcoin",
        lead_time_min=None,
        lead_time_status="no_reference",
    )
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append(text)

    async def _no_minara(*args, **kwargs):
        return None

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    monkeypatch.setattr("scout.trading.minara_alert.maybe_minara_command", _no_minara)

    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=43,
        signal_type="gainers_early",
        token_id="freshcoin",
        symbol="FRESH",
        entry_price=1.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 30.0, "mcap": 1_000_000},
    )
    assert len(sent) == 1
    body = sent[0]
    assert "Liq:" not in body
    assert "no trending reference" in body
    assert "Dashboard: http://89.167.116.187:8000/#/trade/43" in body
    await db.close()


@pytest.mark.asyncio
async def test_notify_body_omits_deep_link_when_base_url_empty(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = _settings(DASHBOARD_BASE_URL="")
    await _insert_paper_trade_with_lead_time(
        db,
        trade_id=44,
        token_id="nolink",
        lead_time_min=-5.0,
        lead_time_status="ok",
    )
    sent = []

    async def _fake_send(text, session, settings, parse_mode=None, **kwargs):
        sent.append(text)

    async def _no_minara(*args, **kwargs):
        return None

    monkeypatch.setattr("scout.alerter.send_telegram_message", _fake_send)
    monkeypatch.setattr("scout.trading.minara_alert.maybe_minara_command", _no_minara)

    await notify_paper_trade_opened(
        db,
        settings,
        session=None,
        paper_trade_id=44,
        signal_type="gainers_early",
        token_id="nolink",
        symbol="NL",
        entry_price=1.0,
        amount_usd=100.0,
        signal_data={"price_change_24h": 30.0, "mcap": 1_000_000},
    )
    assert len(sent) == 1
    assert "Dashboard:" not in sent[0]
    await db.close()

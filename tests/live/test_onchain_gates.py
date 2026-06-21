from __future__ import annotations

from decimal import Decimal

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.gates import VALID_REJECT_REASONS, Gates

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _Adapter:
    is_onchain = True

    def __init__(self, *, impact, sellable, sol):
        self._impact, self._sellable, self._sol = impact, sellable, sol
        self.venue_name = "solana"

    async def quote_at_size(self, *, venue_pair, side, size_usd):
        return {
            "out_amount": 1000,
            "price_impact_pct": self._impact,
            "mid": Decimal("1"),
        }

    async def is_sellable(self, *, venue_pair, expected_out_amount):
        return self._sellable

    async def fetch_account_balance(self, asset="USDT"):
        return self._sol if asset == "SOL" else 1000.0


class _KS:
    async def is_active(self):
        return None


def _gates(adapter, **so):
    s = _settings(LIVE_SIGNAL_ALLOWLIST="x", **so)
    return Gates(
        config=LiveConfig(s), db=None, resolver=None, adapter=adapter, kill_switch=_KS()
    )


def test_not_sellable_is_a_valid_reject_reason():
    assert "not_sellable" in VALID_REJECT_REASONS


@pytest.mark.asyncio
async def test_onchain_pass():
    g = _gates(_Adapter(impact=0.5, sellable=True, sol=0.5))
    res = await g.evaluate_onchain(
        signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )
    assert res.passed is True


@pytest.mark.asyncio
async def test_onchain_price_impact_reject():
    g = _gates(_Adapter(impact=9.0, sellable=True, sol=0.5))  # > 3.0 default
    res = await g.evaluate_onchain(
        signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )
    assert res.passed is False
    assert res.reject_reason == "insufficient_depth"


@pytest.mark.asyncio
async def test_onchain_not_sellable_reject():
    g = _gates(_Adapter(impact=0.5, sellable=False, sol=0.5))
    res = await g.evaluate_onchain(
        signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )
    assert res.passed is False
    assert res.reject_reason == "not_sellable"


@pytest.mark.asyncio
async def test_onchain_gas_reserve_reject():
    g = _gates(_Adapter(impact=0.5, sellable=True, sol=0.0))  # < 0.02 default
    res = await g.evaluate_onchain(
        signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )
    assert res.passed is False
    assert res.reject_reason == "insufficient_balance"


# --- C1: exposure / float-cap gate ---------------------------------------


async def _seed_open_solana_notional(db, *, total_usd: float):
    """Seed a paper_trade + open solana live_trades row summing to total_usd."""
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES (?, 'WSOL', 'wsol', 'solana', 'x', '{}',
                   1, 10, 10, 1.2, 0.8, 'open', '2026-06-21T00:00:00+00:00')""",
        (MINT,),
    )
    ptid = cur.lastrowid
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (?, ?, 'WSOL', 'solana', ?, 'x', ?, 'open',
                   '2026-06-21T00:00:00+00:00')""",
        (ptid, MINT, MINT, str(total_usd)),
    )
    await db._conn.commit()
    return ptid


def _gates_with_db(adapter, db, **so):
    s = _settings(LIVE_SIGNAL_ALLOWLIST="x", **so)
    return Gates(
        config=LiveConfig(s),
        db=db,
        resolver=None,
        adapter=adapter,
        kill_switch=_KS(),
    )


@pytest.mark.asyncio
async def test_onchain_exposure_cap_reject_when_over(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Float cap = 100; seed 95 open → a new 10 trade would push to 105 > cap.
    await _seed_open_solana_notional(db, total_usd=95.0)
    g = _gates_with_db(
        _Adapter(impact=0.5, sellable=True, sol=0.5),
        db,
        SOLANA_FLOAT_CAP_USD=Decimal("100"),
    )
    res = await g.evaluate_onchain(
        signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )
    assert res.passed is False
    assert res.reject_reason == "exposure_cap"
    await db.close()


@pytest.mark.asyncio
async def test_onchain_exposure_cap_passes_when_under(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Float cap = 100; seed 80 open → a new 10 trade pushes to 90 ≤ cap.
    await _seed_open_solana_notional(db, total_usd=80.0)
    g = _gates_with_db(
        _Adapter(impact=0.5, sellable=True, sol=0.5),
        db,
        SOLANA_FLOAT_CAP_USD=Decimal("100"),
    )
    res = await g.evaluate_onchain(
        signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )
    assert res.passed is True


@pytest.mark.asyncio
async def test_onchain_exposure_gate_ignores_binance_open_notional(tmp_path):
    """Open notional on a non-solana venue must NOT count against the cap."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES (?, 'WSOL', 'wsol', 'solana', 'x', '{}',
                   1, 10, 10, 1.2, 0.8, 'open', '2026-06-21T00:00:00+00:00')""",
        (MINT,),
    )
    ptid = cur.lastrowid
    # 95 open on binance — should be ignored by the solana-scoped query.
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (?, ?, 'WSOL', 'binance', 'WSOLUSDT', 'x', '95', 'open',
                   '2026-06-21T00:00:00+00:00')""",
        (ptid, MINT),
    )
    await db._conn.commit()
    g = _gates_with_db(
        _Adapter(impact=0.5, sellable=True, sol=0.5),
        db,
        SOLANA_FLOAT_CAP_USD=Decimal("100"),
    )
    res = await g.evaluate_onchain(
        signal_type="x", symbol="X", venue_pair=MINT, size_usd=Decimal("10")
    )
    assert res.passed is True
    await db.close()

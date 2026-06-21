from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
MINT = "So11111111111111111111111111111111111111112"


def _settings(**o):
    return Settings(
        _env_file=None, **_REQUIRED, LIVE_MODE="shadow",
        LIVE_SIGNAL_ALLOWLIST="first_signal", **o,
    )


class _Adapter:
    is_onchain = True
    venue_name = "solana"

    async def quote_at_size(self, *, venue_pair, side, size_usd):
        return {"out_amount": 1000, "price_impact_pct": 0.5, "mid": Decimal("1")}

    async def is_sellable(self, *, venue_pair, expected_out_amount):
        return True

    async def fetch_account_balance(self, asset="USDT"):
        return 0.5 if asset == "SOL" else 1000.0

    async def place_order_request(self, request):
        raise AssertionError("shadow mode must NOT place an order")


class _KS:
    async def is_active(self):
        return None


async def _seed_paper(db, coin_id):
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES (?, 'WSOL', 'wsol', 'solana', 'first_signal', '{}',
                   1, 10, 10, 1.2, 0.8, 'open', '2026-06-21T00:00:00+00:00')""",
        (coin_id,),
    )
    await db._conn.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_solana_signal_forks_to_shadow_without_broadcast(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = _settings()
    ptid = await _seed_paper(db, MINT)
    engine = LiveEngine(
        config=LiveConfig(s), resolver=None, adapter=_Adapter(), db=db,
        kill_switch=_KS(), routing=None, onchain_adapter=_Adapter(),
    )
    paper = SimpleNamespace(
        id=ptid, coin_id=MINT, symbol="WSOL", signal_type="first_signal", chain="solana"
    )
    await engine.on_paper_trade_opened(paper)

    cur = await db._conn.execute(
        "SELECT venue, status FROM shadow_trades WHERE paper_trade_id=?", (ptid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "solana"
    assert row[1] == "open"
    await db.close()

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
        _env_file=None,
        **_REQUIRED,
        LIVE_MODE="shadow",
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        **o,
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
        config=LiveConfig(s),
        resolver=None,
        adapter=_Adapter(),
        db=db,
        kill_switch=_KS(),
        routing=None,
        onchain_adapter=_Adapter(),
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


class _HighImpactAdapter(_Adapter):
    """Adapter that returns price_impact_pct above the cap to force gate rejection."""

    async def quote_at_size(self, *, venue_pair, side, size_usd):
        return {"out_amount": 1000, "price_impact_pct": 9.0, "mid": Decimal("1")}

    async def place_order_request(self, request):
        raise AssertionError("gate-rejected signal must NOT broadcast")


@pytest.mark.asyncio
async def test_onchain_gate_reject_writes_rejected_shadow_row(tmp_path):
    """Price impact above cap → shadow_trades row status='rejected', no broadcast."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = _settings()
    ptid = await _seed_paper(db, MINT)
    engine = LiveEngine(
        config=LiveConfig(s),
        resolver=None,
        adapter=_Adapter(),
        db=db,
        kill_switch=_KS(),
        routing=None,
        onchain_adapter=_HighImpactAdapter(),
    )
    paper = SimpleNamespace(
        id=ptid, coin_id=MINT, symbol="WSOL", signal_type="first_signal", chain="solana"
    )
    await engine.on_paper_trade_opened(paper)

    cur = await db._conn.execute(
        "SELECT status, reject_reason FROM shadow_trades WHERE paper_trade_id=?",
        (ptid,),
    )
    row = await cur.fetchone()
    assert row is not None, "expected a shadow_trades row for the rejected signal"
    assert row[0] == "rejected"
    assert row[1] is not None, "reject_reason must be non-null"
    assert row[1] == "insufficient_depth"
    await db.close()


# --- C2: signature persisted BEFORE broadcast (crash recovery) -----------


class _LiveAdapter(_Adapter):
    """Two-phase live adapter where broadcast raises AFTER prepare. The signed
    tx (and its signature) is known pre-broadcast, so the engine must persist
    the signature to live_trades before calling broadcast_prepared."""

    PREPARED_SIG = "PREPARED_SIG_XYZ"

    async def prepare_order(self, request):
        return self.PREPARED_SIG, "SIGNED_TX_B64"

    async def broadcast_prepared(self, signed_tx_b64):
        raise RuntimeError("network send blew up after the tx may have landed")

    async def place_order_request(self, request):
        raise AssertionError("live two-phase path must NOT call place_order_request")


def _live_settings(**o):
    return Settings(
        _env_file=None,
        **_REQUIRED,
        LIVE_MODE="live",
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        **o,
    )


@pytest.mark.asyncio
async def test_broadcast_failure_leaves_recoverable_row_with_signature(tmp_path):
    """If broadcast_prepared raises, the live_trades row must already carry
    entry_order_id (the signature) and stay status='open' so boot
    reconciliation can re-check it against the chain. It must NOT be
    needs_manual_review with a NULL signature (unrecoverable)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = _live_settings()
    ptid = await _seed_paper(db, MINT)
    engine = LiveEngine(
        config=LiveConfig(s),
        resolver=None,
        adapter=_Adapter(),
        db=db,
        kill_switch=_KS(),
        routing=None,
        onchain_adapter=_LiveAdapter(),
    )
    paper = SimpleNamespace(
        id=ptid,
        coin_id=MINT,
        symbol="WSOL",
        signal_type="first_signal",
        chain="solana",
    )
    await engine.on_paper_trade_opened(paper)

    cur = await db._conn.execute(
        "SELECT status, entry_order_id FROM live_trades WHERE paper_trade_id=?",
        (ptid,),
    )
    row = await cur.fetchone()
    assert row is not None, "expected a live_trades row"
    status, entry_order_id = row
    assert (
        entry_order_id == _LiveAdapter.PREPARED_SIG
    ), "signature must be persisted BEFORE broadcast for recovery"
    assert status == "open", "row must stay 'open' (recoverable), not manual-review"
    await db.close()

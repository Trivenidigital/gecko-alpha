from __future__ import annotations

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.solana_reconciliation import reconcile_open_solana_trades

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _SeqRpc:
    def __init__(self, mapping):
        self._m = mapping

    async def confirm_signature(self, signature):
        return self._m[signature]


async def _seed_open_solana_trade(db, *, sig, ptid):
    # Create paper_trade first (foreign key requirement)
    pt_cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, status, opened_at)
           VALUES (?, 'X', 'Test', 'solana', 'first_signal', '{}',
                   100.0, 10.0, 0.1, 20.0, 10.0, 120.0, 90.0, 'open', '2026-06-21T00:00:00+00:00')""",
        (f"token-{ptid}",),
    )
    paper_trade_id = pt_cur.lastrowid

    cur = await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, client_order_id, entry_order_id, created_at)
           VALUES (?, 'c', 'X', 'solana', 'MINT', 'first_signal',
                   '10', 'open', ?, ?, '2026-06-21T00:00:00+00:00')""",
        (paper_trade_id, f"cid-{ptid}", sig),
    )
    await db._conn.commit()
    return cur.lastrowid


async def _status_of(db, row_id):
    cur = await db._conn.execute("SELECT status FROM live_trades WHERE id=?", (row_id,))
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_reconcile_confirms_fails_and_leaves_pending(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    ok = await _seed_open_solana_trade(db, sig="SIG_OK", ptid=1)
    bad = await _seed_open_solana_trade(db, sig="SIG_BAD", ptid=2)
    wait = await _seed_open_solana_trade(db, sig="SIG_WAIT", ptid=3)
    rpc = _SeqRpc({"SIG_OK": "success", "SIG_BAD": "failed", "SIG_WAIT": "pending"})

    summary = await reconcile_open_solana_trades(db=db, rpc=rpc, settings=_settings())

    # success => position is genuinely open (NOT 'filled', which the CHECK forbids)
    assert await _status_of(db, ok) == "open"
    # failed swap => no position
    assert await _status_of(db, bad) == "rejected"
    # not yet landed => leave open for next boot
    assert await _status_of(db, wait) == "open"
    assert summary == {"confirmed": 1, "failed": 1, "pending": 1}
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_no_rows_is_noop(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await reconcile_open_solana_trades(db=db, rpc=_SeqRpc({}), settings=_settings())
    await db.close()

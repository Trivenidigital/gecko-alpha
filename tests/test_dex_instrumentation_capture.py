"""Wiring test — flag-ON capture helpers are actually invoked (closes the gap
that writers were only unit-tested in isolation). Local-safe (no aiohttp).
"""

from scout.instrumentation.capture import capture_entry_mcap, capture_txns
from scout.models import CandidateToken


class _FakeDB:
    def __init__(self):
        self.entry_calls = []
        self.txns_calls = []

    async def record_entry_mcap(self, *a):
        self.entry_calls.append(a)

    async def log_txns_snapshot(self, *a):
        self.txns_calls.append(a)


def _tok(**kw):
    d = dict(
        contract_address="9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump",
        chain="solana", token_name="X", ticker="X",
        market_cap_usd=1000.0, liquidity_usd=10.0, token_age_days=1.0,
    )
    d.update(kw)
    return CandidateToken(**d)


async def test_capture_entry_mcap_invoked_when_flag_on(settings_factory):
    db = _FakeDB()
    await capture_entry_mcap(db, _tok(), settings_factory(DEX_INSTRUMENTATION_ENABLED=True))
    assert len(db.entry_calls) == 1


async def test_capture_entry_mcap_noop_when_flag_off(settings_factory):
    db = _FakeDB()
    await capture_entry_mcap(db, _tok(), settings_factory(DEX_INSTRUMENTATION_ENABLED=False))
    assert db.entry_calls == []


async def test_capture_txns_dexscreener_source(settings_factory):
    db = _FakeDB()
    await capture_txns(db, _tok(txns_h1_buys=100, txns_h1_sells=20),
                       settings_factory(DEX_INSTRUMENTATION_ENABLED=True))
    assert len(db.txns_calls) == 1
    assert db.txns_calls[0][3] == "dexscreener"


async def test_capture_txns_geckoterminal_source(settings_factory):
    db = _FakeDB()
    await capture_txns(db, _tok(gt_txns_h1_buys=100, gt_txns_h1_sells=20),
                       settings_factory(DEX_INSTRUMENTATION_ENABLED=True))
    assert len(db.txns_calls) == 1
    assert db.txns_calls[0][3] == "geckoterminal"


async def test_capture_txns_noop_when_flag_off(settings_factory):
    db = _FakeDB()
    await capture_txns(db, _tok(txns_h1_buys=100),
                       settings_factory(DEX_INSTRUMENTATION_ENABLED=False))
    assert db.txns_calls == []


async def test_capture_txns_noop_when_no_counts(settings_factory):
    db = _FakeDB()
    await capture_txns(db, _tok(), settings_factory(DEX_INSTRUMENTATION_ENABLED=True))
    assert db.txns_calls == []

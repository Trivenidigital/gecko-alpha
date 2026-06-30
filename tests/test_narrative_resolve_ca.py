"""resolve_ca CA bug fix — must return a coin_id from contract_coin_map (I1),
not the hardcoded None it used to return on a candidates match.
"""

import pytest

from scout.api.narrative_resolver import resolve_ca
from scout.db import Database

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
EVM = "0xAE3E205C3235C9C3a8a8d0FA72cD3cF5f7e9C8B1"  # checksummed (mixed case)


async def _seed(tmp_path):
    p = tmp_path / "narr.db"
    d = Database(p)
    await d.initialize()
    # I1 map: CA -> coin_id (this is what the fix must read)
    await d.record_contract_coin_map(SOL, "solana", "the-black-bull", "platforms", "high")
    await d.record_contract_coin_map(EVM.lower(), "ethereum", "tensor", "platforms", "high")
    # candidates row provides metadata (symbol/name); coin_id comes from the map
    await d._conn.execute(
        "INSERT INTO candidates (contract_address, chain, token_name, ticker, first_seen_at, "
        "market_cap_usd, liquidity_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SOL, "solana", "The Black Bull", "ANSEM", "2026-06-24T00:00:00+00:00", 189931.0, 36353.0),
    )
    await d._conn.commit()
    await d._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await d.close()
    return str(p)


async def test_resolve_ca_returns_coin_id_from_contract_coin_map(tmp_path):
    db_path = await _seed(tmp_path)
    r = await resolve_ca(db_path, ca=SOL, chain="solana")
    assert r is not None
    assert r.get("coin_id") == "the-black-bull"  # was hardcoded None pre-fix
    assert r.get("symbol") == "ANSEM"  # metadata still comes from candidates


async def test_resolve_ca_evm_checksum_resolves_via_lowercase(tmp_path):
    db_path = await _seed(tmp_path)
    # CA arrives checksummed; map stored lowercase -> must still resolve
    r = await resolve_ca(db_path, ca=EVM, chain="ethereum")
    assert r is not None
    assert r.get("coin_id") == "tensor"


async def test_resolve_ca_unknown_returns_none(tmp_path):
    db_path = await _seed(tmp_path)
    r = await resolve_ca(db_path, ca="So11111111111111111111111111111111111111112", chain="solana")
    assert r is None

from gecko.universe import in_band, admit_survivors


def test_in_band(settings):
    assert in_band(settings, mcap_usd=1e6, liquidity_usd=300_000, chain="solana")
    assert in_band(settings, mcap_usd=None, liquidity_usd=300_000, chain="base")
    assert not in_band(settings, mcap_usd=1e6, liquidity_usd=100, chain="solana")
    assert not in_band(settings, mcap_usd=1e9, liquidity_usd=300_000, chain="solana")
    assert not in_band(settings, mcap_usd=1e6, liquidity_usd=300_000, chain="tron")


async def seed(store):
    await store.upsert_token(
        "solana:a", "solana", "a", "A", "A", "2026-10-01T00:00:00Z", "dexscreener"
    )


async def test_admit_requires_24h_survival(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-01T00:00:00Z", {"liquidity_usd": 300_000}
    )
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-01T12:00:00Z", {"liquidity_usd": 300_000}
    )
    assert await admit_survivors(store, settings, "2026-10-01T13:00:00Z") == 0
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-02T01:00:00Z", {"liquidity_usd": 1_000}
    )
    assert (
        await admit_survivors(store, settings, "2026-10-02T02:00:00Z") == 0
    )  # later obs below floor
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-02T03:00:00Z", {"liquidity_usd": 300_000}
    )
    assert await admit_survivors(store, settings, "2026-10-02T04:00:00Z") == 1
    row = await (
        await store.conn.execute("SELECT status FROM tokens WHERE token_id='solana:a'")
    ).fetchone()
    assert row[0] == "active"
    assert (
        await admit_survivors(store, settings, "2026-10-02T05:00:00Z") == 0
    )  # idempotent

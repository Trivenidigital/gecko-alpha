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


async def status(store):
    row = await (
        await store.conn.execute("SELECT status FROM tokens WHERE token_id='solana:a'")
    ).fetchone()
    return row[0]


async def test_admit_requires_contiguous_24h_survival(store, settings):
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
    )  # latest below floor
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-02T03:00:00Z", {"liquidity_usd": 300_000}
    )
    assert (
        await admit_survivors(store, settings, "2026-10-02T04:00:00Z") == 0
    )  # anchor reset by dip
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-03T04:00:00Z", {"liquidity_usd": 300_000}
    )
    assert await admit_survivors(store, settings, "2026-10-03T05:00:00Z") == 1
    assert await status(store) == "active"
    assert (
        await admit_survivors(store, settings, "2026-10-03T06:00:00Z") == 0
    )  # idempotent


async def test_trailing_observation_without_liquidity_is_ignored(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-01T00:00:00Z", {"liquidity_usd": 300_000}
    )
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-02T01:00:00Z", {"liquidity_usd": 300_000}
    )
    await store.add_observation(
        "solana:a", "coingecko", "2026-10-02T02:00:00Z", {"mcap_usd": 1e6}
    )
    assert await admit_survivors(store, settings, "2026-10-02T03:00:00Z") == 1
    assert await status(store) == "active"


async def test_stale_latest_observation_never_admits(store, settings):
    await seed(store)
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-01T00:00:00Z", {"liquidity_usd": 300_000}
    )
    await store.add_observation(
        "solana:a", "dexscreener", "2026-10-02T01:00:00Z", {"liquidity_usd": 300_000}
    )
    assert (
        await admit_survivors(store, settings, "2026-10-04T02:00:00Z") == 0
    )  # latest is 49h old
    assert await status(store) == "candidate"

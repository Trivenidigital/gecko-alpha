import json
import sqlite3

import pytest

from gecko.calls import call_seeds, event_id, insert_call, parse_call, resolve_calls

NOW = "2026-10-14T12:00:00Z"
EVM = "0x" + "Ab" * 20
SOL = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def test_parse_cashtags_and_addresses():
    text = f"$wif $WIF $100 aped {SOL} and {EVM} https://dexscreener.com/base/{EVM}"
    tags, addrs = parse_call(text)
    assert tags == ["WIF"]
    assert addrs == [SOL, EVM.lower()]  # message order; the URL repeat is deduped


def test_parse_ignores_long_base58_and_empty():
    sig = "5" * 88  # a transaction signature is not a mint
    assert parse_call(f"tx {sig}") == ([], [])
    assert parse_call(None) == ([], [])
    assert parse_call("") == ([], [])


def test_event_id_is_lowercased_handle():
    assert event_id("@Gem_Detecter", 42) == "tg:@gem_detecter:42"


async def test_insert_is_idempotent_and_skips_noise(store):
    kw = dict(
        source_type="telegram", source_id="@c", posted_at="2026-10-14T11:00:00+00:00"
    )
    assert await insert_call(store, event_id="tg:@c:1", text=f"buy {SOL}", **kw) is True
    assert (
        await insert_call(store, event_id="tg:@c:1", text=f"buy {SOL}", **kw) is False
    )
    assert await insert_call(store, event_id="tg:@c:2", text="gm frens", **kw) is False
    row = await (
        await store.conn.execute(
            "SELECT posted_at, addresses_json, cashtags_json, token_id FROM source_calls"
        )
    ).fetchone()
    assert row == ("2026-10-14T11:00:00Z", json.dumps([SOL]), "[]", None)


async def _token(store, chain, addr):
    await store.upsert_token(
        f"{chain}:{addr}", chain, addr, "T", "T", NOW, "dexscreener"
    )


async def test_resolve_unique_match_only(store):
    kw = dict(source_type="telegram", source_id="@c", posted_at="2026-10-14T11:00:00Z")
    await insert_call(store, event_id="tg:@c:1", text=SOL, **kw)
    await insert_call(store, event_id="tg:@c:2", text=EVM, **kw)
    await _token(store, "solana", SOL)
    await _token(store, "base", EVM.lower())
    await _token(store, "bsc", EVM.lower())  # same address on two EVM chains: ambiguous
    assert await resolve_calls(store, NOW, since="2026-10-07T00:00:00Z") == 1
    rows = await (
        await store.conn.execute(
            "SELECT event_id, token_id, resolved_at FROM source_calls ORDER BY event_id"
        )
    ).fetchall()
    assert rows == [("tg:@c:1", f"solana:{SOL}", NOW), ("tg:@c:2", None, None)]


async def test_resolve_respects_window(store):
    await insert_call(
        store,
        source_type="telegram",
        source_id="@c",
        event_id="tg:@c:1",
        posted_at="2026-09-01T00:00:00Z",
        text=SOL,
    )
    await _token(store, "solana", SOL)
    assert await resolve_calls(store, NOW, since="2026-10-07T00:00:00Z") == 0
    assert await resolve_calls(store, NOW, since=None) == 1


async def test_seeds_expand_evm_over_configured_chains(store, settings):
    settings.chains = ["solana", "base", "bsc"]
    kw = dict(source_type="telegram", source_id="@c")
    await insert_call(
        store,
        event_id="tg:@c:1",
        posted_at="2026-10-14T11:00:00Z",
        text=f"{SOL} {EVM}",
        **kw,
    )
    await insert_call(
        store,
        event_id="tg:@c:2",
        posted_at="2026-10-12T00:00:00Z",
        text="0x" + "c" * 40,
        **kw,
    )
    seeds = await call_seeds(store, settings, NOW)
    assert seeds == {"solana": {SOL}, "base": {EVM.lower()}, "bsc": {EVM.lower()}}


async def test_seeds_skip_resolved_and_respect_cap(store, settings):
    evm = "0x" + "d" * 40
    kw = dict(source_type="telegram", source_id="@c")
    await insert_call(
        store, event_id="tg:@c:1", posted_at="2026-10-14T11:00:00Z", text=SOL, **kw
    )
    await insert_call(
        store, event_id="tg:@c:2", posted_at="2026-10-14T10:00:00Z", text=evm, **kw
    )
    settings.tg_seed_max = 1
    assert await call_seeds(store, settings, NOW) == {"solana": {SOL}}  # newest only
    settings.tg_seed_max = 60
    await _token(store, "solana", SOL)
    await resolve_calls(store, NOW, since=None)
    assert await call_seeds(store, settings, NOW) == {
        c: {evm} for c in settings.chains if c != "solana"
    }


async def test_insert_constraint_violation_raises(store):
    with pytest.raises(sqlite3.IntegrityError):
        await insert_call(
            store,
            source_type="Telegram",
            source_id="@c",
            event_id="tg:@c:1",
            posted_at="2026-10-14T11:00:00Z",
            text=SOL,
        )

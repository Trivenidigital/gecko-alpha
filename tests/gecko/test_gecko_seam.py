"""Telegram call -> DexScreener discovery -> GoPlus enrichment -> resolution -> features."""

from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
from aioresponses import aioresponses

from gecko.collectors.dexscreener import BOOSTS_URL, PROFILES_URL, TOKENS_URL, collect
from gecko.collectors.goplus import enrich
from gecko.features import compute_features
from gecko.telegram_reader import poll_once

ADDR = "0x" + "e" * 40
NOW = "2026-10-14T12:00:00Z"
GP = "https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses=" + ADDR


class Client:
    async def get_messages(self, entity, limit, min_id, reverse=False):
        return [
            SimpleNamespace(
                id=1,
                date=datetime(2026, 10, 14, 11, tzinfo=timezone.utc),
                message=f"new gem {ADDR}",
            )
        ]


async def _nosleep(_):
    return None


async def test_call_to_features(store, settings):
    settings.chains = ["base"]
    await poll_once(store, Client(), settings, {"@c": "@c"}, {}, now=NOW)
    pair = {
        "chainId": "base",
        "dexId": "u",
        "pairAddress": "0xp",
        "baseToken": {"address": ADDR, "symbol": "E", "name": "E"},
        "priceUsd": "1",
        "liquidity": {"usd": 400000},
        "marketCap": 2e6,
        "volume": {"h24": 1},
        "txns": {"h24": {}},
        "pairCreatedAt": 1_759_000_000_000,
    }
    async with aiohttp.ClientSession() as s:
        with aioresponses() as m:
            m.get(BOOSTS_URL, payload=[])
            m.get(PROFILES_URL, payload=[])
            m.get(TOKENS_URL.format(chain="base", addresses=ADDR), payload=[pair])
            await collect(store, settings, s, NOW, sleep=_nosleep)
            # every EVM core flag present, else GoPlus reads "unknown" (None)
            m.get(
                GP,
                payload={
                    "code": 1,
                    "result": {
                        ADDR: {
                            "is_honeypot": "0",
                            "cannot_sell_all": "0",
                            "is_blacklisted": "0",
                            "holder_count": "500",
                            "holders": [
                                {
                                    "address": "0x" + "1" * 40,
                                    "percent": "0.05",
                                    "is_contract": 0,
                                }
                            ],
                        }
                    },
                },
            )
            await enrich(store, settings, s, NOW, sleep=_nosleep, budget_now=NOW)
    tid = f"base:{ADDR}"
    # the duplicate message is ignored; the poll's resolution pass maps the stored call
    await poll_once(store, Client(), settings, {"@c": "@c"}, {"@c": 1}, now=NOW)
    row = await (
        await store.conn.execute(
            "SELECT t.first_source, sc.token_id FROM tokens t "
            "JOIN source_calls sc ON sc.token_id=t.token_id"
        )
    ).fetchone()
    assert row == ("telegram", tid)
    f = await compute_features(store, settings, tid, NOW)
    assert f.callers_7d == 1 and f.security_ok is True and f.holders == 500
    assert f.top10_pct == 5.0

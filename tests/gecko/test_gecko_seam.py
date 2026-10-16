"""End-to-end seams: Telegram call -> features; discovery -> watchlist -> digest -> exit."""

import argparse
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
from aioresponses import aioresponses
from pydantic import SecretStr

from gecko.benchmark import PRICE_URL, WBTC, fetch_btc
from gecko.collectors.dexscreener import BOOSTS_URL, PROFILES_URL, TOKENS_URL, collect
from gecko.collectors.geckoterminal import OHLCV_URL, collect_ohlcv
from gecko.collectors.goplus import enrich
from gecko.features import compute_features
from gecko.ledger import btc_price_at
from gecko.rank import rank_once
from gecko.report import digest_job
from gecko.research import research
from gecko.store import iso_plus
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


# --- discovery -> watchlist -> research -> digest -> security exit -----------

TOK = "0x" + "f" * 40
TID = f"base:{TOK}"
GP_TOK = GP.replace(ADDR, TOK)
API = "https://api.anthropic.com/v1/messages"
TG = "https://api.telegram.org/bottok/sendMessage"
T0, T1, T2, T3 = (f"2026-10-{d}Z" for d in ("14T00:00:00", "15T00:00:00", "15T21:00:00", "16T18:00:00"))  # fmt: skip
DAY = 86_400
OCT9 = 1_791_504_000  # 2026-10-09T00:00:00Z: last-week candles carry 3x volume


def _later(ts: str) -> str:
    return ts.replace(":00:00Z", ":05:00Z")


async def _discover(store, settings, s, now, *, boosted):
    pair = {
        "chainId": "base",
        "dexId": "u",
        "pairAddress": "0xpool",
        "baseToken": {"address": TOK, "symbol": "SEAM", "name": "Seam"},
        "priceUsd": "1",
        "liquidity": {"usd": 400000},
        "marketCap": 3e6,
        "volume": {"h24": 1e5},
        "txns": {"h24": {}},
        "pairCreatedAt": (OCT9 - 25 * DAY) * 1000,
    }
    boost = [{"chainId": "base", "tokenAddress": TOK}] if boosted else []
    with aioresponses() as m:
        m.get(BOOSTS_URL, payload=boost)
        m.get(PROFILES_URL, payload=[])
        m.get(TOKENS_URL.format(chain="base", addresses=TOK), payload=[pair])
        await collect(store, settings, s, now, sleep=_nosleep)


async def _enrich(store, settings, s, now, **flags):
    r = {"is_honeypot": "0", "cannot_sell_all": "0", "is_blacklisted": "0"}
    r |= {"holder_count": "900", "holders": [{"address": "0x" + "1" * 40, "percent": "0.05"}]}  # fmt: skip
    with aioresponses() as m:
        m.get(GP_TOK, payload={"code": 1, "result": {TOK: r | flags}})
        await enrich(store, settings, s, now, sleep=_nosleep, budget_now=now)


async def _digest(settings):
    with aioresponses() as m:
        m.post(TG, payload={"ok": True})
        assert await digest_job(settings, argparse.Namespace(as_of=None)) == 0
    [text] = [c.kwargs["json"]["text"] for cs in m.requests.values() for c in cs]
    return text


async def _token(store):
    sql = "SELECT status, disqualify_reason FROM tokens WHERE token_id=?"
    return await (await store.conn.execute(sql, (TID,))).fetchone()


async def test_discovery_to_digest_and_security_exit(store, settings):
    settings.chains = ["base"]
    settings.anthropic_api_key = SecretStr("sk-test")
    settings.telegram_bot_token = SecretStr("tok")
    settings.telegram_digest_chat_id = "chat"
    candles = [
        [OCT9 + d * DAY, 1, 1, 1, 1, 300 if d >= 0 else 100] for d in range(-9, 6)
    ]
    ohlcv = OHLCV_URL.format(network="base", pool="0xpool")
    async with aiohttp.ClientSession() as s:
        await _discover(store, settings, s, T0, boosted=True)
        assert await _token(store) == ("candidate", None)
        await _discover(store, settings, s, T1, boosted=False)  # refresh: survived 24 h
        assert await _token(store) == ("active", None)
        with aioresponses() as m:
            m.get(ohlcv, payload={"data": {"attributes": {"ohlcv_list": candles}}})
            await collect_ohlcv(store, settings, s, T1, sleep=_nosleep)
        await _enrich(store, settings, s, T1, buy_tax="0.2")  # tax only
        r = await rank_once(store, settings, _later(T1))
        assert r.entered == [] and await store.active_watchlist() == []
        assert await _token(store) == ("active", None)  # re-read, not disqualified

        await _discover(store, settings, s, T2, boosted=False)
        await _enrich(store, settings, s, T2, buy_tax="0")
        entered = _later(T2)
        r = await rank_once(store, settings, entered)
        assert r.entered == [TID]
        with aioresponses() as m:
            m.get(PRICE_URL, payload={"data": {"attributes": {"token_prices": {WBTC: "100000"}}}})  # fmt: skip
            await fetch_btc(store, settings, s, entered, sleep=_nosleep)
        rows = await (
            await store.conn.execute(
                "SELECT horizon_days, due_at, measured_at FROM forward_returns "
                "WHERE token_id=? ORDER BY horizon_days",
                (TID,),
            )
        ).fetchall()
        assert rows == [(h, iso_plus(entered, h), None) for h in (1, 7, 30, 90, 180)]
        assert await btc_price_at(store, entered) == 100000.0
        dossier = {
            "what_it_is": "x",
            "team_or_deployer_history": "y",
            "narrative_fit": "z",
            "verdict": "strong",
            "confidence": 0.8,
        }
        reply = {
            "content": [{"type": "text", "text": json.dumps(dossier)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
        with aioresponses() as m:
            m.post(API, payload=reply)
            res = await research(store, settings, s, _later(entered), sleep=_nosleep)
        assert res.rows_written == 1

        text = await _digest(settings)
        assert "ENTRANTS (1)" in text and "EXITS (0)" in text
        assert any("SEAM (base)" in ln and ln.endswith("| strong 0.8") for ln in text.splitlines())  # fmt: skip

        await _discover(store, settings, s, T3, boosted=False)
        await _enrich(store, settings, s, T3, is_honeypot="1")  # structural
        assert await _token(store) == ("disqualified", "security_fail")
        r = await rank_once(store, settings, _later(T3))
        assert r.exited == [TID] and r.exited_tax == 0
    sql = "SELECT exit_reason FROM watchlist WHERE token_id=?"
    assert await (await store.conn.execute(sql, (TID,))).fetchone() == ("security_fail",)  # fmt: skip
    text = await _digest(settings)
    assert "ENTRANTS (0)" in text and "EXITS (1)\nSEAM security_fail\n" in text

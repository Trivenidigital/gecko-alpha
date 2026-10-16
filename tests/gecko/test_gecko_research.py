import argparse
import ast
import asyncio
import json
import re
from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses
from pydantic import SecretStr
from structlog.testing import capture_logs
from yarl import URL

from gecko.research import research, research_job
from gecko.runs import PartialFailure

API = "https://api.anthropic.com/v1/messages"
NOW = "2026-10-16T12:00:00Z"
GECKO = Path(__file__).resolve().parents[2] / "gecko"
GOOD = {
    "what_it_is": "A memecoin on Base.",
    "team_or_deployer_history": "Deployer has two prior launches.",
    "narrative_fit": "Fits the agent narrative.",
    "red_flags": ["sell tax 5%"],
    "verdict": "watch",
    "confidence": 0.6,
    "sources": ["https://example.org/a"],
}
USAGE = {"input_tokens": 10_000, "output_tokens": 1_000}  # $0.02 + $0.01


def reply(obj=None, *, text=None, stop="end_turn", searches=3):
    text = text if text is not None else "Here it is:\n" + json.dumps(obj or GOOD)
    usage = dict(USAGE, server_tool_use={"web_search_requests": searches})
    return {
        "content": [
            {"type": "server_tool_use", "id": "s1", "name": "web_search"},
            {
                "type": "web_search_tool_result",
                "tool_use_id": "s1",
                "content": {"type": "web_search_tool_result_error", "error_code": "x"},
            },
            {"type": "text", "text": text},
        ],
        "stop_reason": stop,
        "usage": usage,
    }


@pytest.fixture
def keyed(settings):
    settings.anthropic_api_key = SecretStr("sk-test-key")
    return settings


async def _tok(store, tid, *, sym="AAA", entered="2026-10-15T00:00:00Z", enrich=None):
    await store.upsert_token(
        tid, "base", "0x" + tid * 4, sym, f"{sym} coin", entered, "dexscreener"
    )
    feats = {
        "age_days": 12.5,
        "liquidity_usd": 400_000.0,
        "mcap_usd": 3_000_000.0,
        "vol_7d_ratio": 3.2,
        "holders": 900,
        "top10_pct": 30.0,
        "callers_7d": 2,
    }
    await store.watchlist_upsert(tid, entered, "F4", 1, 1.0, feats)
    if enrich is not None:
        await store.add_observation(tid, "enrich", entered, enrich)


async def _note(store, tid, created_at, cost, note=None, ver=1):
    await store.conn.execute(
        "INSERT INTO research_notes(token_id,created_at,model,schema_version,"
        "note_json,cost_usd) VALUES(?,?,?,?,?,?)",
        (tid, created_at, "m", ver, json.dumps(note or GOOD), cost),
    )
    await store.conn.commit()


async def _notes(store):
    rows = await (
        await store.conn.execute(
            "SELECT token_id, model, schema_version, note_json, cost_usd "
            "FROM research_notes ORDER BY id"
        )
    ).fetchall()
    return [(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]


async def _run(store, settings, *, sleep=None):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    async with aiohttp.ClientSession() as session:
        res = await research(store, settings, session, NOW, sleep=sleep or fake_sleep)
    return res, sleeps


def _bodies(m):
    return [c.kwargs["json"] for c in m.requests.get(("POST", URL(API)), [])]


async def test_research_writes_validated_note_and_cost(store, keyed):
    await _tok(store, "aa")
    with aioresponses() as m:
        m.post(API, payload=reply())
        res, _ = await _run(store, keyed)
        call = m.requests[("POST", URL(API))][0]
    assert (res.rows_written, res.credits_used) == (1, 1)
    [(tid, model, ver, note, cost)] = await _notes(store)
    assert (tid, model, ver) == ("aa", "claude-sonnet-5-5", 1)
    assert note == GOOD
    assert cost == pytest.approx(0.02 + 0.01 + 3 * 0.01)
    timeout = call.kwargs["timeout"]  # connect= makes a connect hang unbilled
    assert (timeout.total, timeout.connect) == (20 * 6, keyed.http_timeout_sec)
    h = call.kwargs["headers"]
    assert h["x-api-key"] == "sk-test-key"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["content-type"] == "application/json"
    body = call.kwargs["json"]
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 2000
    assert body["tools"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    ]
    assert "at most 5 times" in body["system"] and "untrusted" in body["system"]
    assert [msg["role"] for msg in body["messages"]] == ["user"]


async def test_research_pause_turn_resends_once_with_assistant_content(store, keyed):
    await _tok(store, "aa")
    await _tok(store, "bb", entered="2026-10-14T00:00:00Z")
    paused = reply(text="searching...", stop="pause_turn", searches=2)
    with aioresponses() as m:
        m.post(API, payload=paused)
        m.post(API, payload=reply(searches=1))
        m.post(API, payload=paused)  # bb: paused twice -> abandoned, a marker
        # still paused, though its text holds a valid dossier: abandoned all the same
        m.post(API, payload=reply(stop="pause_turn"))
        res, _ = await _run(store, keyed)
        bodies = _bodies(m)
    assert (res.rows_written, res.credits_used) == (2, 4)
    assert bodies[1]["messages"][1] == {
        "role": "assistant",
        "content": paused["content"],
    }
    [(tid, _, ver, note, cost), marker] = await _notes(store)
    assert (tid, ver, note) == ("aa", 1, GOOD)
    assert cost == pytest.approx(2 * 0.03 + 3 * 0.01)  # both calls are charged
    abandoned = {"status": "invalid", "reason": "paused_turn_abandoned"}
    assert marker[:4] == ("bb", "claude-sonnet-5-5", 0, abandoned)
    assert marker[4] == pytest.approx(2 * 0.03 + 5 * 0.01)


async def test_research_skips_without_key_ok_run(store, settings, capsys):
    for key in (None, SecretStr("")):
        settings.anthropic_api_key = key
        await _tok(store, "aa")
        with aioresponses():  # any request would raise
            rc = await research_job(settings, argparse.Namespace(as_of=None))
        assert rc == 0
        assert "research disabled: GECKO_ANTHROPIC_API_KEY unset" in (
            capsys.readouterr().err
        )
    rows = await (
        await store.conn.execute("SELECT ok, rows_written, credits_used FROM runs")
    ).fetchall()
    assert rows == [(1, 0, 0), (1, 0, 0)]
    assert await _notes(store) == []


async def test_research_respects_daily_usd_cap(store, keyed):
    await _note(store, "old1", "2026-10-15T23:59:59Z", 5.0)  # yesterday: not counted
    await _note(store, "old2", "2026-10-16T00:00:00Z", 1.30)
    await _tok(store, "aa")
    await _tok(store, "bb", entered="2026-10-14T00:00:00Z")
    with aioresponses() as m:
        m.post(API, payload=reply(searches=12))  # $0.15: 1.45 + 0.10 > 1.50
        res, _ = await _run(store, keyed)
        assert len(_bodies(m)) == 1
    assert (res.rows_written, res.credits_used) == (1, 1)
    assert [n[0] for n in await _notes(store)][-1] == "aa"
    keyed.research_daily_usd_cap = 1.54  # 1.45 + 0.10 > 1.54: nothing at all
    with aioresponses() as m:
        res, _ = await _run(store, keyed)
    assert (res.rows_written, res.credits_used) == (0, 0)


async def test_research_respects_per_run_cap(store, keyed):
    await _tok(store, "aa", entered="2026-10-10T00:00:00Z")
    await _tok(store, "bb", entered="2026-10-12T00:00:00Z")
    await _tok(store, "cc", entered="2026-10-11T00:00:00Z")
    await _tok(store, "dd", entered="2026-10-15T00:00:00Z")  # already researched
    await _note(store, "dd", "2026-10-01T00:00:00Z", 0.0)
    await _tok(store, "ee", entered="2026-10-15T00:00:00Z")  # exited
    await store.watchlist_exit("ee", "2026-10-15T06:00:00Z", "rank")
    keyed.research_max_per_run = 2
    with aioresponses() as m:
        m.post(API, payload=reply(), repeat=True)
        res, _ = await _run(store, keyed)
        assert len(_bodies(m)) == 2
    assert res.rows_written == 2
    assert [n[0] for n in await _notes(store)] == ["dd", "bb", "cc"]  # newest first


async def test_research_invalid_json_writes_marker_only(store, keyed):
    # a paid reply with no dossier: a charged marker, never a note, never retried;
    # a run with no dossier at all fails
    await _tok(store, "aa")
    await _tok(store, "bb", entered="2026-10-14T00:00:00Z")
    await _tok(store, "cc", entered="2026-10-13T00:00:00Z")
    deep = '{"a":' * 100_000 + "1" + "}" * 100_000  # RecursionError in json
    with aioresponses() as m:
        m.post(API, payload=reply(text="I could not find anything."))
        m.post(API, payload=reply(text='{"verdict": "watch", oops}'))
        m.post(API, payload=reply(text=deep))
        with pytest.raises(PartialFailure) as ei, capture_logs() as logs:
            await _run(store, keyed)
    assert (ei.value.credits_used, ei.value.rows_written) == (3, 3)
    errors = [e["error"] for e in logs if e["event"] == "research_invalid_json"]
    assert errors == ["ValueError", "ValueError", "RecursionError"]
    marker = {"status": "invalid", "reason": "invalid_json"}
    assert [n[:4] for n in await _notes(store)] == [
        (t, "claude-sonnet-5-5", 0, marker) for t in ("aa", "bb", "cc")
    ]
    assert all(n[4] == pytest.approx(0.06) for n in await _notes(store))
    with aioresponses() as m:  # markers make the tokens non-targets
        res, _ = await _run(store, keyed)
    assert (res.rows_written, res.credits_used) == (0, 0)


async def test_research_paid_failures_write_markers(store, keyed):
    for i, tid in enumerate(("t1", "t2", "t3", "t4")):
        await _tok(store, tid, entered=f"2026-10-1{5 - i}T00:00:00Z")
    with aioresponses() as m:
        m.post(API, payload=reply(text="...", stop="pause_turn", searches=2))
        m.post(API, status=400)  # t1: the resend failed; the paused reply is charged
        m.post(API, exception=asyncio.TimeoutError())  # t2: sent, likely billed
        m.post(API, exception=aiohttp.ConnectionTimeoutError())  # t3: never sent
        m.post(API, payload=reply())  # t4
        res, _ = await _run(store, keyed)
    assert (res.rows_written, res.credits_used) == (3, 5)
    notes = await _notes(store)
    assert [(n[0], n[2], n[3].get("reason")) for n in notes] == [
        ("t1", 0, "resend_failed"),
        ("t2", 0, "timeout"),
        ("t4", 1, None),
    ]
    assert [n[4] for n in notes] == pytest.approx([0.05, 0.10, 0.06])
    keyed.research_daily_usd_cap = 0.309  # markers count: 0.21 + 0.10 > 0.309
    with aioresponses() as m:
        res, _ = await _run(store, keyed)
    assert (res.rows_written, res.credits_used) == (0, 0)
    keyed.research_daily_usd_cap = 0.311  # only t3 (nothing written) is retried
    with aioresponses() as m:
        m.post(API, payload=reply(), repeat=True)
        res, _ = await _run(store, keyed)
        [body] = _bodies(m)
    assert "0xt3t3t3t3" in body["messages"][0]["content"]
    assert (res.rows_written, res.credits_used) == (1, 1)


async def test_research_marker_blocks_a_token_for_seven_days(store, keyed):
    marker = {"status": "invalid", "reason": "invalid_json"}
    await _tok(store, "m8")
    await _note(store, "m8", "2026-10-08T11:59:59Z", 0.06, marker, ver=0)  # 8 d
    await _tok(store, "m6", entered="2026-10-14T00:00:00Z")
    await _note(store, "m6", "2026-10-10T12:00:00Z", 0.06, marker, ver=0)  # 6 d
    await _tok(store, "ok", entered="2026-10-13T00:00:00Z")
    await _note(store, "ok", "2026-09-01T00:00:00Z", 0.06)  # a valid note: forever
    with aioresponses() as m:
        m.post(API, payload=reply(), repeat=True)
        res, _ = await _run(store, keyed)
        [body] = _bodies(m)
    assert "0xm8m8m8m8" in body["messages"][0]["content"]
    assert (res.rows_written, res.credits_used) == (1, 1)


async def test_research_without_searches_omits_tools(store, keyed):
    await _tok(store, "aa")
    keyed.research_max_searches = 0
    with aioresponses() as m:
        m.post(API, payload=reply())
        await _run(store, keyed)
        [body] = _bodies(m)
    assert "tools" not in body and "at most 0 times" in body["system"]


async def test_research_truncates_and_drops_unknown_keys(store, keyed):
    await _tok(store, "aa")
    long = dict(
        GOOD,
        what_it_is="w\x00\x1b" + "x" * 500,
        team_or_deployer_history="t" * 401,
        narrative_fit="n‮" * 400,
        red_flags=["\x00\x01", "   ", 7]
        + [f"flag{i}\n" + "r" * 200 for i in range(12)],
        confidence=1.7,
        sources=["ftp://x.org", "not a url", 5]
        + [f"https://s{i}.org/p" for i in range(10)],
        secret="dropped",
    )
    with aioresponses() as m:
        # a brace in prose before the JSON must not cost the paid reply
        text = "Notes {draft}: see below.\n" + json.dumps(long) + "\n{end}"
        m.post(API, payload=reply(text=text))
        res, _ = await _run(store, keyed)
    [(_, _, _, note, _)] = await _notes(store)
    assert set(note) == set(GOOD)
    assert note["what_it_is"] == "w" + "x" * 399
    assert len(note["team_or_deployer_history"]) == 400
    assert note["narrative_fit"] == "n" * 300
    assert len(note["red_flags"]) == 8
    assert all(len(f) == 120 and f.startswith("flag") for f in note["red_flags"])
    assert note["confidence"] == 1.0
    assert note["sources"] == [f"https://s{i}.org/p" for i in range(8)]


@pytest.mark.parametrize(
    "over", [{"verdict": "buy"}, {"verdict": None}, {"confidence": "high"}]
)
async def test_research_rejects_bad_verdict(store, keyed, over):
    await _tok(store, "aa")
    await _tok(store, "bb", entered="2026-10-14T00:00:00Z")
    with aioresponses() as m:
        m.post(API, payload=reply(dict(GOOD, **over)))
        m.post(API, payload=reply())
        res, _ = await _run(store, keyed)
    assert (res.rows_written, res.credits_used) == (2, 2)
    [aa, bb] = await _notes(store)
    assert aa[0] == "aa" and aa[2:4] == (
        0,
        {"status": "invalid", "reason": "invalid_json"},
    )
    assert bb[0] == "bb" and bb[2:4] == (1, GOOD)


async def test_research_retries_once_on_429(store, keyed):
    await _tok(store, "aa")
    await _tok(store, "bb", entered="2026-10-14T00:00:00Z")
    with aioresponses() as m:
        m.post(API, status=429, payload={"error": {"message": "slow down"}})
        m.post(API, payload=reply())  # aa: retried once, then ok
        m.post(API, status=503)
        m.post(API, status=503)  # bb: one retry only, then failed
        m.post(API, payload=reply())  # never consumed
        res, sleeps = await _run(store, keyed)
        assert len(_bodies(m)) == 4
    assert sleeps == [10, 10]
    assert (res.rows_written, res.credits_used) == (1, 4)
    assert [n[0] for n in await _notes(store)] == ["aa"]


async def test_research_all_failed_is_partial_failure(store, keyed):
    await _tok(store, "aa")
    await _tok(store, "bb", entered="2026-10-14T00:00:00Z")
    with aioresponses() as m:
        m.post(API, status=400)
        m.post(API, exception=aiohttp.ClientConnectionError("down"))
        with pytest.raises(PartialFailure) as ei:
            await _run(store, keyed)
    assert (ei.value.credits_used, ei.value.rows_written) == (2, 0)
    assert await _notes(store) == []


async def test_research_prompt_contains_no_other_token(store, keyed):
    enrich = {"security_flags": ["sell_tax"], "holders": 1234, "top10_pct": 22.5}
    await _tok(store, "a1", sym="ALPHA", enrich=enrich)
    await _tok(store, "b2", sym="BRAVO", entered="2026-10-14T00:00:00Z")
    await _tok(store, "c3", sym="CHARLIE", entered="2026-10-13T00:00:00Z")
    await _note(store, "c3", NOW, 0.0, dict(GOOD, what_it_is="PRIOR_DOSSIER_TEXT"))
    with aioresponses() as m:
        m.post(API, payload=reply(), repeat=True)
        await _run(store, keyed)
        bodies = _bodies(m)
    prompts = [b["messages"][0]["content"] for b in bodies]
    assert len(prompts) == 2
    a, b = prompts
    for p in prompts:
        assert "2026-10-16" in p
        assert "CHARLIE" not in p and "c3c3" not in p and "PRIOR_DOSSIER" not in p
    assert "ALPHA" in a and "0xa1a1a1a1" in a and "BRAVO" not in a and "b2b2" not in a
    assert "BRAVO" in b and "0xb2b2b2b2" in b and "ALPHA" not in b and "a1a1" not in b
    assert "sell_tax" in a and "1234" in a and "22.5" in a  # newest enrich row
    for fact in ("base", "12.5", "400000", "3000000", "3.2", '"callers_7d": 2'):
        assert fact in a
    assert "900" in b and "30.0" in b  # no enrich row: features at entry


def test_research_module_touches_only_research_notes():
    src = (GECKO / "research.py").read_text(encoding="utf-8")
    pattern = r"\b(?:INSERT|UPDATE|DELETE|REPLACE|executescript)\b.*"
    writes = re.findall(pattern, src, re.IGNORECASE)
    assert len(writes) == 1
    assert re.match(r"INSERT INTO research_notes\b", writes[0])
    store_attrs, conn_attrs = set(), set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "store":
                store_attrs.add(node.attr)
            if isinstance(node.value, ast.Attribute) and node.value.attr == "conn":
                conn_attrs.add(node.attr)
    assert store_attrs == {"conn"}  # no Store helper at all, read or write
    assert conn_attrs == {"execute", "commit"}


def test_rank_ignores_research_notes():
    for name in ("rank.py", "features.py"):
        assert "research_notes" not in (GECKO / name).read_text(encoding="utf-8")

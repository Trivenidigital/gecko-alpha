import argparse

import aiohttp
from aioresponses import aioresponses
from pydantic import SecretStr

from gecko.report import _send, scorecard_job, scorecard_text

TG_URL = "https://api.telegram.org/bottok/sendMessage"
E = "2026-10-17T00:00:00Z"  # entry, inside the 7 d window
M = "2026-10-22T00:00:00Z"  # measured
NOW = "2026-10-23T00:00:00Z"


async def _wl(store, tid, *, rule="F4", entered=E, feats=None):
    await store.watchlist_upsert(tid, entered, rule, 1, 1.0, feats or {})
    await store.repair_schedules()


async def _ret(store, tid, h, ret, *, btc=(100.0, 125.0), reason=None, at=M):
    await store.conn.execute(
        "UPDATE forward_returns SET measured_at=?, ret=?, btc_entry=?, btc_price=?, "
        "closed_reason=? WHERE token_id=? AND horizon_days=?",
        (at, ret, btc[0], btc[1], reason, tid, h),
    )
    await store.conn.commit()


def _line(text, prefix):
    hits = [ln for ln in text.splitlines() if ln.startswith(prefix)]
    assert len(hits) == 1, (prefix, text)
    return hits[0]


async def test_scorecard_insufficient_data_under_min_n(store, settings):
    for i, r in enumerate((1.0, -0.5, -1.0)):  # one loss: -1 only
        await _wl(store, f"t{i}")
        await _ret(store, f"t{i}", 7, r)
    await _wl(store, "old", entered="2026-09-01T00:00:00Z")
    await store.watchlist_exit("old", "2026-10-01T00:00:00Z", "rank")
    # As of NOW: an exit and a measurement after NOW have not happened yet,
    # and an entry after NOW does not exist.
    await _wl(store, "late")
    await store.watchlist_exit("late", "2026-10-30T00:00:00Z", "rank")
    await _ret(store, "late", 7, 5.0, at="2026-10-25T00:00:00Z")
    await _wl(store, "future", entered="2026-10-24T00:00:00Z")
    text = await scorecard_text(store, settings, NOW)  # scorecard_min_n=20
    assert text.splitlines()[0] == (
        "gecko scorecard 2026-10-23 — watchlist open 4, exited 1, entrants 7 d 4"
    )
    assert _line(text, "F4 7d:") == (
        "F4 7d: n=3 loss=1 unmeasurable=0 expired=0 pending=2 INSUFFICIENT_DATA (n=3)"
    )
    assert "mean" not in text and "median" not in text and "beat" not in text


async def test_scorecard_stats_on_fixture_cohort(store, settings):
    # rets 1.0, -1.0, 6.0, 0.0 -> multiples m = 2, 0, 7, 1 (2 and 7 on the bounds).
    # BTC 100 -> 125 (x1.25), except token c: 100 -> 175 (x1.75).
    # vs_btc = m / btc_mult = 1.6, 0, 4.0, 0.8.
    # mean m = 10/4 = 2.50; median m = (1 + 2)/2 = 1.50; >=2x: 2/4 = 50%;
    # >=7x: 1/4 = 25%; beat BTC (vs > 1): 2/4 = 50%; mean vs = 6.4/4 = 1.60.
    for tid, r in (("a", 1.0), ("b", -1.0), ("c", 6.0), ("d", 0.0)):
        await _wl(store, tid)
        await _ret(store, tid, 7, r, btc=(100.0, 175.0 if tid == "c" else 125.0))
    settings.scorecard_min_n = 4
    text = await scorecard_text(store, settings, NOW)
    assert _line(text, "F4 7d:") == (
        "F4 7d: n=4 loss=1 unmeasurable=0 expired=0 pending=0 | mean 2.50x "
        "median 1.50x ≥2x 50% ≥7x 25% beat BTC 50% vs BTC 1.60x"
    )
    assert _line(text, "F4 30d:") == (
        "F4 30d: n=0 loss=0 unmeasurable=0 expired=0 pending=4 INSUFFICIENT_DATA (n=0)"
    )


async def test_scorecard_reports_expired_and_unmeasurable(store, settings):
    await _wl(store, "x")
    await _wl(store, "y")
    await _ret(store, "x", 1, None, btc=(None, None), reason="expired")
    await _ret(store, "y", 1, None, btc=(None, None), reason="no_entry_price")
    await _ret(store, "y", 7, None, btc=(None, None), reason="no_entry_price")
    await _ret(store, "x", 30, 1.0, btc=(None, None))  # no BTC pair: no statistics
    settings.scorecard_min_n = 1
    text = await scorecard_text(store, settings, NOW)
    assert _line(text, "F4 1d:") == (
        "F4 1d: n=0 loss=0 unmeasurable=1 expired=1 pending=0 INSUFFICIENT_DATA (n=0)"
    )
    assert _line(text, "F4 7d:").startswith(
        "F4 7d: n=0 loss=0 unmeasurable=1 expired=0 pending=1 "
    )
    assert _line(text, "F4 30d:") == (
        "F4 30d: n=1 loss=0 unmeasurable=0 expired=0 pending=1 INSUFFICIENT_DATA (n=0)"
    )


async def test_scorecard_splits_f4_by_entry_mcap(store, settings):
    # >=5M: m = 3 (6M) and 1 (exactly 5M) -> mean 2.00x; <5M: m = 0.5 alone.
    for tid, mcap, r in (("hi", 6e6, 2.0), ("edge", 5e6, 0.0), ("lo", 1e6, -0.5)):
        await _wl(store, tid, feats={"mcap_usd": mcap})
        await _ret(store, tid, 90, r, btc=(100.0, 100.0))
    await _wl(store, "nomcap", feats={"mcap_usd": None})
    await _ret(store, "nomcap", 90, 9.0, btc=(100.0, 100.0))
    await _wl(store, "other", rule="X1", feats={"mcap_usd": 9e6})
    settings.scorecard_min_n = 1
    text = await scorecard_text(store, settings, NOW)
    assert _line(text, "F4 mcap≥5M 90d:") == (
        "F4 mcap≥5M 90d: n=2 loss=0 unmeasurable=0 expired=0 pending=0 | mean 2.00x "
        "median 2.00x ≥2x 50% ≥7x 0% beat BTC 50% vs BTC 2.00x"
    )
    assert _line(text, "F4 mcap<5M 90d:").endswith(
        "| mean 0.50x median 0.50x ≥2x 0% ≥7x 0% beat BTC 0% vs BTC 0.50x"
    )
    assert _line(text, "F4 90d:").startswith("F4 90d: n=4 ")  # unknown mcap included
    assert _line(text, "F4 mcap unknown") == "F4 mcap unknown 1"
    assert _line(text, "F4 mcap≥5M 1d:").endswith("pending=2 INSUFFICIENT_DATA (n=0)")
    assert _line(text, "X1 90d:").startswith("X1 90d: n=0 ")
    assert "X1 mcap" not in text


def _with_channel(settings, chat="chat"):
    settings.telegram_bot_token = SecretStr("tok")
    settings.telegram_digest_chat_id = chat


async def _run(store, settings):
    rc = await scorecard_job(settings, argparse.Namespace(as_of=NOW))
    row = await (
        await store.conn.execute(
            "SELECT ok, rows_written, error FROM runs WHERE job='scorecard'"
        )
    ).fetchone()
    return rc, row


async def test_scorecard_job_fails_without_chat(store, settings):
    _with_channel(settings, chat=None)
    with aioresponses() as m:
        rc, row = await _run(store, settings)
    assert rc == 1 and row[0] == 0 and "telegram_digest_chat_id" in row[2]
    assert len(m.requests) == 0


async def test_scorecard_job_fails_without_token(store, settings):
    settings.telegram_digest_chat_id = "chat"
    rc, row = await _run(store, settings)
    assert rc == 1 and row[0] == 0 and "telegram_bot_token" in row[2]


async def test_scorecard_job_fails_on_delivery_failure(store, settings):
    _with_channel(settings)
    with aioresponses() as m:
        m.post(TG_URL, payload={"ok": False})
        rc, row = await _run(store, settings)
    assert rc == 1 and row[0] == 0 and "delivery failed" in row[2]


async def test_scorecard_job_delivers_and_prints(store, settings, capsys):
    _with_channel(settings)
    await _wl(store, "a")
    with aioresponses() as m:
        m.post(TG_URL, payload={"ok": True})
        rc, row = await _run(store, settings)
    assert rc == 0 and row[:2] == (1, 0)
    (req,) = m.requests.values()
    body = req[0].kwargs["json"]
    assert body["chat_id"] == "chat" and "parse_mode" not in body
    assert body["text"].startswith("gecko scorecard 2026-10-23 — ")
    assert body["text"] in capsys.readouterr().out


async def test_send_splits_long_text_with_labels(settings):
    _with_channel(settings)
    text = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(100))  # ~10 KB
    with aioresponses() as m:
        for _ in range(3):
            m.post(TG_URL, payload={"ok": True})
        async with aiohttp.ClientSession() as s:
            assert await _send(s, settings, text) is True
    sent = [c.kwargs["json"]["text"] for c in next(iter(m.requests.values()))]
    assert [t[:6] for t in sent] == ["(1/3) ", "(2/3) ", "(3/3) "]
    assert all(len(t) <= 4000 for t in sent)
    assert "\n".join(t[6:] for t in sent) == text  # nothing dropped


async def test_send_short_text_is_one_unlabelled_message(settings):
    _with_channel(settings)
    with aioresponses() as m:
        m.post(TG_URL, payload={"ok": True})
        async with aiohttp.ClientSession() as s:
            assert await _send(s, settings, "hello") is True
    (calls,) = m.requests.values()
    assert [c.kwargs["json"]["text"] for c in calls] == ["hello"]


def test_scorecard_job_is_registered_by_the_cli():
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS

    _load_jobs()
    assert JOBS["scorecard"] is scorecard_job

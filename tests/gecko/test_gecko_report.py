import json
import argparse

import aiohttp
import pytest
from aioresponses import aioresponses
from pydantic import SecretStr

from gecko.report import _send, scorecard_job, scorecard_text

TG_URL = "https://api.telegram.org/bottok/sendMessage"
E = "2026-10-17T00:00:00Z"  # entry, inside the 7 d window
M = "2026-10-22T00:00:00Z"  # measured
NOW = "2026-10-23T00:00:00Z"


async def _wl(store, tid, *, rule="F4", entered=E, feats=None):
    await store.kv_set("activation:at", "2026-09-01T00:00:00Z", E)  # F1 cutoff
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
        "F4 7d: n=3 ret_n=3 loss=1 unmeasurable=0 expired=0 other=0 pending=2 "
        "INSUFFICIENT_DATA"
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
        "F4 7d: n=4 ret_n=4 loss=1 unmeasurable=0 expired=0 other=0 pending=0 | mean 2.50x "
        "median 1.50x ≥2x 50% ≥7x 25% beat BTC 50% vs BTC 1.60x"
    )
    assert _line(text, "F4 30d:") == (
        "F4 30d: n=0 ret_n=0 loss=0 unmeasurable=0 expired=0 other=0 pending=4 "
        "INSUFFICIENT_DATA"
    )


async def test_scorecard_reports_expired_and_unmeasurable(store, settings):
    await _wl(store, "x")
    await _wl(store, "y")
    await _ret(store, "x", 1, None, btc=(None, None), reason="expired")
    await _ret(store, "y", 1, None, btc=(None, None), reason="no_entry_price")
    await _ret(store, "y", 7, None, btc=(None, None), reason="no_entry_price")
    await _ret(store, "x", 30, 1.0, btc=(None, None))  # no BTC pair: no statistics
    await _ret(store, "x", 90, None, btc=(None, None))  # no return, no reason
    settings.scorecard_min_n = 1
    text = await scorecard_text(store, settings, NOW)
    assert _line(text, "F4 1d:") == (
        "F4 1d: n=0 ret_n=0 loss=0 unmeasurable=1 expired=1 other=0 pending=0 "
        "INSUFFICIENT_DATA"
    )
    assert _line(text, "F4 7d:").startswith(
        "F4 7d: n=0 ret_n=0 loss=0 unmeasurable=1 expired=0 other=0 pending=1 "
    )
    assert _line(text, "F4 30d:") == (
        # F2: n counts rows with a usable BTC pair, ret_n rows with a return
        "F4 30d: n=0 ret_n=1 loss=0 unmeasurable=0 expired=0 other=0 pending=1 "
        "INSUFFICIENT_DATA"
    )
    assert _line(text, "F4 90d:") == (
        "F4 90d: n=0 ret_n=0 loss=0 unmeasurable=0 expired=0 other=1 pending=1 "
        "INSUFFICIENT_DATA"
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
        "F4 mcap≥5M 90d: n=2 ret_n=2 loss=0 unmeasurable=0 expired=0 other=0 pending=0 "
        "| mean 2.00x "
        "median 2.00x ≥2x 50% ≥7x 0% beat BTC 50% vs BTC 2.00x"
    )
    assert _line(text, "F4 mcap<5M 90d:").endswith(
        "| mean 0.50x median 0.50x ≥2x 0% ≥7x 0% beat BTC 0% vs BTC 0.50x"
    )
    assert _line(text, "F4 90d:").startswith("F4 90d: n=4 ")  # unknown mcap included
    assert _line(text, "F4 mcap unknown") == "F4 mcap unknown 1"
    assert _line(text, "F4 mcap≥5M 1d:").endswith("pending=2 INSUFFICIENT_DATA")
    assert _line(text, "X1 90d:").startswith("X1 90d: n=0 ")
    assert "X1 mcap" not in text


async def test_scorecard_f4_cohort_starts_at_activation(store, settings):
    # F1: replay/test entrants before activation:at are not the F4 cohort
    for tid, entered in (("pre", "2026-09-30T00:00:00Z"), ("post", E)):
        await _wl(store, tid, entered=entered)
        await _ret(store, tid, 7, 1.0)
    await store.kv_set("activation:at", "2026-10-01T00:00:00Z", E)
    settings.scorecard_min_n = 1
    text = await scorecard_text(store, settings, NOW)
    assert _line(text, "F4 7d:").startswith("F4 7d: n=1 ret_n=1 ")
    assert "activation not recorded" not in text
    await store.conn.execute("DELETE FROM kv")
    await store.conn.commit()
    text = await scorecard_text(store, settings, NOW)
    assert _line(text, "activation not recorded")
    assert _line(text, "F4 7d:").startswith("F4 7d: n=0 ret_n=0 ")


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


# --- digest -------------------------------------------------------------------

OLD = "2026-10-01T00:00:00Z"  # before any real-clock digest run


async def _tok(store, tid, sym, *, status="active", chain="solana"):
    await store.upsert_token(tid, chain, tid, sym, sym, OLD, "dexscreener")
    await store.set_token_status(tid, status)


async def _open(store, tid, rank, *, entered=OLD, feats=None, sym=None):
    await _tok(store, tid, sym or tid.upper())
    await store.watchlist_upsert(tid, entered, "F4", rank, 2.0 - rank / 10, feats or {})


async def _digest(store, settings, *, ok=True, n=1):
    """Run the digest job; returns (rc, sent texts, last digest run row)."""
    from gecko.report import digest_job

    _with_channel(settings)
    with aioresponses() as m:
        for _ in range(n):
            m.post(TG_URL, payload={"ok": ok})
        rc = await digest_job(settings, argparse.Namespace(as_of=None))
    sent = [c.kwargs["json"]["text"] for calls in m.requests.values() for c in calls]
    row = await (
        await store.conn.execute(
            "SELECT ok, rows_written, error FROM runs WHERE job='digest' "
            "ORDER BY id DESC LIMIT 1"
        )
    ).fetchone()
    return rc, sent, row


def _section(text, head):
    lines = text.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith(head))
    out = []
    for ln in lines[i + 1 :]:
        if ln.startswith(("ENTRANTS", "EXITS", "TOP ", "health:")):
            break
        out.append(ln)
    return lines[i], out


async def test_first_ever_digest_lists_whole_watchlist(store, settings):
    feats = {"age_days": 3.2, "vol_7d_ratio": 4.25, "liquidity_usd": 52_400.0}
    feats["top10_pct"] = 31.0
    await _open(store, "a", 1, feats=feats)
    await _open(store, "b", 2)
    await _open(store, "gone", 3)
    await store.watchlist_exit("gone", "2026-10-02T00:00:00Z", "rank")
    await _tok(store, "c", "C", status="candidate")
    rc, (text,), row = await _digest(store, settings)
    assert rc == 0 and row == (1, 3, None)
    assert text.splitlines()[0].startswith("gecko digest 20")
    head, lines = _section(text, "ENTRANTS")
    assert head == "ENTRANTS (2)"
    assert lines == [
        "#1 A (solana) score 1.90 | age 3d vol7d x4.2 liq $52K top10 31% | no dossier",
        "#2 B (solana) score 1.80 | age ?d vol7d x? liq $?K top10 ?% | no dossier",
    ]
    assert _section(text, "EXITS") == ("EXITS (0)", [])
    head, lines = _section(text, "TOP ")
    assert head == "TOP 10" and [ln.split()[1] for ln in lines] == ["A", "B"]
    assert text.splitlines()[-1] == (
        "health: freshness clean | candidates 1 active 3 watchlist 2 | "
        "freshness never ran | enrich queue 4"
    )


async def _reported(store, ins=(), outs=()):
    await store.kv_set("digest:reported_entrants", json.dumps(list(ins)), OLD)
    await store.kv_set("digest:reported_exits", json.dumps(list(outs)), OLD)


async def _kv(store):
    rows = await (await store.conn.execute("SELECT key, value FROM kv")).fetchall()
    return dict(rows)


async def test_digest_diff_is_by_reported_identity(store, settings):
    await _open(store, "old", 1, entered="2026-10-01T00:00:00Z")
    await _open(store, "mid", 2, entered="2026-10-05T00:00:00Z")
    await _open(store, "out", 3, entered="2026-10-01T00:00:00Z")
    await store.watchlist_exit("out", "2026-10-06T00:00:00Z", "rank")
    await _open(store, "before", 4, entered="2026-10-01T00:00:00Z")
    await store.watchlist_exit("before", "2026-10-02T00:00:00Z", "rank")
    await _reported(store, ["old", "out", "before"], ["before"])
    marks = await _kv(store)
    rc, _, _ = await _digest(store, settings, ok=False)
    assert rc == 1 and await _kv(store) == marks  # a failed send moves nothing
    rc, (text,), _ = await _digest(store, settings)
    assert rc == 0
    head, lines = _section(text, "ENTRANTS")
    assert head == "ENTRANTS (1)" and lines[0].startswith("#2 MID (solana) ")
    assert _section(text, "EXITS") == ("EXITS (1)", ["OUT rank"])
    assert await _kv(store) == {
        "digest:top": json.dumps(["old", "mid"]),
        "digest:reported_entrants": json.dumps(["before", "mid", "old", "out"]),
        "digest:reported_exits": json.dumps(["before", "out"]),
    }


T0 = "2026-10-05T00:00:00Z"  # one rank run's as_of, shared by all its rows


async def test_digest_entrant_reported_once(store, settings, capsys):
    # the last digest read mid-run: x (stamped T0) was committed, y not yet
    await _open(store, "x", 1, entered=T0)
    await _open(store, "y", 2, entered=T0)
    await _reported(store, ["x"])
    await _fresh(store)
    rc, (text,), _ = await _digest(store, settings)
    head, lines = _section(text, "ENTRANTS")
    assert rc == 0 and head == "ENTRANTS (1)" and lines[0].startswith("#2 Y ")
    assert json.loads(await store.kv_get("digest:reported_entrants")) == ["x", "y"]
    capsys.readouterr()
    rc, sent, _ = await _digest(store, settings)  # both reported: silent
    assert rc == 0 and sent == [] and "[SILENT]" in capsys.readouterr().out


async def test_digest_exit_not_yet_reported(store, settings):
    for tid, rank in (("a", 1), ("x", 2), ("y", 3)):
        await _open(store, tid, rank)
    for tid in ("x", "y"):
        await store.watchlist_exit(tid, T0, "rank")
    await _reported(store, ["a", "x", "y"], ["x"])
    rc, (text,), _ = await _digest(store, settings)
    assert rc == 0 and _section(text, "EXITS") == ("EXITS (1)", ["Y rank"])
    assert _section(text, "ENTRANTS")[0] == "ENTRANTS (0)"


async def test_digest_two_writer_race_reports_an_earlier_stamp_committed_later(
    store, settings
):
    # rank stamps its as_of R; the ledger stamps W > R and commits first. A
    # digest between the two commits must not hide the rank row behind W.
    r, w = "2026-10-05T00:00:00Z", "2026-10-05T00:10:00Z"
    for tid, rank in (("a", 1), ("b", 2)):
        await _open(store, tid, rank)
    await _digest(store, settings)
    await store.watchlist_exit("a", w, "ledger_horizon")
    await _open(store, "d", 4, entered=w)
    rc, (text,), _ = await _digest(store, settings)
    assert _section(text, "EXITS") == ("EXITS (1)", ["A ledger_horizon"])
    await store.watchlist_exit("b", r, "rank")  # committed after that digest
    await _open(store, "c", 3, entered=r)
    rc, (text,), _ = await _digest(store, settings)
    assert rc == 0 and _section(text, "EXITS") == ("EXITS (1)", ["B rank"])
    head, lines = _section(text, "ENTRANTS")
    assert head == "ENTRANTS (1)" and lines[0].startswith("#3 C ")


async def test_digest_freshness_error_still_sends(store, settings, monkeypatch):
    class _Boom(Exception):  # not a RuntimeError: the handler must catch it all
        pass

    async def boom(*a, **k):
        raise _Boom("detector broke")

    monkeypatch.setattr("gecko.report.check_freshness", boom)
    await _open(store, "a", 1)
    rc, (text,), _ = await _digest(store, settings)
    assert rc == 0 and text.splitlines()[-1] == (
        "health: freshness check error _Boom | candidates 0 active 1 watchlist 1 | "
        "freshness never ran | enrich queue 1"
    )


async def test_digest_does_not_miss_row_committed_with_earlier_timestamp(
    store, settings
):
    await _open(store, "a", 1)  # entered OLD
    await _digest(store, settings)  # snapshot at the real clock, after 10-05
    # a rank run stamped 10-05 commits only now: earlier than that digest's
    # snapshot time but later than the newest entered_at the digest saw
    await _open(store, "late", 2, entered="2026-10-05T00:00:00Z")
    rc, (text,), _ = await _digest(store, settings)
    head, lines = _section(text, "ENTRANTS")
    assert rc == 0 and head == "ENTRANTS (1)" and lines[0].startswith("#2 LATE ")


async def _fresh(store, hours_ago: float = 0) -> None:
    """An ok freshness run, so the digest footer carries no warning."""
    from datetime import timedelta

    from gecko.store import canon_ts, parse_utc, utcnow

    at = canon_ts((parse_utc(utcnow()) - timedelta(hours=hours_ago)).isoformat())
    await store.record_run("freshness", at, at, True)


async def test_digest_silent_when_unchanged(store, settings, capsys):
    await _open(store, "a", 1)
    await _open(store, "b", 2)
    await _fresh(store)
    rc, sent, _ = await _digest(store, settings)
    assert rc == 0 and len(sent) == 1
    assert json.loads(await store.kv_get("digest:top")) == ["a", "b"]
    capsys.readouterr()
    rc, sent, row = await _digest(store, settings)
    assert rc == 0 and sent == [] and row == (1, 0, None)
    cap = capsys.readouterr()  # the log is on stdout or stderr: test order decides
    assert "[SILENT]" in cap.out.splitlines() and "digest_silent" in cap.out + cap.err


@pytest.mark.parametrize(
    "case,footer",
    [
        ("never", "freshness never ran"),
        ("stale", "freshness ok 27h ago"),
        ("unresolved", "reader: 3 unresolved handle(s)"),
        ("aging", None),  # 25 h: not yet stale
        ("clean", None),  # 0 unresolved handles is no warning
    ],
)
async def test_digest_footer_warning_sends_on_a_silent_day(
    store, settings, case, footer
):
    # N1: an unchanged watchlist stays silent only when the footer is clean
    from gecko.store import utcnow

    await _open(store, "a", 1)
    await _fresh(store)
    await _digest(store, settings)
    await store.conn.execute("DELETE FROM runs WHERE job='freshness'")
    await store.conn.commit()
    if case != "never":
        await _fresh(store, {"stale": 27.5, "aging": 25.5}.get(case, 0))
    n = {"unresolved": 3, "clean": 0}.get(case)
    if n is not None:
        err = f"0 channel(s) failed; {n} handle(s) unresolved"
        await store.record_run("telegram-reader", utcnow(), utcnow(), True, error=err)
    rc, sent, _ = await _digest(store, settings)
    assert rc == 0 and len(sent) == (1 if footer else 0)
    if footer:
        assert _section(sent[0], "ENTRANTS")[0] == "ENTRANTS (0)"
        assert f"| {footer} |" in sent[0].splitlines()[-1]


async def test_digest_not_silent_when_top_order_changes(store, settings):
    await _open(store, "a", 1)
    await _open(store, "b", 2)
    await _digest(store, settings)
    await store.conn.execute("UPDATE watchlist SET rank=3-rank")
    await store.conn.commit()
    rc, (text,), row = await _digest(store, settings)
    assert rc == 0 and row == (1, 3, None)
    assert _section(text, "ENTRANTS")[0] == "ENTRANTS (0)"
    assert [ln.split()[1] for ln in _section(text, "TOP ")[1]] == ["B", "A"]
    assert json.loads(await store.kv_get("digest:top")) == ["b", "a"]


async def test_digest_not_silent_on_entry_or_exit_outside_top(store, settings):
    from gecko.store import iso_plus, utcnow

    settings.digest_top_n = 1
    await _open(store, "a", 1)
    await _open(store, "b", 2)
    _, (text,), _ = await _digest(store, settings)
    assert _section(text, "TOP ") == ("TOP 1", _section(text, "ENTRANTS")[1][:1])
    later = iso_plus(utcnow(), 1)  # after the run's real-clock anchor
    await _open(store, "c", 3, entered=later)
    rc, (text,), row = await _digest(store, settings)  # top unchanged: ["a"]
    assert rc == 0 and row == (1, 3, None)
    assert _section(text, "ENTRANTS")[0] == "ENTRANTS (1)"
    await store.watchlist_exit("b", iso_plus(later, 1), "rank")
    rc, (text,), _ = await _digest(store, settings)
    assert rc == 0 and _section(text, "EXITS") == ("EXITS (1)", ["B rank"])


async def _top_lines(store, settings):
    from gecko.report import digest_text

    return _section((await digest_text(store, settings, OLD))[0], "TOP ")[1]


async def test_digest_includes_research_verdict(store, settings):
    await _open(store, "a", 1)
    await _open(store, "b", 2)
    for at, note in (
        ("2026-10-02T00:00:00Z", {"verdict": "avoid", "confidence": 0.2}),
        (
            "2026-10-03T00:00:00Z",
            {
                "verdict": "watch",
                "confidence": 0.66,  # .1f
                "red_flags": ["mint\nauthority live", "x" * 60, "third"],
            },
        ),
    ):
        await store.conn.execute(
            "INSERT INTO research_notes(token_id,created_at,model,schema_version,"
            "note_json) VALUES('a',?,'m',1,?)",
            (at, json.dumps(note)),
        )
    await store.conn.commit()
    _, (text,), _ = await _digest(store, settings)
    lines = _section(text, "ENTRANTS")[1]
    assert lines[0].endswith("| watch 0.7")
    assert lines[1] == "  flags: mintauthority live; " + "x" * 40
    assert lines[2].endswith("| no dossier")
    await store.conn.execute(
        "INSERT INTO research_notes(token_id,created_at,model,schema_version,"
        "note_json) VALUES('b','2026-10-04T00:00:00Z','m',1,'{}')"
    )
    await store.conn.commit()
    assert (await _top_lines(store, settings))[2].endswith("| ? ?")


async def test_digest_sanitises_symbols(store, settings):
    evil = "AB\x00C\nD‮EVILLONGSYMBOLNAME123"
    await _reported(store)
    await _open(store, "a", 1, sym=evil, entered="2026-10-02T00:00:00Z")
    await _open(store, "z", 2, sym=evil, entered="2026-10-02T00:00:00Z")
    await store.watchlist_exit("z", "2026-10-03T00:00:00Z", "rank")
    _, (text,), _ = await _digest(store, settings)
    assert _section(text, "ENTRANTS")[1][0].startswith("#1 ABCDEVILLONGSYMB (solana) ")
    assert _section(text, "EXITS")[1] == ["ABCDEVILLONGSYMB rank"]
    assert "\x00" not in text and "‮" not in text
    assert not any(ln.startswith(("D", "EVIL")) for ln in text.splitlines())


async def test_digest_splits_long_messages(store, settings):
    settings.digest_top_n = 50
    for i in range(60):
        await _open(store, f"t{i:02d}", i + 1, sym=f"SYMBOL{i:02d}LONGER")
    from gecko.report import digest_text
    from gecko.store import utcnow

    full = (await digest_text(store, settings, utcnow()))[0]
    rc, sent, row = await _digest(store, settings, n=3)
    assert rc == 0 and row[0] == 1
    assert len(sent) == 3 and [t[:6] for t in sent] == ["(1/3) ", "(2/3) ", "(3/3) "]
    assert all(len(t) <= 4000 for t in sent)
    rejoined = "\n".join(t[6:] for t in sent)  # nothing dropped (date line aside)
    assert rejoined.splitlines()[1:] == full.splitlines()[1:]


async def test_digest_send_failure_fails_run(store, settings):
    await _open(store, "a", 1)
    rc, sent, row = await _digest(store, settings, ok=False)
    assert rc == 1 and len(sent) == 1
    assert row[0] == 0 and "delivery failed" in row[2]
    assert await _kv(store) == {}
    # the failed run did not move the anchor: "a" is still an entrant
    rc, (text,), _ = await _digest(store, settings)
    assert rc == 0 and _section(text, "ENTRANTS")[0] == "ENTRANTS (1)"


async def test_digest_fails_without_channel_even_when_silent(store, settings):
    from gecko.report import digest_job

    settings.telegram_digest_chat_id = "chat"
    rc = await digest_job(settings, argparse.Namespace(as_of=None))
    row = await (await store.conn.execute("SELECT ok, error FROM runs")).fetchone()
    assert rc == 1 and row[0] == 0 and "telegram_bot_token" in row[1]


async def test_digest_health_lists_current_freshness_breaches(store, settings):
    await _open(store, "a", 1)
    await store.repair_schedules()  # the 1 d return fell due 10-02: overdue now
    _, (text,), _ = await _digest(store, settings)
    assert text.splitlines()[-1] == (
        "health: 1 breach(es): forward_returns_backlog | "
        "candidates 0 active 1 watchlist 1 | freshness never ran | enrich queue 1"
    )


async def test_digest_renders_a_marker_as_dossier_failed(store, settings):
    # D2: a paid call that gave no dossier is not "no dossier"; never a verdict
    await _open(store, "a", 1)
    await store.conn.execute(
        "INSERT INTO research_notes(token_id,created_at,model,schema_version,"
        "note_json) VALUES('a','2026-10-02T00:00:00Z','m',0,"
        '\'{"verdict":"x","reason":"paused_turn_abandoned"}\')'
    )
    await store.conn.commit()
    line = (await _top_lines(store, settings))[0]
    assert line.endswith("| dossier failed (paused_turn_abandoned)")


async def test_digest_marker_over_older_valid_note_keeps_verdict(store, settings):
    await _open(store, "a", 1)
    for at, ver, note in (
        ("2026-10-02T00:00:00Z", 1, {"verdict": "watch", "confidence": 0.7}),
        ("2026-10-03T00:00:00Z", 0, {}),  # a newer paid-but-invalid marker
    ):
        await store.conn.execute(
            "INSERT INTO research_notes(token_id,created_at,model,schema_version,"
            "note_json) VALUES('a',?,'m',?,?)",
            (at, ver, json.dumps(note)),
        )
    await store.conn.commit()
    assert (await _top_lines(store, settings))[0].endswith("| watch 0.7")


def test_digest_job_is_registered_by_the_cli():
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS
    from gecko.report import digest_job

    _load_jobs()
    assert JOBS["digest"] is digest_job


async def test_digest_footer_freshness_age_reader_handles_enrich_queue(store, settings):
    # A1 freshness age, A3 unresolved reader handles, A4 the enrich queue length
    from gecko.collectors.goplus import enrich_queue
    from gecko.report import digest_text

    now = "2026-10-16T12:00:00Z"
    await _open(store, "a", 1)
    await _tok(store, "c", "C", status="candidate")
    text, _, _ = await digest_text(store, settings, now)
    assert text.splitlines()[-1].endswith("| freshness never ran | enrich queue 2")
    await store.record_run(
        "freshness", "2026-10-16T08:50:00Z", "2026-10-16T08:55:00Z", 1
    )
    await store.record_run("freshness", now, now, False)  # a failed run is not ok
    err = "1 channel(s) failed; 3 handle(s) unresolved"
    await store.record_run("telegram-reader", now, now, False, error=err)
    text, _, _ = await digest_text(store, settings, now)
    assert len(await enrich_queue(store, settings, now)) == 2
    assert text.splitlines()[-1].endswith(
        "| freshness ok 3h ago | reader: 3 unresolved handle(s) | enrich queue 2"
    )

"""Operator reports over the ledger: weekly scorecard and daily digest (deterministic)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

import aiohttp
import structlog

from gecko.features import compute_features
from gecko.freshness import check_freshness
from gecko.jobs import register
from gecko.notify import send_telegram
from gecko.numeric import num
from gecko.runs import JobResult, run_job
from gecko.settings import GeckoSettings
from gecko.store import Store, canon_ts, parse_utc, utcnow

HORIZONS = (1, 7, 30, 90, 180)
MAX_CHARS = 4000  # Telegram's limit is 4096; longer texts are split, never cut
F4_RULE = "F4"
# Pre-registered split: docs/prereg/2026-10-16-F4-entry-mcap-5m.md
F4_MCAP_SPLIT_USD = 5_000_000
F4_HI, F4_LO = "F4 mcap≥5M", "F4 mcap<5M"
# kv state of the last SENT digest: its top-N ids and the newest entered_at /
# exited_at it saw ("" = none yet), so a row committed late is never skipped.
TOP, HWM_IN, HWM_OUT = "digest:top", "digest:hwm_entered", "digest:hwm_exited"

log = structlog.get_logger("gecko.report")


def _split(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Line-boundary chunks of at most ``limit`` chars, labelled ``(i/n)``."""
    if len(text) <= limit:
        return [text]
    budget = limit - 16  # room for the "(i/n) " label
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        pieces = [line[i : i + budget] for i in range(0, len(line), budget)]
        for piece in pieces or [""]:
            if buf and size + 1 + len(piece) > budget:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            size += len(piece) + (1 if buf else 0)
            buf.append(piece)
    chunks.append("\n".join(buf))
    return [f"({i}/{len(chunks)}) {c}" for i, c in enumerate(chunks, 1)]


async def _send(
    session: aiohttp.ClientSession, settings: GeckoSettings, text: str
) -> bool:
    """Deliver ``text`` to the digest chat as plain text, split as needed.

    Raises ``RuntimeError`` when the bot token or digest chat is missing;
    returns False when any part is not delivered (later parts are not sent).
    """
    token, chat = _channel(settings)
    for part in _split(text):
        if not await send_telegram(session, token, chat, part):
            return False
    return True


def _channel(settings: GeckoSettings) -> tuple[str, str]:
    """(bot token, digest chat id); ``RuntimeError`` when either is missing."""
    token = settings.telegram_bot_token
    if not (token and token.get_secret_value() and settings.telegram_digest_chat_id):
        raise RuntimeError(
            "no digest channel configured (telegram_bot_token / telegram_digest_chat_id)"
        )
    return token.get_secret_value(), settings.telegram_digest_chat_id


def _line(label: str, rows: list[tuple], min_n: int) -> str:
    """One horizon line. ``rows``: (measured, ret, closed_reason, btc_entry, btc_price)."""
    done = [r for r in rows if r[0]]
    vals = [r for r in done if r[1] is not None]
    text = (
        f"{label}: n={len(vals)} loss={sum(r[1] == -1.0 for r in vals)} "
        f"unmeasurable={sum(r[2] == 'no_entry_price' for r in done)} "
        f"expired={sum(r[2] == 'expired' for r in done)} pending={len(rows) - len(done)}"
    )
    # (multiple, multiple vs BTC); rows without a usable BTC pair are skipped
    st = [(1 + r[1], (1 + r[1]) / (r[4] / r[3])) for r in vals if r[3] and r[4]]
    if len(st) < min_n:
        return f"{text} INSUFFICIENT_DATA (n={len(st)})"
    ms = [m for m, _ in st]

    def pct(flags) -> str:
        return f"{100 * sum(flags) / len(st):.0f}%"

    return (
        f"{text} | mean {mean(ms):.2f}x median {median(ms):.2f}x "
        f"≥2x {pct(m >= 2 for m in ms)} ≥7x {pct(m >= 7 for m in ms)} "
        f"beat BTC {pct(v > 1 for _, v in st)} vs BTC {mean(v for _, v in st):.2f}x"
    )


async def scorecard_text(store: Store, settings: GeckoSettings, now: str) -> str:
    """Weekly scorecard as of ``now``: per rule and horizon, ledger counts and,
    from ``scorecard_min_n`` measured rows up, the return statistics."""
    now = canon_ts(now)
    p = {"now": now, "week": canon_ts((parse_utc(now) - timedelta(days=7)).isoformat())}
    o, e, k = await (
        await store.conn.execute(
            "SELECT COALESCE(SUM(exited_at IS NULL OR exited_at>:now),0), "
            "COALESCE(SUM(exited_at<=:now),0), COALESCE(SUM(entered_at>=:week),0) "
            "FROM watchlist WHERE entered_at<=:now",
            p,
        )
    ).fetchone()
    rows = await (
        await store.conn.execute(
            "SELECT w.rule_id, w.token_id, f.horizon_days, "
            "f.measured_at IS NOT NULL AND f.measured_at<=:now, f.ret, f.closed_reason, "
            "f.btc_entry, f.btc_price, json_extract(w.features_at_entry_json,'$.mcap_usd') "
            "FROM forward_returns f JOIN watchlist w ON w.token_id=f.token_id "
            "WHERE w.entered_at<=:now",
            p,
        )
    ).fetchall()
    groups: dict[tuple[str, int], list[tuple]] = defaultdict(list)
    unknown: set[str] = set()
    for rule, tid, h, done, ret, reason, be, bp, mcap in rows:
        r = (bool(done), ret, reason, be, bp)
        groups[(rule, h)].append(r)
        if rule == F4_RULE:
            if (mc := num(mcap)) is None:
                unknown.add(tid)  # excluded from both sides of the split
            else:
                groups[(F4_HI if mc >= F4_MCAP_SPLIT_USD else F4_LO, h)].append(r)
    lines = [
        f"gecko scorecard {now[:10]} — watchlist open {o}, exited {e}, entrants 7 d {k}"
    ]
    for rule in sorted({r[0] for r in rows}):
        labels = (rule, F4_HI, F4_LO) if rule == F4_RULE else (rule,)
        for h in HORIZONS:
            lines += [
                _line(f"{lb} {h}d", groups[(lb, h)], settings.scorecard_min_n)
                for lb in labels
            ]
        if rule == F4_RULE:
            lines.append(f"F4 mcap unknown {len(unknown)}")
    return "\n".join(lines)


@register("scorecard")
async def scorecard_job(settings: GeckoSettings, ns) -> int:
    now = canon_ts(ns.as_of) if ns.as_of else utcnow()

    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        text = await scorecard_text(store, settings, now)
        print(text)
        if not await _send(session, settings, text):
            raise RuntimeError("scorecard delivery failed")
        return JobResult(rows_written=0)

    return await run_job("scorecard", settings, body)


def _clean(value, cap: int = 16) -> str:
    """Untrusted text (token symbols, model output): printable characters, capped."""
    text = "" if value is None else str(value)
    return "".join(c for c in text if c.isprintable())[:cap] or "?"


def _val(v, spec: str, scale: float = 1.0) -> str:
    x = num(v)
    return "?" if x is None else format(x / scale, spec)


async def _token_lines(store: Store, row: tuple, feats: dict) -> list[str]:
    """``#rank SYMBOL (chain) score … | features | verdict`` plus a flags line."""
    tid, rank, score, sym, chain = row[0], row[1], row[2], row[7], row[8]
    note = await (
        await store.conn.execute(
            "SELECT note_json FROM research_notes WHERE token_id=? "
            "AND schema_version<>0 ORDER BY created_at DESC, id DESC LIMIT 1",
            (tid,),
        )
    ).fetchone()
    try:
        note = json.loads(note[0]) if note else None
    except ValueError:
        note = "bad"
    flags: list = []
    if note is None:
        verdict = "no dossier"
    elif not isinstance(note, dict):
        verdict = "bad dossier"
    else:
        verdict = f"{_clean(note.get('verdict'))} {_val(note.get('confidence'), '.1f')}"
        flags = note.get("red_flags") if isinstance(note.get("red_flags"), list) else []
    lines = [
        f"#{rank} {_clean(sym)} ({_clean(chain)}) score {score:.2f} | "
        f"age {_val(feats.get('age_days'), '.0f')}d "
        f"vol7d x{_val(feats.get('vol_7d_ratio'), '.1f')} "
        f"liq ${_val(feats.get('liquidity_usd'), '.0f', 1000)}K "
        f"top10 {_val(feats.get('top10_pct'), '.0f')}% | {verdict}"
    ]
    if flags:
        lines.append("  flags: " + "; ".join(_clean(f, 40) for f in flags[:2]))
    return lines


async def digest_text(
    store: Store, settings: GeckoSettings, now: str
) -> tuple[str, dict[str, str], bool]:
    """(message, kv state to store once sent, whether anything entered or
    exited) since the last sent digest; the first lists every open row."""
    now = canon_ts(now)
    q = store.conn.execute
    hwm_in, hwm_out = await store.kv_get(HWM_IN), await store.kv_get(HWM_OUT)
    rows = await (
        await q(
            "SELECT w.token_id, w.rank, w.score, w.features_at_entry_json, "
            "w.entered_at, w.exited_at, w.exit_reason, t.symbol, t.chain "
            "FROM watchlist w LEFT JOIN tokens t USING(token_id) "
            "ORDER BY w.rank, w.token_id"
        )
    ).fetchall()
    open_rows = [r for r in rows if r[5] is None]
    new = open_rows if hwm_in is None else [r for r in rows if r[4] > hwm_in]
    gone = [] if hwm_out is None else [r for r in rows if r[5] and r[5] > hwm_out]
    top = open_rows[: settings.digest_top_n]
    entry = {r[0]: json.loads(r[3]) for r in new}
    lines = [f"gecko digest {now[:10]}", f"ENTRANTS ({len(new)})"]
    for r in new:
        lines += await _token_lines(store, r, entry[r[0]])
    lines.append(f"EXITS ({len(gone)})")
    lines += [f"{_clean(r[7])} {r[6]}" for r in gone]
    lines.append(f"TOP {settings.digest_top_n}")
    for r in top:
        feats = entry.get(r[0])
        if feats is None:
            feats = (await compute_features(store, settings, r[0], now)).as_dict()
        lines += await _token_lines(store, r, feats)
    breaches = [b.table for b in await check_freshness(store, now, settings)]
    health = f"{len(breaches)} breach(es): {', '.join(breaches)}"
    counts = dict(
        await (
            await q("SELECT status, COUNT(*) FROM tokens GROUP BY status")
        ).fetchall()
    )
    lines.append(
        f"health: {health if breaches else 'freshness clean'} | "
        f"candidates {counts.get('candidate', 0)} active {counts.get('active', 0)} "
        f"watchlist {len(open_rows)}"
    )
    state = {
        TOP: json.dumps([r[0] for r in top]),
        HWM_IN: max((r[4] for r in rows), default=""),
        HWM_OUT: max((r[5] for r in rows if r[5]), default=""),
    }
    return "\n".join(lines), state, bool(new or gone)


@register("digest")
async def digest_job(settings: GeckoSettings, ns) -> int:
    async def body(store: Store, session: aiohttp.ClientSession) -> JobResult:
        _channel(settings)  # a missing channel fails the run even on a silent day
        now = utcnow()  # live-only: __main__ refuses --as-of
        text, state, changed = await digest_text(store, settings, now)
        if not changed and await store.kv_get(TOP) == state[TOP]:
            print("[SILENT]")
            log.info("digest_silent")
            return JobResult(rows_written=0)
        print(text)
        if not await _send(session, settings, text):
            raise RuntimeError("digest delivery failed")
        for key, value in state.items():  # only after every part was delivered
            await store.kv_set(key, value, now)
        return JobResult(rows_written=len(state))

    return await run_job("digest", settings, body)

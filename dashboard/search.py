"""Global cross-table search over scout.db.

Read-only. All queries are parameterized — query string NEVER concatenated
into SQL (only ever bound via `?` placeholders inside `%q%` LIKE patterns).
All connections open in URI read-only mode (`?mode=ro`) via
`dashboard.db._ro_db` for defense-in-depth.

See tasks/plan_dashboard_global_search.md and
tasks/design_dashboard_global_search.md for design context.
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import structlog

from dashboard.db import _ro_db
from dashboard.models import SearchHit, SearchResponse

_log = structlog.get_logger()


class QueryTooShortError(ValueError):
    """Query must be at least 2 non-whitespace characters."""


_CTRL_TABLE = {i: None for i in range(0x20)}


def normalize_query(raw: str) -> str:
    """Strip control chars + whitespace, lowercase, strip leading $/# sigils.

    Raises QueryTooShortError for queries < 2 chars after normalization.
    """
    if raw is None:
        raise QueryTooShortError("query required")
    q = raw.translate(_CTRL_TABLE).strip()
    if q.startswith("$") or q.startswith("#"):
        q = q[1:]
    q = q.lower()
    if len(q) < 2:
        raise QueryTooShortError(f"query too short: {raw!r}")
    return q


def _classify_match(q: str, *fields: str | None) -> str:
    """Return 'exact_symbol', 'exact_contract', 'prefix', or 'substring'."""
    q_lower = q.lower()
    for f in fields:
        if f is None:
            continue
        f_lower = f.lower()
        if f_lower == q_lower:
            if f_lower.startswith("0x") or len(f_lower) > 20:
                return "exact_contract"
            return "exact_symbol"
    for f in fields:
        if f is None:
            continue
        if f.lower().startswith(q_lower):
            return "prefix"
    return "substring"


_MATCH_QUALITY_RANK = {
    "exact_symbol": 0,
    "exact_contract": 1,
    "prefix": 2,
    "substring": 3,
    "text": 4,
}


async def search_candidates(db_path: str, q: str, limit: int) -> list[SearchHit]:
    """Search candidates table on contract_address, token_name, ticker.

    contract_address matched case-sensitively to preserve Solana base58
    semantics; name/ticker matched case-insensitively.
    """
    pattern = f"%{q}%"
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT contract_address, chain, token_name, ticker,
                      market_cap_usd, first_seen_at, alerted_at
               FROM candidates
               WHERE contract_address LIKE ?
                  OR lower(token_name) LIKE ?
                  OR lower(ticker) LIKE ?
               ORDER BY first_seen_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["contract_address"], r["token_name"], r["ticker"])
        hits.append(SearchHit(
            canonical_id=r["contract_address"],
            entity_kind="token",
            symbol=r["ticker"],
            name=r["token_name"],
            chain=r["chain"],
            contract_address=r["contract_address"],
            sources=["candidates"],
            source_counts={"candidates": 1},
            first_seen_at=r["first_seen_at"],
            last_seen_at=r["first_seen_at"],
            match_quality=mq,
        ))
    hits.sort(key=lambda h: _MATCH_QUALITY_RANK[h.match_quality])
    return hits


async def search_paper_trades(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT token_id, symbol, name, chain,
                      MIN(opened_at) AS first_seen,
                      MAX(opened_at) AS last_seen,
                      COUNT(*) AS n,
                      MAX(pnl_pct) AS best_pnl
               FROM paper_trades
               WHERE lower(symbol) LIKE ?
                  OR lower(name) LIKE ?
                  OR lower(token_id) LIKE ?
               GROUP BY token_id, symbol, name, chain
               ORDER BY last_seen DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["symbol"], r["name"], r["token_id"])
        hits.append(SearchHit(
            canonical_id=r["token_id"],
            entity_kind="token",
            symbol=r["symbol"],
            name=r["name"],
            chain=r["chain"],
            sources=["paper_trades"],
            source_counts={"paper_trades": r["n"]},
            first_seen_at=r["first_seen"],
            last_seen_at=r["last_seen"],
            match_quality=mq,
            best_paper_trade_pnl_pct=r["best_pnl"],
        ))
    return hits


async def search_alerts(db_path: str, q: str, limit: int) -> list[SearchHit]:
    """LEFT JOIN candidates so older alert rows (NULL ticker/token_name from
    the pre-migration era) still match queries against the joined candidates
    row's ticker/token_name.
    """
    pattern = f"%{q}%"
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT a.contract_address, a.chain, a.conviction_score, a.alert_market_cap,
                      a.alerted_at,
                      COALESCE(a.token_name, c.token_name) AS token_name,
                      COALESCE(a.ticker, c.ticker) AS ticker
               FROM alerts a
               LEFT JOIN candidates c ON a.contract_address = c.contract_address
               WHERE lower(a.contract_address) LIKE ?
                  OR lower(COALESCE(a.token_name, c.token_name, '')) LIKE ?
                  OR lower(COALESCE(a.ticker, c.ticker, '')) LIKE ?
               ORDER BY a.alerted_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["contract_address"], r["token_name"], r["ticker"])
        hits.append(SearchHit(
            canonical_id=r["contract_address"],
            entity_kind="token",
            symbol=r["ticker"],
            name=r["token_name"],
            chain=r["chain"],
            contract_address=r["contract_address"],
            sources=["alerts"],
            source_counts={"alerts": 1},
            first_seen_at=r["alerted_at"],
            last_seen_at=r["alerted_at"],
            match_quality=mq,
        ))
    return hits


_SNAPSHOT_TABLES = {
    "gainers_snapshots",
    "trending_snapshots",
    "momentum_7d",
    "slow_burn_candidates",
    "velocity_alerts",
    "volume_spikes",
    "predictions",
}

_TIME_COL = {
    "gainers_snapshots": "snapshot_at",
    "trending_snapshots": "snapshot_at",
    "momentum_7d": "detected_at",
    "slow_burn_candidates": "detected_at",
    "velocity_alerts": "detected_at",
    "volume_spikes": "detected_at",
    "predictions": "predicted_at",
}


async def search_snapshots(
    db_path: str, q: str, limit: int, table: str
) -> list[SearchHit]:
    if table not in _SNAPSHOT_TABLES:
        raise ValueError(f"unknown snapshot table: {table}")
    time_col = _TIME_COL[table]
    pattern = f"%{q}%"
    sql = f"""
        SELECT coin_id, symbol, name,
               MIN({time_col}) AS first_seen,
               MAX({time_col}) AS last_seen,
               COUNT(*) AS n
        FROM {table}
        WHERE lower(coin_id) LIKE ?
           OR lower(symbol) LIKE ?
           OR lower(name) LIKE ?
        GROUP BY coin_id, symbol, name
        ORDER BY last_seen DESC
        LIMIT ?
    """
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(sql, (pattern, pattern, pattern, limit))
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["symbol"], r["name"], r["coin_id"])
        hits.append(SearchHit(
            canonical_id=r["coin_id"],
            entity_kind="token",
            symbol=r["symbol"],
            name=r["name"],
            chain="coingecko",
            sources=[table],
            source_counts={table: r["n"]},
            first_seen_at=r["first_seen"],
            last_seen_at=r["last_seen"],
            match_quality=mq,
        ))
    return hits


async def search_tg_messages(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT id, channel_handle, posted_at, sender, text, cashtags, contracts
               FROM tg_social_messages
               WHERE lower(text) LIKE ?
                  OR lower(cashtags) LIKE ?
                  OR lower(contracts) LIKE ?
               ORDER BY posted_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        hits.append(SearchHit(
            canonical_id=f"tg_msg:{r['id']}",
            entity_kind="tg_msg",
            symbol=None,
            name=f"{r['channel_handle']} #{r['id']}",
            chain=None,
            sources=["tg_social_messages"],
            source_counts={"tg_social_messages": 1},
            first_seen_at=r["posted_at"],
            last_seen_at=r["posted_at"],
            match_quality="text",
        ))
    return hits


async def search_tg_signals(db_path: str, q: str, limit: int) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT token_id, symbol, contract_address, chain,
                      MIN(created_at) AS first_seen,
                      MAX(created_at) AS last_seen,
                      COUNT(*) AS n
               FROM tg_social_signals
               WHERE lower(symbol) LIKE ?
                  OR lower(token_id) LIKE ?
                  OR lower(COALESCE(contract_address, '')) LIKE ?
               GROUP BY token_id, symbol, contract_address, chain
               ORDER BY last_seen DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        mq = _classify_match(q, r["symbol"], r["token_id"], r["contract_address"])
        hits.append(SearchHit(
            canonical_id=r["token_id"],
            entity_kind="token",
            symbol=r["symbol"],
            chain=r["chain"],
            contract_address=r["contract_address"],
            sources=["tg_social_signals"],
            source_counts={"tg_social_signals": r["n"]},
            first_seen_at=r["first_seen"],
            last_seen_at=r["last_seen"],
            match_quality=mq,
        ))
    return hits


async def search_narrative_inbound(
    db_path: str, q: str, limit: int
) -> list[SearchHit]:
    pattern = f"%{q}%"
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT id, tweet_author, tweet_ts, tweet_text,
                      extracted_cashtag, resolved_coin_id, received_at
               FROM narrative_alerts_inbound
               WHERE lower(tweet_text) LIKE ?
                  OR lower(COALESCE(extracted_cashtag, '')) LIKE ?
                  OR lower(COALESCE(resolved_coin_id, '')) LIKE ?
                  OR lower(tweet_author) LIKE ?
               ORDER BY received_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, pattern, limit),
        )
        rows = await cur.fetchall()
    hits = []
    for r in rows:
        if r["resolved_coin_id"]:
            canonical = r["resolved_coin_id"]
            kind = "token"
            chain = "coingecko"
        else:
            canonical = f"x_alert:{r['id']}"
            kind = "x_alert"
            chain = None
        hits.append(SearchHit(
            canonical_id=canonical,
            entity_kind=kind,
            symbol=(r["extracted_cashtag"] or "").lstrip("$") or None,
            name=f"@{r['tweet_author']} (X)",
            chain=chain,
            sources=["narrative_alerts_inbound"],
            source_counts={"narrative_alerts_inbound": 1},
            first_seen_at=r["received_at"],
            last_seen_at=r["received_at"],
            match_quality="text",
        ))
    return hits


async def _safe_call(coro):
    """Wrap a search coroutine so a missing table or other DB error returns []."""
    try:
        return await coro
    except (aiosqlite.OperationalError, FileNotFoundError):
        return []


def _ts_to_int(ts: str | None) -> int:
    """Map ISO timestamp to a sortable int (epoch-seconds). None → 0."""
    if not ts:
        return 0
    try:
        from datetime import datetime

        return int(
            datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        )
    except (ValueError, AttributeError):
        return 0


async def run_search(
    db_path: str, raw_q: str, limit: int = 50
) -> SearchResponse:
    """Orchestrate per-table searches in parallel, dedup by (canonical_id, entity_kind, chain)."""
    q = normalize_query(raw_q)
    _t0 = time.monotonic()
    coros = [
        _safe_call(search_candidates(db_path, q, limit)),
        _safe_call(search_paper_trades(db_path, q, limit)),
        _safe_call(search_alerts(db_path, q, limit)),
        _safe_call(search_snapshots(db_path, q, limit, "gainers_snapshots")),
        _safe_call(search_snapshots(db_path, q, limit, "trending_snapshots")),
        _safe_call(search_snapshots(db_path, q, limit, "momentum_7d")),
        _safe_call(search_snapshots(db_path, q, limit, "slow_burn_candidates")),
        _safe_call(search_snapshots(db_path, q, limit, "velocity_alerts")),
        _safe_call(search_snapshots(db_path, q, limit, "volume_spikes")),
        _safe_call(search_snapshots(db_path, q, limit, "predictions")),
        _safe_call(search_tg_messages(db_path, q, limit)),
        _safe_call(search_tg_signals(db_path, q, limit)),
        _safe_call(search_narrative_inbound(db_path, q, limit)),
    ]
    results = await asyncio.gather(*coros, return_exceptions=False)
    by_id: dict[tuple, SearchHit] = {}
    for hits in results:
        for h in hits:
            cid = (h.canonical_id, h.entity_kind, h.chain or "")
            if cid not in by_id:
                by_id[cid] = h
                continue
            existing = by_id[cid]
            existing.sources = sorted(set(existing.sources) | set(h.sources))
            for src, n in h.source_counts.items():
                existing.source_counts[src] = (
                    existing.source_counts.get(src, 0) + n
                )
            if h.first_seen_at and (
                not existing.first_seen_at
                or h.first_seen_at < existing.first_seen_at
            ):
                existing.first_seen_at = h.first_seen_at
            if h.last_seen_at and (
                not existing.last_seen_at
                or h.last_seen_at > existing.last_seen_at
            ):
                existing.last_seen_at = h.last_seen_at
            if (
                _MATCH_QUALITY_RANK[h.match_quality]
                < _MATCH_QUALITY_RANK[existing.match_quality]
            ):
                existing.match_quality = h.match_quality
            existing.symbol = existing.symbol or h.symbol
            existing.name = existing.name or h.name
            existing.chain = existing.chain or h.chain
            existing.contract_address = (
                existing.contract_address or h.contract_address
            )
            if h.best_paper_trade_pnl_pct is not None and (
                existing.best_paper_trade_pnl_pct is None
                or h.best_paper_trade_pnl_pct
                > existing.best_paper_trade_pnl_pct
            ):
                existing.best_paper_trade_pnl_pct = h.best_paper_trade_pnl_pct
    merged = list(by_id.values())
    merged.sort(
        key=lambda h: (
            _MATCH_QUALITY_RANK[h.match_quality],
            -(len(h.sources)),
            -(_ts_to_int(h.last_seen_at)),
        )
    )
    total_pre_slice = len(merged)
    merged = merged[:limit]
    _dur_ms = int((time.monotonic() - _t0) * 1000)
    _log.info(
        "dashboard_search",
        q_len=len(q),
        hits=len(merged),
        pre_slice=total_pre_slice,
        truncated=total_pre_slice > limit,
        dur_ms=_dur_ms,
    )
    if _dur_ms > 2000:
        _log.warning("dashboard_search_slow", q_len=len(q), dur_ms=_dur_ms)
    return SearchResponse(
        query=q,
        total_hits=len(merged),
        hits=merged,
        truncated=total_pre_slice > limit,
    )

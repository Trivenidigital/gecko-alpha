"""DASH-05 moved-already / too-late postmortem recorder (forward-recording only).

The trader's central complaint is that monsters like ANSEM (+3,354% while the
token sat at TOO_LATE / score 0) never feed back into detection: by the time the
dashboard classifies a token as "moved already", the T-minus evidence that would
name the gate that dropped it is 7 days from being pruned out of
``gainers_snapshots``. This recorder closes that loop FORWARD — the first time a
token crosses into the dashboard's moved-already state, it serialises the
still-available T-minus evidence into ``moved_already_postmortems`` so the next
monster has a record naming its dropping gate.

Detection MIRRORS the dashboard predicate (``_trade_window_state`` "late" in
dashboard/db.py): an OPEN paper trade whose pct-from-entry exceeds
``MOVED_ALREADY_RUN_PCT_THRESHOLD`` (25% by default, matching the dashboard's
``pct > 25`` late boundary). One representative trade per token is used — the most
recent open trade, exactly the ``primary`` row the inbox classifies. Dedup is per
token via a UNIQUE(token_id) on the table, so each token records one postmortem.

Observe-only: nothing here changes the scorer, gate, trader, or any alert. The job
is flag-gated OFF (``MOVED_ALREADY_POSTMORTEM_ENABLED``); when False it returns
immediately and the pipeline is byte-identical.

There is NO backfill path: ``gainers_snapshots`` has a 7-day retention, so pre-run
evidence for tokens that already moved is gone. Only forward captures are possible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

logger = structlog.get_logger()

# Row caps so a single pathological token can't bloat one evidence blob.
_GAINERS_SNAPSHOT_CAP = 50
_SCORE_HISTORY_CAP = 50


async def _gather_evidence(
    conn,
    token_id: str,
    *,
    run_stats: dict,
    detected_iso: str,
    evidence_cutoff_iso: str,
    window_days: int,
) -> tuple[dict, str | None]:
    """Serialise the T-minus evidence available NOW for one moved-already token.

    Returns ``(evidence_dict, dropping_gate)``. ``dropping_gate`` is the most
    frequent pre-detection blocked reason in ``trade_decision_events`` (None when
    the token was never blocked). Every sub-query is fail-soft — a missing/legacy
    table degrades that evidence slice to empty rather than dropping the row.
    """
    evidence: dict = {
        "run_stats": run_stats,
        "symbol": run_stats.get("symbol"),
        "name": run_stats.get("name"),
        "chain": run_stats.get("chain"),
        "evidence_window_days": window_days,
        "captured_at": detected_iso,
        "gainers_snapshots": [],
        "candidate": None,
        "trade_decision_blocks": [],
        "entry_mcap_snapshot": None,
        "score_history": [],
    }

    # Latest gainers_snapshots rows for the coin, inside the 7-day evidence window.
    try:
        cur = await conn.execute(
            "SELECT symbol, name, price_change_24h, market_cap, volume_24h, "
            "price_at_snapshot, snapshot_at "
            "FROM gainers_snapshots "
            "WHERE coin_id = ? AND datetime(snapshot_at) >= datetime(?) "
            "ORDER BY datetime(snapshot_at) DESC LIMIT ?",
            (token_id, evidence_cutoff_iso, _GAINERS_SNAPSHOT_CAP),
        )
        evidence["gainers_snapshots"] = [dict(r) for r in await cur.fetchall()]
    except Exception:
        logger.exception(
            "moved_already_gainers_snapshots_query_failed", token_id=token_id
        )

    # candidates first_seen / scores. contract_address holds the CG slug for
    # coingecko-sourced rows (== token_id); a non-CG token simply misses here.
    try:
        cur = await conn.execute(
            "SELECT first_seen_at, quant_score, narrative_score, conviction_score, "
            "signals_fired, virality_class "
            "FROM candidates WHERE contract_address = ? LIMIT 1",
            (token_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            evidence["candidate"] = dict(row)
    except Exception:
        logger.exception("moved_already_candidate_query_failed", token_id=token_id)

    # trade_decision_events blocked reasons BEFORE detection — the gate(s) that
    # dropped it. Most frequent = dropping_gate.
    dropping_gate: str | None = None
    try:
        cur = await conn.execute(
            "SELECT reason, COUNT(*) AS c FROM trade_decision_events "
            "WHERE token_id = ? AND decision = 'blocked' "
            "AND datetime(created_at) < datetime(?) "
            "GROUP BY reason ORDER BY c DESC, reason ASC",
            (token_id, detected_iso),
        )
        blocks = [
            {"reason": r["reason"], "count": r["c"]} for r in await cur.fetchall()
        ]
        evidence["trade_decision_blocks"] = blocks
        if blocks:
            dropping_gate = blocks[0]["reason"]
    except Exception:
        logger.exception(
            "moved_already_decision_events_query_failed", token_id=token_id
        )

    # entry_mcap_snapshots (write-once, non-pruned) if present.
    try:
        cur = await conn.execute(
            "SELECT chain, first_seen_at, mcap_usd_at_entry, liquidity_usd_at_entry, "
            "token_age_days_at_entry, captured_at "
            "FROM entry_mcap_snapshots WHERE contract_address = ? LIMIT 1",
            (token_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            evidence["entry_mcap_snapshot"] = dict(row)
    except Exception:
        logger.exception("moved_already_entry_mcap_query_failed", token_id=token_id)

    # score_history (recent) if present.
    try:
        cur = await conn.execute(
            "SELECT score, scanned_at FROM score_history "
            "WHERE contract_address = ? "
            "ORDER BY datetime(scanned_at) DESC LIMIT ?",
            (token_id, _SCORE_HISTORY_CAP),
        )
        evidence["score_history"] = [dict(r) for r in await cur.fetchall()]
    except Exception:
        logger.exception("moved_already_score_history_query_failed", token_id=token_id)

    return evidence, dropping_gate


async def record_moved_already_postmortems(
    db, settings, *, now: datetime | None = None
) -> dict:
    """Capture a postmortem for each NEW token in the moved-already/late state.

    Scans open paper trades, mirrors the dashboard's "late" predicate to detect
    tokens that have run past the threshold, dedups against already-recorded
    tokens, and writes one evidence row per new token. Returns a run summary.
    """
    if not getattr(settings, "MOVED_ALREADY_POSTMORTEM_ENABLED", False):
        return {"enabled": False, "detected": 0, "recorded": 0}
    if db._conn is None:
        raise RuntimeError("Database not initialized")

    conn = db._conn
    now = now or datetime.now(timezone.utc)
    detected_iso = now.isoformat()
    threshold = float(settings.MOVED_ALREADY_RUN_PCT_THRESHOLD)
    window_days = int(settings.MOVED_ALREADY_EVIDENCE_WINDOW_DAYS)
    evidence_cutoff_iso = (now - timedelta(days=window_days)).isoformat()

    already_recorded = await db.get_recorded_moved_already_token_ids()

    # Open paper trades joined to their latest price. Ordered so the FIRST row per
    # token is the most-recent open trade — the dashboard's `primary` row.
    cur = await conn.execute(
        """SELECT pt.token_id AS token_id, pt.entry_price AS entry_price,
                  pt.symbol AS symbol, pt.name AS name, pt.chain AS chain,
                  pc.current_price AS current_price, pc.market_cap AS market_cap,
                  pc.price_change_24h AS price_change_24h
             FROM paper_trades pt
             JOIN price_cache pc ON pc.coin_id = pt.token_id
            WHERE pt.status = 'open'
              AND pt.entry_price IS NOT NULL AND pt.entry_price > 0
              AND pc.current_price IS NOT NULL
            ORDER BY datetime(pt.opened_at) IS NULL ASC,
                     datetime(pt.opened_at) DESC,
                     pt.id DESC""",
    )
    rows = await cur.fetchall()

    seen: set[str] = set()
    detected = 0
    recorded = 0
    for row in rows:
        token_id = row["token_id"]
        if token_id in seen:
            continue  # keep only the primary (most-recent) trade per token
        seen.add(token_id)

        entry_price = float(row["entry_price"])
        current_price = float(row["current_price"])
        pct_from_entry = round((current_price - entry_price) / entry_price * 100, 2)
        if pct_from_entry <= threshold:
            continue

        detected += 1
        if token_id in already_recorded:
            continue  # dedup: already captured on an earlier detection

        run_stats = {
            "pct_from_entry": pct_from_entry,
            "price_change_24h": row["price_change_24h"],
            "current_price": current_price,
            "entry_price": entry_price,
            "market_cap": row["market_cap"],
            "symbol": row["symbol"],
            "name": row["name"],
            "chain": row["chain"],
        }
        evidence, dropping_gate = await _gather_evidence(
            conn,
            token_id,
            run_stats=run_stats,
            detected_iso=detected_iso,
            evidence_cutoff_iso=evidence_cutoff_iso,
            window_days=window_days,
        )
        wrote = await db.insert_moved_already_postmortem(
            token_id=token_id,
            detected_at=detected_iso,
            run_pct=pct_from_entry,
            evidence=evidence,
            dropping_gate=dropping_gate,
        )
        if wrote:
            recorded += 1
            already_recorded.add(token_id)
            logger.info(
                "moved_already_postmortem_recorded",
                token_id=token_id,
                run_pct=pct_from_entry,
                dropping_gate=dropping_gate,
                gainers_snapshots=len(evidence["gainers_snapshots"]),
            )

    summary = {
        "enabled": True,
        "detected": detected,
        "recorded": recorded,
        "run_at": detected_iso,
    }
    logger.info("moved_already_postmortem_run", **summary)
    return summary

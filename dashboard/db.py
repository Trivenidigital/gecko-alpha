"""Read-only database queries for the dashboard against scout.db."""

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiosqlite
import structlog

KNOWN_SIGNALS = [
    "vol_liq_ratio",
    "market_cap_range",
    "holder_growth",
    "token_age",
    "social_mentions",
    "buy_pressure",
    "momentum_ratio",
    "vol_acceleration",
    "cg_trending_rank",
    "solana_bonus",
    "score_velocity",
]


@asynccontextmanager
async def _ro_db(db_path: str):
    """Open a read-only connection to the database.

    Connection is acquired INSIDE the try block so any exception between
    ``connect()`` returning and ``yield`` (e.g. failed row_factory
    assignment) closes the connection cleanly. Previously the
    row_factory line was outside try and would leak if it raised.
    """
    import os

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database file not found: {db_path}")
    db = None
    try:
        db = await aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = aiosqlite.Row
        yield db
    finally:
        if db is not None:
            await db.close()


async def get_candidates(db_path: str, limit: int = 20) -> list[dict]:
    """Top candidates ordered by conviction_score DESC."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT contract_address, token_name, ticker, chain,
                      market_cap_usd, liquidity_usd, volume_24h_usd,
                      quant_score, narrative_score, conviction_score,
                      signals_fired, alerted_at, first_seen_at
               FROM candidates
               ORDER BY COALESCE(conviction_score, -1) DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            raw = d.get("signals_fired")
            d["signals_fired"] = json.loads(raw) if raw else []
            results.append(d)
        return results


async def get_recent_alerts(db_path: str, limit: int = 20) -> list[dict]:
    """Recent alerts ordered by alerted_at DESC, with outcome data if available."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT a.contract_address, a.chain, a.conviction_score, a.alerted_at,
                      a.alert_market_cap,
                      c.token_name, c.ticker, c.market_cap_usd,
                      o.price_change_pct, o.check_price, o.check_time
               FROM alerts a
               LEFT JOIN candidates c ON a.contract_address = c.contract_address
               LEFT JOIN outcomes o ON a.contract_address = o.contract_address
               ORDER BY a.alerted_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_win_rate(db_path: str) -> dict:
    """Compute win rate from outcomes table."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM outcomes WHERE price_change_pct IS NOT NULL",
        )
        total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM outcomes WHERE price_change_pct > 0",
        )
        wins = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT AVG(price_change_pct) FROM outcomes WHERE price_change_pct IS NOT NULL",
        )
        avg_row = await cursor.fetchone()
        avg_pct = avg_row[0] if avg_row and avg_row[0] is not None else 0

    return {
        "total_outcomes": total,
        "wins": wins,
        "win_rate_pct": round((wins / total * 100) if total > 0 else 0, 1),
        "avg_return_pct": round(avg_pct, 1),
    }


async def get_signal_hit_rates(db_path: str) -> list[dict]:
    """For each known signal, count how many candidates fired it today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT signals_fired FROM candidates
               WHERE date(first_seen_at) = ? AND signals_fired IS NOT NULL""",
            (today,),
        )
        rows = await cursor.fetchall()

        count_cursor = await db.execute(
            "SELECT COUNT(*) FROM candidates WHERE date(first_seen_at) = ?",
            (today,),
        )
        total_row = await count_cursor.fetchone()
        total = total_row[0] if total_row else 0

    counts: dict[str, int] = {s: 0 for s in KNOWN_SIGNALS}
    for row in rows:
        try:
            signals = json.loads(row["signals_fired"])
            for sig in signals:
                if sig in counts:
                    counts[sig] += 1
        except (json.JSONDecodeError, TypeError):
            pass

    return [
        {"signal_name": name, "fired_count": count, "total_candidates_today": total}
        for name, count in counts.items()
    ]


async def get_status(db_path: str) -> dict:
    """Pipeline status summary."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with _ro_db(db_path) as db:
        # All tokens seen today
        cursor = await db.execute(
            "SELECT COUNT(*) FROM candidates WHERE date(first_seen_at) = ?",
            (today,),
        )
        tokens_scanned = (await cursor.fetchone())[0]

        # Candidates promoted (quant_score >= 25 today)
        cursor = await db.execute(
            """SELECT COUNT(*) FROM candidates
               WHERE date(first_seen_at) = ? AND quant_score IS NOT NULL AND quant_score >= 25""",
            (today,),
        )
        candidates_today = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM mirofish_jobs WHERE date(created_at) = ?",
            (today,),
        )
        mirofish_jobs = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM alerts WHERE date(alerted_at) = ?",
            (today,),
        )
        alerts_today = (await cursor.fetchone())[0]

    return {
        "pipeline_status": "running",
        "tokens_scanned_session": tokens_scanned,
        "candidates_today": candidates_today,
        "mirofish_jobs_today": mirofish_jobs,
        "mirofish_cap": 50,
        "alerts_today": alerts_today,
        "cg_calls_this_minute": 0,
        "cg_rate_limit": 30,
    }


@asynccontextmanager
async def _rw_db(db_path: str):
    """Open a read-write connection to the database.

    Same try-block guard as ``_ro_db`` — connection acquired inside try
    so exceptions during ``connect()`` or ``row_factory`` cannot leak.
    """
    conn = None
    try:
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        if conn is not None:
            await conn.close()


# ---------------------------------------------------------------------------
# Narrative rotation queries
# ---------------------------------------------------------------------------


async def get_narrative_heating(db_path: str, limit: int = 20) -> list[dict]:
    """Most recent category snapshots with acceleration."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT cs1.category_id, cs1.name, cs1.market_cap,
                      cs1.market_cap_change_24h, cs1.volume_24h,
                      cs1.market_regime, cs1.snapshot_at
               FROM category_snapshots cs1
               WHERE cs1.snapshot_at = (SELECT MAX(snapshot_at) FROM category_snapshots)
               ORDER BY cs1.market_cap_change_24h DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]

        # Enrich with first detection time from narrative_signals
        if result:
            try:
                category_ids = [r["category_id"] for r in result]
                placeholders = ",".join("?" * len(category_ids))
                cursor = await conn.execute(
                    f"""SELECT category_id, MIN(detected_at) as first_detected
                        FROM narrative_signals
                        WHERE category_id IN ({placeholders})
                        GROUP BY category_id""",
                    category_ids,
                )
                first_detected = {
                    r["category_id"]: r["first_detected"]
                    for r in await cursor.fetchall()
                }
                for r in result:
                    r["first_detected_at"] = first_detected.get(r["category_id"])

                # Enrich with gain since detection & peak gain
                for r in result:
                    cid = r["category_id"]
                    first_det = first_detected.get(cid)
                    if not first_det:
                        r["gain_since_detection"] = None
                        r["peak_gain"] = None
                        continue

                    # Market cap at detection
                    cur = await conn.execute(
                        """SELECT market_cap FROM category_snapshots
                           WHERE category_id = ? AND snapshot_at <= ?
                           ORDER BY snapshot_at DESC LIMIT 1""",
                        (cid, first_det),
                    )
                    det_row = await cur.fetchone()
                    mcap_at_det = (det_row[0] if det_row else None) or 0
                    current_mcap = r.get("market_cap") or 0

                    if mcap_at_det > 0 and current_mcap > 0:
                        r["gain_since_detection"] = round(
                            ((current_mcap - mcap_at_det) / mcap_at_det) * 100, 2
                        )
                    else:
                        r["gain_since_detection"] = None

                    # Peak market cap since detection
                    cur = await conn.execute(
                        """SELECT MAX(market_cap) FROM category_snapshots
                           WHERE category_id = ? AND snapshot_at >= ?""",
                        (cid, first_det),
                    )
                    peak_row = await cur.fetchone()
                    peak_mcap = (peak_row[0] if peak_row else None) or 0

                    if mcap_at_det > 0 and peak_mcap > 0:
                        r["peak_gain"] = round(
                            ((peak_mcap - mcap_at_det) / mcap_at_det) * 100, 2
                        )
                    else:
                        r["peak_gain"] = None

            except Exception:
                # narrative_signals table may not exist in older DBs
                for r in result:
                    r["first_detected_at"] = None
                    r["gain_since_detection"] = None
                    r["peak_gain"] = None

        return result


async def get_narrative_predictions(
    db_path: str, limit: int = 50, outcome: str | None = None
) -> list[dict]:
    """Paginated predictions with optional outcome filter."""
    async with _ro_db(db_path) as conn:
        if outcome:
            cursor = await conn.execute(
                """SELECT * FROM predictions
                   WHERE outcome_class = ?
                   ORDER BY predicted_at DESC LIMIT ?""",
                (outcome, limit),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM predictions ORDER BY predicted_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_narrative_metrics(db_path: str) -> dict:
    """Hit rate and true alpha metrics for narrative predictions."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute("""SELECT
                SUM(CASE WHEN outcome_class='HIT' AND is_control=0 THEN 1 ELSE 0 END) as agent_hits,
                SUM(CASE WHEN is_control=0 AND outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as agent_total,
                SUM(CASE WHEN outcome_class='HIT' AND is_control=1 THEN 1 ELSE 0 END) as ctrl_hits,
                SUM(CASE WHEN is_control=1 AND outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as ctrl_total,
                COUNT(*) as total_predictions,
                SUM(CASE WHEN outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as resolved
               FROM predictions""")
        row = await cursor.fetchone()
        d = dict(row) if row else {}

        agent_hits = d.get("agent_hits") or 0
        agent_total = d.get("agent_total") or 0
        ctrl_hits = d.get("ctrl_hits") or 0
        ctrl_total = d.get("ctrl_total") or 0
        total_predictions = d.get("total_predictions") or 0
        resolved = d.get("resolved") or 0

        agent_rate = round(
            (agent_hits / agent_total * 100) if agent_total > 0 else 0, 1
        )
        ctrl_rate = round((ctrl_hits / ctrl_total * 100) if ctrl_total > 0 else 0, 1)
        true_alpha = round(agent_rate - ctrl_rate, 1)

    return {
        "agent_hit_rate": agent_rate,
        "ctrl_hit_rate": ctrl_rate,
        "true_alpha": true_alpha,
        "total_predictions": total_predictions,
        "active_predictions": total_predictions - resolved,
    }


async def get_narrative_strategy(db_path: str) -> list[dict]:
    """All rows from agent_strategy table."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute("SELECT * FROM agent_strategy ORDER BY key")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_narrative_strategy(db_path: str, key: str, value: str) -> dict | None:
    """Update a strategy row: set value, locked=1, updated_by='manual'."""
    async with _rw_db(db_path) as conn:
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """UPDATE agent_strategy
               SET value = ?, locked = 1, updated_by = 'manual', updated_at = ?
               WHERE key = ?""",
            (value, now, key),
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT * FROM agent_strategy WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_narrative_learn_logs(db_path: str, limit: int = 20) -> list[dict]:
    """Recent learn_logs entries."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT * FROM learn_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_narrative_category_history(
    db_path: str, category_id: str, hours: int = 48
) -> list[dict]:
    """Timeline data for a specific category."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT category_id, name, market_cap, market_cap_change_24h,
                      volume_24h, market_regime, snapshot_at
               FROM category_snapshots
               WHERE category_id = ?
                 AND snapshot_at >= datetime('now', ?)
               ORDER BY snapshot_at ASC""",
            (category_id, f"-{hours} hours"),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Chains queries
# ---------------------------------------------------------------------------


async def get_chains_active(db_path: str, limit: int = 50) -> list[dict]:
    """Incomplete chains ordered by last_step_time desc."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT ac.id, ac.token_id, ac.pipeline, ac.pattern_id, ac.pattern_name,
                      ac.steps_matched, ac.step_events, ac.anchor_time,
                      ac.last_step_time, ac.is_complete, ac.completed_at, ac.created_at,
                      c.token_name, c.ticker, c.chain,
                      c.market_cap_usd, c.volume_24h_usd, c.quant_score
               FROM active_chains ac
               LEFT JOIN candidates c ON ac.token_id = c.contract_address
               WHERE ac.is_complete = 0
               ORDER BY ac.last_step_time DESC
               LIMIT ?""",
            (limit,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        for r in rows:
            try:
                r["steps_matched"] = json.loads(r.get("steps_matched") or "[]")
            except (json.JSONDecodeError, TypeError):
                r["steps_matched"] = []
            try:
                r["step_events"] = json.loads(r.get("step_events") or "[]")
            except (json.JSONDecodeError, TypeError):
                r["step_events"] = []
        return rows


async def get_chains_matches(db_path: str, limit: int = 30) -> list[dict]:
    """Recent completed chain matches."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT cm.*, c.token_name, c.ticker, c.chain
               FROM chain_matches cm
               LEFT JOIN candidates c ON cm.token_id = c.contract_address
               ORDER BY cm.completed_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_chains_patterns(db_path: str) -> list[dict]:
    """Chain pattern definitions with stats."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT id, name, description, min_steps_to_trigger,
                      conviction_boost, alert_priority, is_active,
                      historical_hit_rate, total_triggers, total_hits,
                      steps_json
               FROM chain_patterns
               ORDER BY id"""
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        for r in rows:
            triggers = r.get("total_triggers") or 0
            hits = r.get("total_hits") or 0
            r["hit_rate"] = round((hits / triggers * 100) if triggers > 0 else 0, 1)
            try:
                r["steps_json"] = json.loads(r.get("steps_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                r["steps_json"] = []
        return rows


async def get_chains_events_recent(db_path: str, limit: int = 50) -> list[dict]:
    """Most recent INTERESTING signal events (filters out routine zero-score noise)."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT se.id, se.token_id, se.pipeline, se.event_type, se.event_data,
                      se.source_module, se.created_at,
                      c.token_name, c.ticker, c.chain,
                      c.market_cap_usd, c.volume_24h_usd, c.quant_score
               FROM signal_events se
               LEFT JOIN candidates c ON se.token_id = c.contract_address
               WHERE NOT (
                   se.event_type = 'candidate_scored'
                   AND (se.event_data LIKE '%"signal_count": 0%' OR se.event_data LIKE '%"quant_score": 0%')
               )
               ORDER BY se.created_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        # Parse event_data JSON and extract useful fields
        for r in rows:
            ed = {}
            try:
                ed = json.loads(r.get("event_data") or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            r["event_data_parsed"] = ed
            # Promote key fields for frontend convenience
            r["ed_quant_score"] = ed.get("quant_score") or r.get("quant_score") or 0
            r["ed_signal_count"] = ed.get("signal_count") or 0
            r["ed_signals_fired"] = ed.get("signals_fired") or []
            r["ed_price_change_1h"] = ed.get("price_change_1h")
            r["ed_price_change_24h"] = ed.get("price_change_24h")
        return rows


async def get_chains_top_movers(db_path: str, limit: int = 5) -> list[dict]:
    """Top tokens by quant_score from recent signal events (last 24h).

    Since price_change columns are not persisted in the candidates table,
    we rank by quant_score and extract price data from event_data JSON.
    """
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT DISTINCT se.token_id,
                      c.token_name, c.ticker, c.chain,
                      c.market_cap_usd, c.volume_24h_usd, c.quant_score,
                      se.event_data, se.created_at
               FROM signal_events se
               LEFT JOIN candidates c ON se.token_id = c.contract_address
               WHERE se.created_at >= datetime('now', '-24 hours')
               ORDER BY COALESCE(c.quant_score, 0) DESC
               LIMIT ?""",
            (limit * 3,),  # fetch extra to dedup
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    # Dedup by token_id, keep best score, extract price data from event_data
    seen: set[str] = set()
    results: list[dict] = []
    for r in rows:
        tid = r["token_id"]
        if tid in seen:
            continue
        seen.add(tid)
        ed = {}
        try:
            ed = json.loads(r.get("event_data") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        r["price_change_1h"] = ed.get("price_change_1h")
        r["price_change_24h"] = ed.get("price_change_24h")
        del r["event_data"]
        results.append(r)
        if len(results) >= limit:
            break
    return results


async def get_chains_stats(db_path: str) -> dict:
    """Aggregate chain stats."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM signal_events "
            "WHERE created_at >= datetime('now', '-24 hours')"
        )
        events_24h = (await cursor.fetchone())[0]

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM active_chains WHERE is_complete = 0"
        )
        active_count = (await cursor.fetchone())[0]

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM active_chains WHERE is_complete = 1"
        )
        completed_active_count = (await cursor.fetchone())[0]

        cursor = await conn.execute("SELECT COUNT(*) FROM chain_matches")
        matches_count = (await cursor.fetchone())[0]

        # Expired = is_complete but no chain_matches linkage (best-effort)
        cursor = await conn.execute("SELECT COUNT(*) FROM signal_events")
        total_events = (await cursor.fetchone())[0]

    return {
        "active_chains": active_count,
        "completed_matches": matches_count,
        "completed_active": completed_active_count,
        "events_24h": events_24h,
        "total_events": total_events,
    }


# ---------------------------------------------------------------------------
# System health query
# ---------------------------------------------------------------------------


_SQL_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


async def _table_stats(conn, table: str, time_col: str) -> dict:
    """Return count and latest timestamp for a table; tolerate missing tables.

    SQLite does not parametrise table/column names, so the f-string-built
    queries below MUST validate identifiers at the call boundary. Today's
    only caller (``get_system_health``) uses a hardcoded list, but the
    regex guard makes the contract explicit and prevents future callers
    from passing user input that smuggles SQL. Raises ``ValueError`` on
    a non-identifier.
    """
    if not _SQL_IDENTIFIER_RE.match(table):
        raise ValueError(f"invalid table identifier: {table!r}")
    if not _SQL_IDENTIFIER_RE.match(time_col):
        raise ValueError(f"invalid column identifier: {time_col!r}")
    try:
        cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
        count = (await cursor.fetchone())[0]
        cursor = await conn.execute(f"SELECT MAX({time_col}) FROM {table}")
        latest = (await cursor.fetchone())[0]
        return {"count": count, "latest": latest}
    except Exception:
        return {"count": 0, "latest": None}


async def get_system_health(db_path: str) -> dict:
    """Row counts + last activity for major tables."""
    tables = [
        ("category_snapshots", "snapshot_at"),
        ("narrative_signals", "created_at"),
        ("predictions", "predicted_at"),
        ("second_wave_candidates", "detected_at"),
        ("signal_events", "created_at"),
        ("active_chains", "last_step_time"),
        ("chain_matches", "completed_at"),
        ("chain_patterns", "created_at"),
        ("briefings", "created_at"),
        ("trending_snapshots", "snapshot_at"),
        ("trending_comparisons", "created_at"),
        ("candidates", "first_seen_at"),
        ("alerts", "alerted_at"),
        ("learn_logs", "created_at"),
        ("agent_strategy", "updated_at"),
    ]
    result = {}
    async with _ro_db(db_path) as conn:
        for table, time_col in tables:
            result[table] = await _table_stats(conn, table, time_col)
    return result


async def get_funnel(db_path: str) -> dict:
    """Pipeline funnel counts derived from current DB state."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM candidates WHERE date(first_seen_at) = ?",
            (today,),
        )
        ingested = (await cursor.fetchone())[0]

        cursor = await db.execute(
            """SELECT COUNT(*) FROM candidates
               WHERE date(first_seen_at) = ? AND quant_score IS NOT NULL AND quant_score >= 60""",
            (today,),
        )
        scored = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM mirofish_jobs WHERE date(created_at) = ?",
            (today,),
        )
        mirofish_run = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM alerts WHERE date(alerted_at) = ?",
            (today,),
        )
        alerted = (await cursor.fetchone())[0]

    return {
        "ingested": ingested,
        "aggregated": ingested,
        "scored": scored,
        "safety_passed": scored,
        "mirofish_run": mirofish_run,
        "alerted": alerted,
    }


async def get_quality_signals(
    db_path: str, max_mcap: float = 500_000_000, limit: int = 30
) -> list[dict]:
    """Curated, enriched signals from narrative predictions + pipeline candidates + category heating."""
    per_source = max(limit // 2, 10)

    async with _ro_db(db_path) as conn:
        # 1. Narrative predictions (highest quality -- Claude scored)
        narrative = []
        try:
            cursor = await conn.execute(
                """SELECT
                       'narrative_prediction' as signal_type,
                       p.coin_id              as token_id,
                       p.symbol,
                       p.name                 as token_name,
                       p.category_name,
                       p.market_cap_at_prediction as market_cap,
                       p.narrative_fit_score,
                       p.counter_risk_score,
                       p.counter_argument,
                       p.confidence,
                       p.market_regime,
                       p.watchlist_users,
                       p.predicted_at         as detected_at,
                       p.outcome_class,
                       COALESCE(p.narrative_fit_score, 0)
                           as quality_score
                   FROM predictions p
                   WHERE p.is_control = 0
                     AND p.market_cap_at_prediction < ?
                   ORDER BY p.predicted_at DESC
                   LIMIT ?""",
                (max_mcap, per_source),
            )
            narrative = [dict(r) for r in await cursor.fetchall()]
        except Exception:
            pass

        # 2. Pipeline candidates with real signals (quant_score > 15, mcap > 0)
        pipeline = []
        try:
            cursor = await conn.execute(
                """SELECT
                       'pipeline_candidate' as signal_type,
                       c.contract_address    as token_id,
                       c.ticker              as symbol,
                       c.token_name,
                       NULL                  as category_name,
                       c.market_cap_usd      as market_cap,
                       NULL                  as narrative_fit_score,
                       c.counter_risk_score,
                       c.counter_argument,
                       NULL                  as confidence,
                       NULL                  as market_regime,
                       NULL                  as watchlist_users,
                       c.first_seen_at       as detected_at,
                       NULL                  as outcome_class,
                       c.quant_score         as quality_score,
                       c.chain
                   FROM candidates c
                   WHERE c.quant_score > 15
                     AND c.market_cap_usd > 0
                     AND c.market_cap_usd < ?
                   ORDER BY c.first_seen_at DESC
                   LIMIT ?""",
                (max_mcap, per_source),
            )
            pipeline = [dict(r) for r in await cursor.fetchall()]
        except Exception:
            pass

        # 3. Category heating signals
        heating = []
        try:
            cursor = await conn.execute("""SELECT
                       'category_heating'    as signal_type,
                       ns.category_id        as token_id,
                       NULL                  as symbol,
                       ns.category_name      as token_name,
                       ns.category_name      as category_name,
                       NULL                  as market_cap,
                       NULL                  as narrative_fit_score,
                       NULL                  as counter_risk_score,
                       NULL                  as counter_argument,
                       NULL                  as confidence,
                       NULL                  as market_regime,
                       NULL                  as watchlist_users,
                       ns.detected_at,
                       NULL                  as outcome_class,
                       ns.acceleration        as quality_score
                   FROM narrative_signals ns
                   ORDER BY ns.detected_at DESC
                   LIMIT 10""")
            heating = [dict(r) for r in await cursor.fetchall()]
        except Exception:
            pass

    # Merge, compute tiers, sort by quality_score desc
    merged = narrative + pipeline + heating
    for row in merged:
        qs = row.get("quality_score") or 0
        if qs > 60:
            row["quality_tier"] = "high"
        elif qs > 30:
            row["quality_tier"] = "medium"
        else:
            row["quality_tier"] = "low"

    # Classify signals as narrative vs meme/DEX
    for s in merged:
        if (
            s["signal_type"] == "narrative_prediction"
            or s["signal_type"] == "category_heating"
        ):
            s["is_meme"] = False
        elif s.get("chain") and s["chain"] not in ("coingecko", None, ""):
            s["is_meme"] = True
        else:
            s["is_meme"] = False

    merged.sort(key=lambda r: (r.get("quality_score") or 0), reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------------------
# Briefing queries
# ---------------------------------------------------------------------------


async def get_briefing_latest(db_path: str) -> dict | None:
    """Return the most recent briefing."""
    async with _ro_db(db_path) as conn:
        try:
            cursor = await conn.execute(
                "SELECT * FROM briefings ORDER BY created_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        except Exception:
            return None  # table doesn't exist yet


async def get_briefing_history(db_path: str, limit: int = 10) -> list[dict]:
    """Return past briefings (most recent first)."""
    async with _ro_db(db_path) as conn:
        try:
            cursor = await conn.execute(
                """SELECT id, briefing_type, synthesis, model_used, tokens_used, created_at
                   FROM briefings ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
            return [dict(r) for r in await cursor.fetchall()]
        except Exception:
            return []  # table doesn't exist yet


async def get_last_briefing_time(db_path: str) -> str | None:
    """Return the created_at of the most recent briefing."""
    async with _ro_db(db_path) as conn:
        try:
            cursor = await conn.execute("SELECT MAX(created_at) FROM briefings")
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            return None  # table doesn't exist yet


async def store_briefing(
    db_path: str,
    briefing_type: str,
    raw_data: str,
    synthesis: str,
    model_used: str,
    tokens_used: int | None = None,
    created_at: str | None = None,
) -> int:
    """Insert a briefing and return its id."""
    from datetime import datetime, timezone as tz

    if created_at is None:
        created_at = datetime.now(tz.utc).isoformat()
    async with _rw_db(db_path) as conn:
        cursor = await conn.execute(
            """INSERT INTO briefings (briefing_type, raw_data, synthesis, model_used, tokens_used, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (briefing_type, raw_data, synthesis, model_used, tokens_used, created_at),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_available_categories(db_path: str) -> list[dict]:
    """Return distinct categories from recent snapshots (last 24h)."""
    async with _ro_db(db_path) as conn:
        cursor = await conn.execute(
            """SELECT DISTINCT category_id, name
               FROM category_snapshots
               WHERE snapshot_at > datetime('now', '-1 day')
               ORDER BY name""",
        )
        rows = await cursor.fetchall()
        return [{"category_id": row[0], "name": row[1]} for row in rows]


# ---------------------------------------------------------------------------
# Paper trading queries
# ---------------------------------------------------------------------------


async def get_trading_positions(db_path: str) -> list[dict]:
    """Open paper trades enriched with current prices from price_cache."""
    async with _ro_db(db_path) as db:
        try:
            return await _get_trading_positions_inner(db)
        except Exception:
            return []  # table doesn't exist yet


async def _get_trading_positions_inner(db) -> list[dict]:
    """Inner implementation split out for M11 try/except wrapper."""
    cursor = await db.execute(
        """SELECT id, token_id, symbol, name, chain, signal_type, signal_data,
                  entry_price, amount_usd, quantity,
                  tp_price, sl_price, tp_pct, sl_pct,
                  peak_price, peak_pct,
                  checkpoint_1h_pct, checkpoint_6h_pct,
                  checkpoint_24h_pct, checkpoint_48h_pct,
                  opened_at,
                  leg_1_filled_at,
                  leg_2_filled_at,
                  remaining_qty,
                  realized_pnl_usd,
                  floor_armed,
                  would_be_live,
                  actionable,
                  actionability_reason,
                  actionability_version
           FROM paper_trades
           WHERE status = 'open'
           ORDER BY opened_at DESC"""
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    # Enrich with current prices from price_cache
    token_ids = [r["token_id"] for r in rows]
    if token_ids:
        placeholders = ",".join("?" * len(token_ids))
        pcursor = await db.execute(
            f"SELECT coin_id, current_price FROM price_cache WHERE coin_id IN ({placeholders})",
            token_ids,
        )
        prices = {r["coin_id"]: r["current_price"] for r in await pcursor.fetchall()}

        for r in rows:
            cp = prices.get(r["token_id"])
            if cp and r["entry_price"]:
                r["current_price"] = cp
                # Price-only delta from entry — useful for UX badges but
                # NOT a portfolio metric on partially-filled ladder trades.
                r["unrealized_pnl_pct"] = round(
                    ((cp - r["entry_price"]) / r["entry_price"]) * 100, 2
                )
                # Post-leg-1 ladder trades hold only remaining_qty at current price;
                # quantity is the initial size and overstates the open slice.
                open_qty = (
                    r["remaining_qty"]
                    if r.get("remaining_qty") is not None
                    else r["quantity"]
                )
                r["unrealized_pnl_usd"] = round((cp - r["entry_price"]) * open_qty, 2)
                # Total PnL = realized (from any closed ladder legs) +
                # unrealized on the still-open remainder. Reconciled against
                # the trader's original capital so PnL$ and PnL% always tell
                # the same story (closes UI bug where +X% price move +
                # post-leg-1 partial fill produced misleading numbers).
                realized = r.get("realized_pnl_usd") or 0.0
                total_pnl_usd = realized + r["unrealized_pnl_usd"]
                r["total_pnl_usd"] = round(total_pnl_usd, 2)
                r["total_pnl_pct"] = (
                    round(total_pnl_usd / r["amount_usd"] * 100, 2)
                    if r["amount_usd"]
                    else None
                )
            else:
                r["current_price"] = None
                r["unrealized_pnl_pct"] = None
                r["unrealized_pnl_usd"] = None
                r["total_pnl_usd"] = None
                r["total_pnl_pct"] = None

    return rows


def _parse_iso_dt(value: str | None) -> datetime | None:
    """Best-effort ISO8601 parser for DB timestamps.

    Accepts `...Z` by rewriting to `...+00:00`. Returns timezone-aware UTC
    datetimes when possible; returns None on parse failure.
    """
    if not value:
        return None
    try:
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _live_candidates_meta(
    *,
    limit: int,
    window_hours: int,
    open_trades_scanned: int,
    rows_returned: int,
    generated_at: str,
) -> dict:
    return {
        "read_only": True,
        "not_trade_advice": True,
        "experimental": True,
        "generated_at": generated_at,
        "window_hours": window_hours,
        "limit": limit,
        "open_trades_scanned": open_trades_scanned,
        "rows_returned": rows_returned,
    }


async def get_live_candidates(
    db_path: str, *, limit: int = 20, window_hours: int = 36
) -> dict:
    """Read-only per-token "live candidates" cockpit.

    V1 is deterministic and visibility-only: no writes, no execution, no LLM.
    Primary cohort is `paper_trades.status='open'`. Optional context looks back
    `window_hours` for recent trade ids + surfaces.

    Returns an envelope ``{"meta": {...}, "rows": [...]}`` so consumers always
    see the read-only / not-trade-advice flags + row counts, even when ``rows``
    is empty.
    """
    limit = max(1, min(int(limit), 50))
    window_hours = max(6, min(int(window_hours), 72))

    now = datetime.now(timezone.utc)
    generated_at = now.isoformat()
    cutoff_iso = (now - timedelta(hours=window_hours)).isoformat()

    disclaimer = "read-only labels; not trading advice; triggers no actions"
    stale_warn_age = timedelta(hours=1)
    stale_hard_age = timedelta(hours=2)
    log = structlog.get_logger()

    def _envelope(rows: list[dict], scanned: int) -> dict:
        return {
            "meta": _live_candidates_meta(
                limit=limit,
                window_hours=window_hours,
                open_trades_scanned=scanned,
                rows_returned=len(rows),
                generated_at=generated_at,
            ),
            "rows": rows,
        }

    async with _ro_db(db_path) as db:
        try:
            # NOTE: `opened_at` is stored as TEXT; historical rows may contain
            # dirty / non-ISO values. Keep the bounded SQL scan aligned with
            # the public contract before LIMIT applies: parseable timestamps
            # first, newest first, deterministic id tie-break.
            scan_cap = max(limit * 20, 400)
            cursor = await db.execute(
                """SELECT id, token_id, symbol, name, chain,
                          signal_type, entry_price, opened_at,
                          would_be_live, actionable
                     FROM paper_trades
                    WHERE status = 'open'
                    ORDER BY datetime(opened_at) IS NULL ASC,
                             datetime(opened_at) DESC,
                             id DESC
                    LIMIT ?""",
                (scan_cap,),
            )
            open_rows = await cursor.fetchall()
        except aiosqlite.OperationalError:
            log.warning(
                "live_candidates_empty_cohort",
                reason="paper_trades_table_missing",
                rows_returned=0,
                window_hours=window_hours,
            )
            return _envelope([], 0)

        def _open_row_sort_key(r: dict) -> tuple:
            dt = _parse_iso_dt(r.get("opened_at"))
            # Null / unparseable opened_at sorts last for selection.
            is_missing = dt is None
            ts = dt.timestamp() if dt else 0.0
            # opened_at desc, id desc
            return (is_missing, -ts, -int(r["id"]))

        open_rows_sorted = sorted([dict(x) for x in open_rows], key=_open_row_sort_key)

        open_primary_by_token: dict[str, dict] = {}
        open_ids_by_token: dict[str, list[int]] = {}
        token_order: list[str] = []
        for r in open_rows_sorted:
            token_id = r["token_id"]
            open_ids_by_token.setdefault(token_id, []).append(int(r["id"]))
            if token_id not in open_primary_by_token:
                open_primary_by_token[token_id] = dict(r)
                token_order.append(token_id)
                if len(token_order) >= limit:
                    break

        if not token_order:
            log.warning(
                "live_candidates_empty_cohort",
                reason="no_open_trades",
                rows_returned=0,
                open_trades_scanned=len(open_rows),
                window_hours=window_hours,
            )
            return _envelope([], len(open_rows))

        # Recent context: ids + surfaces within the lookback window, restricted
        # to the token_ids already in the open set (bounded).
        surfaces_by_token: dict[str, set[str]] = {
            t: {open_primary_by_token[t]["signal_type"]} for t in token_order
        }
        recent_ids_by_token: dict[str, list[int]] = {t: [] for t in token_order}
        placeholders = ",".join("?" * len(token_order))
        try:
            cursor = await db.execute(
                f"""SELECT id, token_id, signal_type
                      FROM paper_trades
                     WHERE token_id IN ({placeholders})
                       AND opened_at >= ?
                     ORDER BY opened_at DESC, id DESC""",
                (*token_order, cutoff_iso),
            )
            recent_rows = await cursor.fetchall()
            for rr in recent_rows:
                tid = rr["token_id"]
                if tid in recent_ids_by_token:
                    recent_ids_by_token[tid].append(int(rr["id"]))
                    st = rr["signal_type"]
                    if st:
                        surfaces_by_token[tid].add(st)
        except aiosqlite.OperationalError:
            # If opened_at column is missing (unexpected) or table missing, treat
            # as "no context" rather than failing the cockpit.
            pass

        # Batch-fetch price snapshots (best-effort join on token_id == coin_id).
        price_by_coin: dict[str, dict] = {}
        try:
            cursor = await db.execute(
                f"""SELECT coin_id, current_price, market_cap, price_change_24h, updated_at
                      FROM price_cache
                     WHERE coin_id IN ({placeholders})""",
                tuple(token_order),
            )
            for pr in await cursor.fetchall():
                price_by_coin[pr["coin_id"]] = dict(pr)
        except aiosqlite.OperationalError:
            # price_cache missing -> everything is data_insufficient.
            price_by_coin = {}

        # Optional predictions enrichment (latest per coin_id).
        pred_by_coin: dict[str, dict] = {}
        try:
            cursor = await db.execute(
                f"""
                SELECT coin_id, narrative_fit_score, counter_risk_score, counter_flags, predicted_at
                  FROM (
                        SELECT coin_id, narrative_fit_score, counter_risk_score, counter_flags, predicted_at, id,
                               ROW_NUMBER() OVER (PARTITION BY coin_id ORDER BY predicted_at DESC, id DESC) AS rn
                          FROM predictions
                         WHERE coin_id IN ({placeholders})
                       )
                 WHERE rn = 1
                """,
                tuple(token_order),
            )
            for pr in await cursor.fetchall():
                d = dict(pr)
                raw = d.get("counter_flags")
                items: list = []
                if raw:
                    try:
                        decoded = json.loads(raw)
                        if isinstance(decoded, list):
                            items = [
                                x for x in decoded if isinstance(x, (dict, str))
                            ]
                    except Exception:
                        items = []
                d["counter_flags"] = items
                pred_by_coin[d["coin_id"]] = d
        except aiosqlite.OperationalError as e:
            msg = str(e).lower()
            # Optional enrichment: degrade gracefully on missing table/columns,
            # or in environments without window-function support.
            if (
                ("no such table" in msg and "predictions" in msg)
                or "no such column" in msg
                or "no such function" in msg
            ):
                pred_by_coin = {}
            else:
                raise

        # Optional chain_matches enrichment (latest per token_id).
        chain_by_token: dict[str, dict] = {}
        try:
            cursor = await db.execute(
                f"""
                SELECT token_id, pipeline, pattern_name, completed_at
                  FROM (
                        SELECT token_id, pipeline, pattern_name, completed_at, id,
                               ROW_NUMBER() OVER (PARTITION BY token_id ORDER BY completed_at DESC, id DESC) AS rn
                          FROM chain_matches
                         WHERE token_id IN ({placeholders})
                       )
                 WHERE rn = 1
                """,
                tuple(token_order),
            )
            for cr in await cursor.fetchall():
                chain_by_token[cr["token_id"]] = dict(cr)
        except aiosqlite.OperationalError as e:
            msg = str(e).lower()
            if (
                ("no such table" in msg and "chain_matches" in msg)
                or "no such column" in msg
                or "no such function" in msg
            ):
                chain_by_token = {}
            else:
                raise

    def _entry_quality(pct: float | None) -> str:
        if pct is None:
            return "data_insufficient"
        if pct < -10.0:
            return "already_faded"
        if pct > 25.0:
            return "already_ran"
        if -2.0 <= pct <= 8.0:
            return "fresh_entry"
        if -6.0 <= pct <= 15.0:
            return "acceptable_pullback"
        # No "borderline" label in V1: map remaining ranges to the nearest
        # conservative bucket so the UI doesn't show "data_insufficient" for
        # a perfectly valid price move.
        if pct < -6.0:
            return "already_faded"
        return "already_ran"

    results: list[dict] = []
    for token_id in token_order:
        base = open_primary_by_token[token_id]
        price = price_by_coin.get(token_id)
        pred = pred_by_coin.get(token_id)
        chain = chain_by_token.get(token_id)

        inclusion_reasons: list[str] = ["open_paper_trade"]
        risk_reasons: list[str] = []

        actionable = base.get("actionable")
        would_be_live = base.get("would_be_live")

        entry_price = base.get("entry_price")
        entry_price = float(entry_price) if entry_price is not None else None
        opened_at = base.get("opened_at")
        opened_dt = _parse_iso_dt(opened_at)
        if opened_dt is None:
            risk_reasons.append("opened_at_unparseable")
            opened_at = None
        else:
            opened_at = opened_dt.isoformat()

        current_price = None
        market_cap = None
        price_change_24h = None
        price_updated_at = None
        price_is_stale = False
        price_age = None
        if price:
            current_price = price.get("current_price")
            market_cap = price.get("market_cap")
            price_change_24h = price.get("price_change_24h")
            price_updated_at = price.get("updated_at")
            upd_dt = _parse_iso_dt(price_updated_at)
            if upd_dt is None:
                risk_reasons.append("price_timestamp_unparseable")
                price_updated_at = None
            else:
                price_updated_at = upd_dt.isoformat()
                price_age = now - upd_dt
                price_is_stale = price_age > stale_warn_age
                if price_is_stale:
                    risk_reasons.append("price_is_stale")
        else:
            risk_reasons.append("no_price_snapshot_for_token_id")

        pct_from_entry = None
        if current_price is not None and entry_price and entry_price > 0:
            delta = (float(current_price) - entry_price) / entry_price * 100
            pct_from_entry = round(delta, 2)
        elif entry_price is None or entry_price <= 0:
            risk_reasons.append("entry_price_missing_or_invalid")

        eq = _entry_quality(pct_from_entry)
        if price_age is not None and price_age > stale_hard_age:
            eq = "too_stale"

        verdict = "watch"
        if price_age is not None and price_age > stale_hard_age:
            verdict = "data_insufficient"
        elif any(
            r
            in risk_reasons
            for r in (
                "no_price_snapshot_for_token_id",
                "price_timestamp_unparseable",
                "opened_at_unparseable",
                "entry_price_missing_or_invalid",
            )
        ):
            verdict = "data_insufficient"
        elif actionable == 0:
            verdict = "blocked"
            risk_reasons.append("not_actionable")
        elif actionable is None:
            verdict = "data_insufficient"
            risk_reasons.append("actionable_null_pre_cutover")
        elif actionable == 1 and would_be_live == 1 and eq in (
            "fresh_entry",
            "acceptable_pullback",
        ):
            verdict = "candidate_review"
        else:
            verdict = "watch"

        if actionable == 1:
            inclusion_reasons.append("actionable=1")
        elif actionable == 0:
            inclusion_reasons.append("actionable=0")
        elif actionable is None:
            inclusion_reasons.append("actionable=null")

        if would_be_live == 1:
            inclusion_reasons.append("would_be_live=1")
        elif would_be_live == 0:
            risk_reasons.append("would_be_live=0")

        counter_flags = pred.get("counter_flags") if pred else []
        narrative_fit_score = pred.get("narrative_fit_score") if pred else None
        counter_risk_score = pred.get("counter_risk_score") if pred else None
        if counter_risk_score is not None:
            risk_reasons.append("counter_risk_present_display_only_v1")

        results.append(
            {
                "disclaimer": disclaimer,
                "token_id": token_id,
                "symbol": base.get("symbol"),
                "name": base.get("name"),
                "chain": base.get("chain"),
                "open_trade_ids": sorted(open_ids_by_token.get(token_id, []), reverse=True),
                "recent_trade_ids": sorted(recent_ids_by_token.get(token_id, []), reverse=True),
                "surfaces": sorted(surfaces_by_token.get(token_id, set())),
                "actionable": actionable,
                "would_be_live": would_be_live,
                "opened_at": opened_at,
                "entry_price": entry_price,
                "pct_from_entry": pct_from_entry,
                "current_price": current_price,
                "market_cap": market_cap,
                "price_change_24h": price_change_24h,
                "price_updated_at": price_updated_at,
                "price_is_stale": bool(price_is_stale),
                "narrative_fit_score": narrative_fit_score,
                "counter_risk_score": counter_risk_score,
                "counter_flags": counter_flags or [],
                "latest_chain_match": chain,
                "entry_quality": eq,
                "verdict": verdict,
                "inclusion_reasons": inclusion_reasons,
                "risk_reasons": risk_reasons,
            }
        )

    verdict_rank = {
        "candidate_review": 0,
        "watch": 1,
        "blocked": 2,
        "data_insufficient": 3,
    }

    def _sort_key(r: dict):
        dt = _parse_iso_dt(r.get("opened_at"))
        # opened_at null/unparseable sorts last within the verdict bucket.
        is_missing = dt is None
        ts = dt.timestamp() if dt else 0.0
        token_id = r.get("token_id") or ""
        return (verdict_rank.get(r.get("verdict"), 9), is_missing, -ts, token_id)

    results.sort(key=_sort_key)
    sliced = results[:limit]
    log.info(
        "live_candidates_returned",
        open_trades_scanned=len(open_rows),
        unique_tokens=len(token_order),
        rows_returned=len(sliced),
        window_hours=window_hours,
    )
    return _envelope(sliced, len(open_rows))


async def get_trading_history(
    db_path: str, limit: int = 50, offset: int = 0, actionability: str = "all"
) -> list[dict]:
    """Closed paper trades, paginated."""
    async with _ro_db(db_path) as db:
        try:
            filter_sql, filter_params = _actionability_filter_sql(actionability)
            cursor = await db.execute(
                f"""SELECT id, token_id, symbol, name, chain, signal_type, signal_data,
                          entry_price, exit_price, amount_usd, quantity,
                          pnl_usd, pnl_pct, exit_reason, status,
                          peak_price, peak_pct,
                          checkpoint_1h_pct, checkpoint_6h_pct,
                          checkpoint_24h_pct, checkpoint_48h_pct,
                          opened_at, closed_at,
                          would_be_live,
                          actionable,
                          actionability_reason,
                          actionability_version
                   FROM paper_trades
                   WHERE status != 'open'
                     {filter_sql}
                   ORDER BY closed_at DESC
                   LIMIT ? OFFSET ?""",
                (*filter_params, limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []  # table doesn't exist yet


async def get_outcomes_by_token_ids(
    db_path: str, token_ids: list[str]
) -> dict[str, dict]:
    """Most-recent paper_trade per token_id, returned as {token_id: outcome}.

    Read-only join surface for the Top Gainers / candidate views to render
    "this candidate has a linked paper_trade" badges without exposing full
    trade history. Returns {} for an empty input list.

    Per-token outcome shape:
        paper_trade_id: int
        status: 'open' | 'closed_tp' | 'closed_sl' | 'closed_trail' | ...
        actionable: 1 | 0 | None
        actionability_reason: str | None
        actionability_version: str | None
        pnl_usd: float | None  (NULL while open)
        opened_at: ISO timestamp

    Tokens without any paper_trade row are simply absent from the returned
    dict (caller must default to None / "no trade yet" badge). Per-token
    selection uses ROW_NUMBER OVER (PARTITION BY ... ORDER BY opened_at DESC)
    so the most-recent trade wins regardless of open/closed status.
    """
    if not token_ids:
        return {}
    async with _ro_db(db_path) as db:
        try:
            placeholders = ",".join("?" for _ in token_ids)
            cursor = await db.execute(
                f"""
                SELECT token_id, paper_trade_id, status, actionable,
                       actionability_reason, actionability_version,
                       pnl_usd, opened_at
                FROM (
                    SELECT token_id,
                           id AS paper_trade_id,
                           status,
                           actionable,
                           actionability_reason,
                           actionability_version,
                           pnl_usd,
                           opened_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY token_id
                               ORDER BY opened_at DESC, id DESC
                           ) AS rn
                    FROM paper_trades
                    WHERE token_id IN ({placeholders})
                )
                WHERE rn = 1
                """,
                tuple(token_ids),
            )
            rows = await cursor.fetchall()
            return {row["token_id"]: dict(row) for row in rows}
        except Exception:
            return {}


def _actionability_filter_sql(actionability: str) -> tuple[str, tuple]:
    """Return SQL fragment for actionability state filter.

    API validation constrains values before this helper is called. The fallback
    to no filter keeps direct internal callers backwards-compatible.
    """
    if actionability == "actionable":
        return "AND actionable = 1", ()
    if actionability == "exploratory":
        return "AND actionable = 0", ()
    if actionability == "unknown":
        return "AND actionable IS NULL", ()
    return "", ()


async def get_trading_history_count(db_path: str, actionability: str = "all") -> int:
    """Total count of closed paper trades (status != 'open').

    Read by /api/trading/history/count for frontend pagination math.
    Mirrors the WHERE clause of get_trading_history exactly so totals
    line up with the paginated rows.
    """
    async with _ro_db(db_path) as db:
        try:
            filter_sql, filter_params = _actionability_filter_sql(actionability)
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM paper_trades WHERE status != 'open' {filter_sql}",
                filter_params,
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0  # table doesn't exist yet


def _empty_actionability_summary(days: int) -> dict:
    return {
        "window_days": days,
        "open_counts": {
            "actionable": 0,
            "exploratory": 0,
            "unknown": 0,
            "total": 0,
        },
        "closed_cohorts": [
            _empty_actionability_cohort("actionable"),
            _empty_actionability_cohort("exploratory"),
            _empty_actionability_cohort("unknown"),
        ],
        "top_reasons": [],
        "policy_note": (
            "Actionability is metadata only. Exploratory paper trades are not "
            "suppressed in this view."
        ),
    }


def _empty_actionability_cohort(state: str) -> dict:
    return {
        "state": state,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_usd": 0.0,
        "avg_pnl_pct": 0.0,
        "win_rate_pct": 0.0,
    }


def _actionability_state(value) -> str:
    if value == 1:
        return "actionable"
    if value == 0:
        return "exploratory"
    return "unknown"


async def get_trading_actionability_summary(db_path: str, days: int = 7) -> dict:
    """Read-only actionability rollup for the Trading dashboard."""
    if days < 1:
        days = 1
    window = f"-{days} days"
    summary = _empty_actionability_summary(days)

    async with _ro_db(db_path) as db:
        try:
            cursor = await db.execute(
                """SELECT actionable, COUNT(*) AS n
                   FROM paper_trades
                   WHERE status = 'open'
                   GROUP BY actionable"""
            )
            open_rows = await cursor.fetchall()

            cursor = await db.execute(
                """SELECT actionable,
                          COUNT(*) AS trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) AS losses,
                          COALESCE(SUM(pnl_usd), 0) AS pnl,
                          COALESCE(AVG(pnl_pct), 0) AS avg_pct
                   FROM paper_trades
                   WHERE status != 'open'
                     AND closed_at >= datetime('now', ?)
                   GROUP BY actionable""",
                (window,),
            )
            closed_rows = await cursor.fetchall()

            cursor = await db.execute(
                """SELECT COALESCE(actionability_reason, 'unknown') AS reason,
                          COUNT(*) AS trades,
                          SUM(CASE WHEN status != 'open' THEN 1 ELSE 0 END) AS closed_trades,
                          COALESCE(SUM(CASE WHEN status != 'open' THEN pnl_usd ELSE 0 END), 0)
                              AS closed_pnl
                   FROM paper_trades
                   WHERE actionability_reason IS NOT NULL
                     AND (
                       (status = 'open' AND opened_at >= datetime('now', ?))
                       OR (status != 'open' AND closed_at >= datetime('now', ?))
                     )
                   GROUP BY actionability_reason
                   ORDER BY trades DESC, reason ASC
                   LIMIT 10""",
                (window, window),
            )
            reason_rows = await cursor.fetchall()
        except Exception:
            return summary

    for row in open_rows:
        state = _actionability_state(row["actionable"])
        n = int(row["n"] or 0)
        summary["open_counts"][state] = n
        summary["open_counts"]["total"] += n

    by_state = {row["state"]: row for row in summary["closed_cohorts"]}
    for row in closed_rows:
        state = _actionability_state(row["actionable"])
        trades = int(row["trades"] or 0)
        wins = int(row["wins"] or 0)
        by_state[state].update(
            {
                "trades": trades,
                "wins": wins,
                "losses": int(row["losses"] or 0),
                "total_pnl_usd": round(row["pnl"] or 0, 2),
                "avg_pnl_pct": round(row["avg_pct"] or 0, 2),
                "win_rate_pct": round((wins / trades) * 100, 1)
                if trades > 0
                else 0.0,
            }
        )

    summary["top_reasons"] = [
        {
            "reason": row["reason"],
            "trades": int(row["trades"] or 0),
            "closed_trades": int(row["closed_trades"] or 0),
            "closed_pnl_usd": round(row["closed_pnl"] or 0, 2),
        }
        for row in reason_rows
    ]
    return summary


async def get_trading_stats(db_path: str, days: int = 7) -> dict:
    """Aggregate paper trading PnL stats."""
    _empty_stats = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl_usd": 0,
        "avg_pnl_pct": 0,
        "best_trade": None,
        "worst_trade": None,
        "win_rate_pct": 0,
        "open_positions": 0,
        "open_exposure": 0,
    }
    async with _ro_db(db_path) as db:
        try:
            cursor = await db.execute(
                """SELECT
                     COUNT(*) as total_trades,
                     SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                     SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                     COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
                     COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                     MAX(pnl_usd) as best_trade,
                     MIN(pnl_usd) as worst_trade
                   FROM paper_trades
                   WHERE status != 'open'
                     AND closed_at >= datetime('now', ?)""",
                (f"-{days} days",),
            )
            row = await cursor.fetchone()
        except Exception:
            return _empty_stats  # table doesn't exist yet
        total = row[0] or 0
        wins = row[1] or 0

        # Open positions count
        cursor2 = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_usd), 0) FROM paper_trades WHERE status = 'open'"
        )
        open_row = await cursor2.fetchone()

        return {
            "total_trades": total,
            "wins": wins,
            "losses": row[2] or 0,
            "total_pnl_usd": round(row[3] or 0, 2),
            "avg_pnl_pct": round(row[4] or 0, 2),
            "best_trade": row[5],
            "worst_trade": row[6],
            "win_rate_pct": round((wins / total) * 100, 1) if total > 0 else 0,
            "open_positions": open_row[0] or 0,
            "open_exposure": round(open_row[1] or 0, 2),
        }


async def get_trading_stats_by_signal(db_path: str, days: int = 7) -> dict:
    """Paper trading PnL breakdown by signal type."""
    async with _ro_db(db_path) as db:
        try:
            cursor = await db.execute(
                """SELECT signal_type,
                     COUNT(*) as trades,
                     COALESCE(SUM(pnl_usd), 0) as pnl,
                     SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins
                   FROM paper_trades
                   WHERE status != 'open'
                     AND closed_at >= datetime('now', ?)
                   GROUP BY signal_type""",
                (f"-{days} days",),
            )
            rows = await cursor.fetchall()
        except Exception:
            return {}  # table doesn't exist yet
        result = {}
        for row in rows:
            total = row[1]
            w = row[3] or 0
            result[row[0]] = {
                "trades": total,
                "pnl": round(row[2], 2),
                "win_rate": round((w / total) * 100, 1) if total > 0 else 0,
            }
        return result


# Tier 1a/2a/2b enumerated types per scout.trading.live_eligibility.
# Kept in sync with matches_tier_1_or_2(); a signal_type in this set is
# never structurally excluded — only Tier 1b (stack >= 3) can promote
# other signal_types into the eligible cohort.
_LIVE_ELIGIBLE_ENUMERATED_TYPES = ("chain_completed", "volume_spike", "gainers_early")


def _is_expected_cohort_oe(err: "aiosqlite.OperationalError") -> bool:
    """True iff the OperationalError is the expected pre-migration / pre-writer
    snapshot shape (missing table or missing would_be_live / conviction_locked_stack
    column). Anything else — syntax errors, locked DB, renamed-but-still-present
    column — propagates so the dashboard 500s loudly instead of silently emptying
    the cohort view.

    Vector A N2 fold: narrow the catch to match the project's documented precedent
    (see get_tg_social_dlq below) per global CLAUDE.md
    feedback_resilience_layered_failure_modes.md ("every resilience addition must
    extend a visibility surface").
    """
    msg = str(err).lower()
    if "no such table" in msg and "paper_trades" in msg:
        return True
    if "no such column" in msg and (
        "would_be_live" in msg or "conviction_locked_stack" in msg
    ):
        return True
    return False


async def get_trading_stats_by_signal_cohort(db_path: str, days: int = 7) -> dict:
    """Side-by-side PnL/win-rate by signal_type for full vs live-eligible cohorts.

    Powers the dashboard cohort-toggle view (see `tasks/plan_dashboard_live_eligible_view.md`).
    Read-only. No behavior change.

    Returns three lists:
      - full_cohort: every signal_type, all `would_be_live` values
      - eligible_cohort: every signal_type, restricted to `would_be_live=1`
      - excluded_signal_types: signal_types whose eligible-subset is structurally
        empty (max observed conviction_locked_stack < 3 AND not in Tier 1a/2a/2b).
        Derived from data — list updates as new signal_types ship.

    The derivation matters: excluding by hardcoded list would silently miss new
    structurally-non-stackable signals; deriving from MAX(conviction_locked_stack)
    catches them. The trade-off — a signal that has never coincidentally stacked
    to 3 will be flagged as excluded even if it theoretically could; the operator
    sees the derivation in the reason string and can override.
    """
    if days < 1:
        days = 1
    window = f"-{days} days"
    async with _ro_db(db_path) as db:
        # Full cohort (existing get_trading_stats_by_signal pattern with avg_pnl_pct added).
        # symbols field added for the dashboard's ticker-in-aggregate display
        # (see tasks/plan_dashboard_live_eligible_view.md follow-up + plan
        # at ~/.claude/plans/fluttering-riding-kernighan.md). GROUP_CONCAT order
        # is SQLite-unspecified; Python sorts after split for deterministic UI.
        try:
            cursor = await db.execute(
                """SELECT signal_type,
                          COUNT(*)                                AS trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                          COALESCE(SUM(pnl_usd), 0)               AS pnl,
                          COALESCE(AVG(pnl_pct), 0)               AS avg_pct,
                          GROUP_CONCAT(symbol, '|')               AS symbols
                     FROM paper_trades
                    WHERE status != 'open'
                      AND closed_at >= datetime('now', ?)
                    GROUP BY signal_type""",
                (window,),
            )
            full_rows = await cursor.fetchall()
        except aiosqlite.OperationalError as e:
            if not _is_expected_cohort_oe(e):
                structlog.get_logger().warning("cohort_full_query_oe", err=str(e))
                raise
            full_rows = []

        # Eligible cohort — same shape, filtered to would_be_live=1.
        try:
            cursor = await db.execute(
                """SELECT signal_type,
                          COUNT(*)                                AS trades,
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                          COALESCE(SUM(pnl_usd), 0)               AS pnl,
                          COALESCE(AVG(pnl_pct), 0)               AS avg_pct,
                          GROUP_CONCAT(symbol, '|')               AS symbols
                     FROM paper_trades
                    WHERE status != 'open'
                      AND closed_at >= datetime('now', ?)
                      AND would_be_live = 1
                    GROUP BY signal_type""",
                (window,),
            )
            eligible_rows = await cursor.fetchall()
        except aiosqlite.OperationalError as e:
            if not _is_expected_cohort_oe(e):
                structlog.get_logger().warning("cohort_eligible_query_oe", err=str(e))
                raise
            eligible_rows = []

        # Excluded list — derived from ALL-time history (not the days window),
        # because structural eligibility is a property of the signal_type, not
        # the rolling window. A signal that hasn't stacked in 7d but did stack
        # 30d ago is NOT structurally excluded.
        placeholders = ",".join(["?"] * len(_LIVE_ELIGIBLE_ENUMERATED_TYPES))
        try:
            cursor = await db.execute(
                f"""SELECT signal_type,
                           MAX(COALESCE(conviction_locked_stack, 0)) AS max_stack,
                           COUNT(*)                                  AS lifetime_trades
                      FROM paper_trades
                     WHERE signal_type NOT IN ({placeholders})
                     GROUP BY signal_type
                    HAVING max_stack < 3""",
                _LIVE_ELIGIBLE_ENUMERATED_TYPES,
            )
            excluded_rows = await cursor.fetchall()
        except aiosqlite.OperationalError as e:
            if not _is_expected_cohort_oe(e):
                structlog.get_logger().warning("cohort_excluded_query_oe", err=str(e))
                raise
            excluded_rows = []

    def _to_row(r):
        trades = r[1] or 0
        wins = r[2] or 0
        # r[5] is GROUP_CONCAT(symbol, '|') — split + sort for deterministic
        # UI display order. Empty symbols (NULL or empty string) filtered out
        # so the frontend never renders a blank ticker chip.
        raw_symbols = r[5] if len(r) > 5 else None
        symbols = (
            sorted({s for s in raw_symbols.split("|") if s}) if raw_symbols else []
        )
        return {
            "signal_type": r[0],
            "trades": trades,
            "wins": wins,
            "total_pnl_usd": round(r[3] or 0, 2),
            "win_rate_pct": round((wins / trades) * 100, 1) if trades > 0 else 0,
            "avg_pnl_pct": round(r[4] or 0, 2),
            "symbols": symbols,
        }

    excluded = [
        {
            "signal_type": r[0],
            "max_observed_stack": r[1] or 0,
            "lifetime_trades": r[2] or 0,
            "reason": (
                f"max stack achieved in lifetime: {r[1] or 0} (need >=3 for live "
                "eligibility); single-source signal — eligible subset is "
                "structurally empty, not small. Still paper-trading."
            ),
        }
        for r in excluded_rows
    ]

    # chain_completed annotation (Vector B M-CRIT-2 fold): Tier 1a entry means
    # full and eligible cohorts are nearly identical populations; divergence
    # verdicts are not informative. Surface this in the API response so the
    # UI can annotate the row.
    near_identical_cohorts = ["chain_completed"]

    return {
        "window_days": days,
        "full_cohort": [_to_row(r) for r in full_rows],
        "eligible_cohort": [_to_row(r) for r in eligible_rows],
        "excluded_signal_types": excluded,
        "near_identical_cohorts": near_identical_cohorts,
        "min_eligible_n_for_verdict": 10,
        "verdict_window_anchor": "writer-deployment 2026-05-11 + 28d = 2026-06-08",
        "small_n_caveat": (
            "Live-eligible cohort is typically 5-10% of paper-trade volume. "
            "Per-signal-type verdicts require eligible n >= 10 (otherwise "
            "INSUFFICIENT_DATA). Strong-pattern verdicts are exploratory, NOT "
            "confirmatory — family-wise FPR ~50% across 4 signal_types at "
            "projected n. Decision-locked at writer-deployment + 28d = "
            "2026-06-08. See tasks/plan_dashboard_live_eligible_view.md."
        ),
    }


# ---------------------------------------------------------------------------
# BL-066': TG-social dashboard gap-fill
# ---------------------------------------------------------------------------


async def get_x_alerts(db_path: str, limit: int = 80) -> dict:
    """Recent Hermes/xurl narrative alerts for the dashboard.

    Source table is ``narrative_alerts_inbound``. The Hermes side owns X
    collection/classification; the dashboard only renders the accepted inbound
    events so we don't introduce another X polling path.
    """
    safe_limit = max(1, min(limit, 200))
    async with _ro_db(db_path) as conn:
        try:
            cur = await conn.execute(
                """SELECT id, event_id, tweet_id, tweet_author, tweet_ts,
                          tweet_text, extracted_cashtag, extracted_ca,
                          extracted_chain, resolved_coin_id, narrative_theme,
                          urgency_signal, classifier_confidence,
                          classifier_version, received_at
                   FROM narrative_alerts_inbound
                   ORDER BY datetime(received_at) DESC, id DESC
                   LIMIT ?""",
                (safe_limit,),
            )
            rows = await cur.fetchall()

            stats_cur = await conn.execute("""SELECT
                         COUNT(*) AS alerts,
                         COUNT(DISTINCT tweet_author) AS unique_authors,
                         SUM(CASE WHEN COALESCE(extracted_ca, '') != ''
                                  THEN 1 ELSE 0 END) AS with_ca,
                         SUM(CASE WHEN COALESCE(extracted_cashtag, '') != ''
                                  THEN 1 ELSE 0 END) AS with_cashtag,
                         SUM(CASE WHEN COALESCE(resolved_coin_id, '') != ''
                                  THEN 1 ELSE 0 END) AS resolved,
                         AVG(classifier_confidence) AS avg_confidence
                   FROM narrative_alerts_inbound
                   WHERE datetime(received_at) >= datetime('now', '-24 hours')""")
            stats_row = await stats_cur.fetchone()
        except aiosqlite.OperationalError as e:
            msg = str(e)
            if "no such table" not in msg or "narrative_alerts_inbound" not in msg:
                raise
            structlog.get_logger().warning(
                "dashboard_x_alerts_table_missing_fallback",
                err=msg,
            )
            return {
                "stats_24h": {
                    "alerts": 0,
                    "unique_authors": 0,
                    "with_ca": 0,
                    "with_cashtag": 0,
                    "resolved": 0,
                    "avg_confidence": None,
                },
                "alerts": [],
            }

        def _tweet_url(author: str | None, tweet_id: str | None) -> str | None:
            if not author or not tweet_id:
                return None
            return f"https://x.com/{author}/status/{tweet_id}"

        async def _safe_fetchall(sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
            try:
                cur = await conn.execute(sql, params)
                return await cur.fetchall()
            except aiosqlite.OperationalError as exc:
                structlog.get_logger().debug(
                    "dashboard_x_alerts_outcome_source_unavailable",
                    err=str(exc),
                )
                return []

        # Per-request memoization caches. Many X alerts share the same
        # `extracted_cashtag` (multiple tweets about the same token).
        # Without these caches the endpoint ran 80 × N sequential sqlite
        # queries and timed out at >12s for limit=80 (verified 2026-05-19).
        #
        # entry_price_data (added 2026-05-21 per BL-NEW-DASHBOARD-X-ALERTS-TIMEOUT-FIX):
        # pre-loaded once per request as dict[coin_id, list[(ts, price)]]
        # covering the global window across all resolved rows. Replaces the
        # per-row 5-source-table BETWEEN queries that scaled at O(N rows
        # x 5 queries) and tripped the 5s frontend timeout at limit=80.
        symbol_cache: dict[str, tuple[str | None, str]] = {}
        current_price_cache: dict[str, float | None] = {}
        entry_price_data: dict[str, list[tuple[datetime, float]]] = {}

        async def _resolve_coin_id_for_outcome(row: aiosqlite.Row) -> tuple[str | None, str]:
            resolved = row["resolved_coin_id"]
            if resolved:
                return resolved, "resolved_coin_id"

            # Contract-match path removed 2026-05-19 per
            # BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-SCHEMA-ALIGN.
            # The previous branch queried `candidates.coingecko_id`, but
            # that column has never existed on the `candidates` table
            # (verified via PRAGMA table_info(candidates) on prod and via
            # the schema definition in scout/db.py CREATE TABLE
            # candidates — no ALTER ever added it). _safe_fetchall
            # silently caught the OperationalError on every request and
            # emitted `dashboard_x_alerts_outcome_source_unavailable`
            # log spam. All rows fell through to the cashtag / symbol
            # path anyway. Removing the dead branch is honest about the
            # current behavior; if a future fix wants contract→coin_id
            # resolution it must either (a) add the column to candidates
            # with a population path, or (b) use `second_wave_candidates`
            # which does carry coingecko_id (narrow population semantic).
            # See BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-SCHEMA-ALIGN.

            cashtag = row["extracted_cashtag"]
            symbol = (cashtag or "").strip().lstrip("$").upper()
            if not symbol:
                return None, "unresolved"
            if symbol in symbol_cache:
                return symbol_cache[symbol]

            symbol_rows: list[aiosqlite.Row] = []
            for table, time_col in (
                ("gainers_snapshots", "snapshot_at"),
                ("volume_history_cg", "recorded_at"),
                ("volume_spikes", "detected_at"),
                ("momentum_7d", "detected_at"),
            ):
                # Drop the `datetime()` wrapper on the indexed column —
                # ORDER BY column directly so SQLite can use existing
                # (coin_id, time_col) indexes. ISO-8601 strings sort
                # lexicographically the same as parsed datetimes.
                symbol_rows.extend(
                    await _safe_fetchall(
                        f"""SELECT DISTINCT coin_id
                            FROM {table}
                            WHERE UPPER(symbol) = ?
                              AND COALESCE(coin_id, '') != ''
                            ORDER BY {time_col} DESC
                            LIMIT 25""",
                        (symbol,),
                    )
                )

            coin_ids = sorted({r["coin_id"] for r in symbol_rows if r["coin_id"]})
            if len(coin_ids) == 1:
                result = (coin_ids[0], "unique_symbol")
            elif len(coin_ids) > 1:
                result = (None, "ambiguous_symbol")
            else:
                result = (None, "unresolved_symbol")
            symbol_cache[symbol] = result
            return result

        def _parse_ts(value: str | None) -> datetime | None:
            # NOTE on `received_at` shape heterogeneity: rows ingested via
            # the SQL DEFAULT (datetime('now')) at scout/db.py:3432
            # arrive as '2026-05-19 12:34:56' (space-separated, no tz, no
            # microseconds). Rows that flow through Python writers arrive
            # as ISO-T with `+00:00` suffix and microseconds. Python's
            # fromisoformat accepts both since 3.11; the explicit
            # tzinfo-stamp below normalizes the naive-UTC path so the
            # downstream isoformat() bound computation is consistent.
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                return None

        # Per-source-table query templates for batch entry-price preload.
        # `?` for the IN-list of coin_ids is filled at query time; the
        # window bounds are the union of all per-row 24h windows.
        entry_price_source_queries = (
            (
                "gainers_snapshots",
                """SELECT coin_id, snapshot_at AS ts, price_at_snapshot AS price
                   FROM gainers_snapshots
                   WHERE coin_id IN ({placeholders})
                     AND price_at_snapshot IS NOT NULL
                     AND snapshot_at BETWEEN ? AND ?""",
            ),
            (
                "losers_snapshots",
                """SELECT coin_id, snapshot_at AS ts, price_at_snapshot AS price
                   FROM losers_snapshots
                   WHERE coin_id IN ({placeholders})
                     AND price_at_snapshot IS NOT NULL
                     AND snapshot_at BETWEEN ? AND ?""",
            ),
            (
                "volume_history_cg",
                """SELECT coin_id, recorded_at AS ts, price AS price
                   FROM volume_history_cg
                   WHERE coin_id IN ({placeholders})
                     AND price IS NOT NULL
                     AND recorded_at BETWEEN ? AND ?""",
            ),
            (
                "volume_spikes",
                """SELECT coin_id, detected_at AS ts, price AS price
                   FROM volume_spikes
                   WHERE coin_id IN ({placeholders})
                     AND price IS NOT NULL
                     AND detected_at BETWEEN ? AND ?""",
            ),
            (
                "momentum_7d",
                """SELECT coin_id, detected_at AS ts, current_price AS price
                   FROM momentum_7d
                   WHERE coin_id IN ({placeholders})
                     AND current_price IS NOT NULL
                     AND detected_at BETWEEN ? AND ?""",
            ),
        )

        async def _preload_entry_price_data(
            coin_ids: list[str],
            window_lo: datetime,
            window_hi: datetime,
        ) -> None:
            """One-shot batch load of entry-price source rows for all
            resolved coin_ids over the union window. Mutates
            `entry_price_data` in place. Indexes (coin_id, time_col)
            on all 5 source tables make IN-list + BETWEEN fast.

            Per BL-NEW-DASHBOARD-X-ALERTS-TIMEOUT-FIX 2026-05-21:
            replaces O(N rows x 5 queries) with O(5 queries) total.
            """
            if not coin_ids:
                return
            placeholders = ",".join("?" * len(coin_ids))
            lo_iso = window_lo.isoformat()
            hi_iso = window_hi.isoformat()
            for _table, sql_template in entry_price_source_queries:
                sql = sql_template.format(placeholders=placeholders)
                params = tuple(coin_ids) + (lo_iso, hi_iso)
                for price_row in await _safe_fetchall(sql, params):
                    parsed_ts = _parse_ts(price_row["ts"])
                    price = price_row["price"]
                    if parsed_ts and price and price > 0:
                        entry_price_data.setdefault(
                            price_row["coin_id"], []
                        ).append((parsed_ts, float(price)))

        async def _price_at_alert(coin_id: str, received_at: str) -> float | None:
            """Closest-prior-snapshot if exists within ±24h of received_at,
            else earliest-after within window. Reads from prebuilt
            `entry_price_data` (no per-row SQL queries).
            """
            received_dt = _parse_ts(received_at)
            if received_dt is None:
                # received_at unparseable — preserve old behavior of
                # returning None so the caller's "no_entry_price" path
                # fires.
                return None
            window_lo = received_dt - timedelta(hours=24)
            window_hi = received_dt + timedelta(hours=24)
            all_sources = entry_price_data.get(coin_id) or []
            sources = [
                (ts, price)
                for ts, price in all_sources
                if window_lo <= ts <= window_hi
            ]
            if not sources:
                return None
            before = [(ts, price) for ts, price in sources if ts <= received_dt]
            if before:
                return sorted(before, key=lambda item: item[0], reverse=True)[0][1]
            return sorted(sources, key=lambda item: item[0])[0][1]

        async def _current_price(coin_id: str) -> float | None:
            # Per-request cache. Multiple alerts about the same token
            # share a single price_cache lookup.
            if coin_id in current_price_cache:
                return current_price_cache[coin_id]
            rows = await _safe_fetchall(
                """SELECT current_price
                   FROM price_cache
                   WHERE coin_id = ? AND current_price IS NOT NULL
                   LIMIT 1""",
                (coin_id,),
            )
            if rows and rows[0]["current_price"] and rows[0]["current_price"] > 0:
                price = float(rows[0]["current_price"])
                current_price_cache[coin_id] = price
                return price
            current_price_cache[coin_id] = None
            return None

        async def _outcome(row: aiosqlite.Row, resolved: tuple[str | None, str]) -> dict:
            investment = 300.0
            coin_id, status = resolved
            base = {
                "outcome_investment_usd": investment,
                "outcome_coin_id": coin_id,
                "entry_price_usd": None,
                "current_price_usd": None,
                "gain_pct_since_alert": None,
                "profit_usd_at_300": None,
                "outcome_status": status,
            }
            if not coin_id:
                return base

            entry = await _price_at_alert(coin_id, row["received_at"])
            current = await _current_price(coin_id)
            base["entry_price_usd"] = entry
            base["current_price_usd"] = current

            if entry is None:
                base["outcome_status"] = "no_entry_price"
                return base
            if current is None:
                base["outcome_status"] = "no_current_price"
                return base

            gain_pct = ((current - entry) / entry) * 100
            base["gain_pct_since_alert"] = round(gain_pct, 4)
            base["profit_usd_at_300"] = round(investment * (gain_pct / 100), 2)
            base["outcome_status"] = "priced"
            return base

        def _asset_link(row: aiosqlite.Row, outcome: dict) -> dict:
            ca = row["extracted_ca"]
            chain = row["extracted_chain"]
            if ca:
                if chain and chain != "coingecko":
                    return {
                        "asset_url": f"https://dexscreener.com/{chain}/{ca}",
                        "asset_url_source": "dexscreener_contract",
                    }
                return {
                    "asset_url": f"https://dexscreener.com/search?q={ca}",
                    "asset_url_source": "dexscreener_search",
                }

            coin_id = outcome.get("outcome_coin_id") or row["resolved_coin_id"]
            if coin_id:
                return {
                    "asset_url": f"https://www.coingecko.com/en/coins/{coin_id}",
                    "asset_url_source": "coingecko_resolved",
                }

            cashtag = row["extracted_cashtag"]
            symbol = (cashtag or "").strip().lstrip("$").upper()
            if symbol:
                return {
                    "asset_url": f"https://www.coingecko.com/en/search?query={symbol}",
                    "asset_url_source": outcome.get("outcome_status") or "coingecko_search",
                }

            return {
                "asset_url": None,
                "asset_url_source": outcome.get("outcome_status") or "unresolved",
            }

        # PASS 1: resolve coin_ids per row (cached via symbol_cache).
        # Collect resolved (coin_id, received_at) tuples so the batched
        # entry-price preload knows which coin_ids + window to fetch for.
        resolutions: list[tuple[str | None, str]] = []
        resolved_received_at: list[datetime] = []
        resolved_coin_ids: set[str] = set()
        for row in rows:
            res = await _resolve_coin_id_for_outcome(row)
            resolutions.append(res)
            coin_id_for_row, _status = res
            if coin_id_for_row:
                resolved_coin_ids.add(coin_id_for_row)
                parsed_received = _parse_ts(row["received_at"])
                if parsed_received is not None:
                    resolved_received_at.append(parsed_received)

        # Batched entry-price preload — one query per source table for
        # ALL coin_ids across the union of per-row 24h windows. Replaces
        # the per-row 5-source-table BETWEEN sweep that scaled at
        # O(N rows x 5 queries) and tripped the 5s frontend timeout at
        # limit=80 (verified prod 2026-05-21: 9.3s @ limit=80).
        if resolved_coin_ids and resolved_received_at:
            window_lo = min(resolved_received_at) - timedelta(hours=24)
            window_hi = max(resolved_received_at) + timedelta(hours=24)
            # Bound IN-list size to avoid SQLite SQLITE_MAX_VARIABLE_NUMBER
            # (default 999); each query consumes len(coin_ids)+2 binds.
            # In practice resolved_coin_ids fits in well under 100 even
            # at limit=200; the slice is defensive. Cap-hit emits a
            # structured warning so the silent-failure surface (rows
            # 501+ would silently get no_entry_price) is observable.
            preload_coin_ids = sorted(resolved_coin_ids)
            if len(preload_coin_ids) > 500:
                structlog.get_logger().warning(
                    "x_alerts_preload_coin_id_cap_hit",
                    n_resolved=len(preload_coin_ids),
                    cap=500,
                    n_truncated=len(preload_coin_ids) - 500,
                )
                preload_coin_ids = preload_coin_ids[:500]
            await _preload_entry_price_data(
                preload_coin_ids,
                window_lo,
                window_hi,
            )

        # PASS 2: build outcomes + asset links + response rows.
        alerts = []
        for row, resolved in zip(rows, resolutions):
            text = row["tweet_text"] or ""
            outcome = await _outcome(row, resolved)
            asset_link = _asset_link(row, outcome)
            alerts.append(
                {
                    "id": row["id"],
                    "event_id": row["event_id"],
                    "tweet_id": row["tweet_id"],
                    "tweet_author": row["tweet_author"],
                    "tweet_ts": row["tweet_ts"],
                    "tweet_url": _tweet_url(row["tweet_author"], row["tweet_id"]),
                    "text_preview": text[:240],
                    "extracted_cashtag": row["extracted_cashtag"],
                    "extracted_ca": row["extracted_ca"],
                    "extracted_chain": row["extracted_chain"],
                    "resolved_coin_id": row["resolved_coin_id"],
                    "narrative_theme": row["narrative_theme"],
                    "urgency_signal": row["urgency_signal"],
                    "classifier_confidence": row["classifier_confidence"],
                    "classifier_version": row["classifier_version"],
                    "received_at": row["received_at"],
                    **asset_link,
                    **outcome,
                }
            )

        avg = stats_row["avg_confidence"] if stats_row else None
        return {
            "stats_24h": {
                "alerts": (stats_row["alerts"] if stats_row else 0) or 0,
                "unique_authors": (stats_row["unique_authors"] if stats_row else 0)
                or 0,
                "with_ca": (stats_row["with_ca"] if stats_row else 0) or 0,
                "with_cashtag": (stats_row["with_cashtag"] if stats_row else 0) or 0,
                "resolved": (stats_row["resolved"] if stats_row else 0) or 0,
                "avg_confidence": round(avg, 3) if avg is not None else None,
            },
            "alerts": alerts,
        }


async def get_tg_social_dlq(db_path: str, limit: int = 20) -> list[dict]:
    """Recent tg_social DLQ entries, ordered by failed_at DESC.

    raw_text is truncated to 240 chars (mirrors text_preview convention
    in get_tg_social_alerts handler) so the response stays under the
    payload budget — full text accessible by SSH if needed.

    Defensive (S1 — F17 mitigation): if the dashboard is pointed at a
    pre-BL-064 DB snapshot (rollback scenario), tg_social_dlq won't exist
    and the SELECT 500s. Mirror the cashtag_trade_eligible column-missing
    pattern: catch OperationalError mentioning the table, return [].
    """
    async with _ro_db(db_path) as conn:
        try:
            cur = await conn.execute(
                "SELECT id, channel_handle, msg_id, raw_text, "
                "error_class, error_text, failed_at, retried_at "
                "FROM tg_social_dlq "
                "ORDER BY failed_at DESC "
                "LIMIT ?",
                (max(1, min(limit, 100)),),
            )
            rows = await cur.fetchall()
        except aiosqlite.OperationalError as e:
            # PR-review MF2 (a707628): narrow catch — only swallow the
            # specific "no such table" form, not any error mentioning the
            # table name. Otherwise a future query bug like
            # "near 'tg_social_dlq': syntax error" would silently return
            # [], masking the bug forever.
            msg = str(e)
            if "no such table" not in msg or "tg_social_dlq" not in msg:
                raise
            structlog.get_logger().warning(
                "dashboard_dlq_table_missing_fallback",
                err=msg,
            )
            return []
        return [
            {
                "id": r[0],
                "channel_handle": r[1],
                "msg_id": r[2],
                "raw_text_preview": (r[3] or "")[:240],
                "error_class": r[4],
                "error_text": r[5],
                "failed_at": r[6],
                "retried_at": r[7],
            }
            for r in rows
        ]


async def get_tg_social_cashtag_stats_24h(db_path: str) -> dict:
    """BL-066' cashtag-dispatch rollup: count of paper_trades opened in
    last 24h whose signal_data carries resolution=cashtag.

    Returns {"dispatched": int}. Rolling 24h window — distinct from
    get_tg_social_per_channel_cashtag_today (calendar-day) which mirrors
    the dispatcher's gate semantics. This is a different surface (24h
    rollup card, not cap enforcement)."""
    async with _ro_db(db_path) as conn:
        cur = await conn.execute("""SELECT COUNT(*)
               FROM paper_trades
               WHERE signal_type = 'tg_social'
                 AND json_extract(signal_data, '$.resolution') = 'cashtag'
                 AND datetime(opened_at) >= datetime('now', '-24 hours')""")
        row = await cur.fetchone()
        return {"dispatched": row[0] if row else 0}


async def get_tg_social_per_channel_cashtag_today(db_path: str) -> dict[str, int]:
    """BL-066' per-channel cashtag dispatches since UTC midnight.

    Mirrors the **calendar-day** semantics of the dispatcher's gate at
    `scout/social/telegram/dispatcher.py:_channel_cashtag_trades_today_count`
    (which uses `opened_at >= datetime('now', 'start of day')`). If we
    used a rolling 24h window instead, the dashboard would lie about cap
    utilization — at 06:00 UTC, a channel that hit cap=5 yesterday at
    23:00 would read `5/5 (warn)` here but `0/5` to the dispatcher, and
    the next dispatch would actually go through. **The two surfaces MUST
    use identical date math.**

    Returns dict keyed by channel_handle; channels with zero dispatches
    are omitted (frontend defaults missing keys to 0).
    """
    async with _ro_db(db_path) as conn:
        cur = await conn.execute(
            """SELECT json_extract(signal_data, '$.channel_handle') AS ch,
                      COUNT(*) AS n
               FROM paper_trades
               WHERE signal_type = 'tg_social'
                 AND json_extract(signal_data, '$.resolution') = 'cashtag'
                 AND opened_at >= datetime('now', 'start of day')
               GROUP BY ch"""
        )
        rows = await cur.fetchall()
        # PR-review SHOULD-FIX (a707628 SF1 + ae6d0a #2): count rows where
        # json_extract($.channel_handle) returned NULL or empty separately
        # and warn if any. Otherwise a producer-side bug that drops
        # channel_handle from signal_data silently makes per-channel counts
        # mismatch the rolling 24h aggregate — exactly the silent failure
        # the project's discipline forbids.
        result: dict[str, int] = {}
        unknown_count = 0
        for r in rows:
            if r[0]:
                result[r[0]] = r[1]
            else:
                unknown_count += r[1]
        if unknown_count:
            structlog.get_logger().warning(
                "dashboard_cashtag_null_channel_handle",
                count=unknown_count,
            )
        return result


async def get_source_calls_health(db_path: str) -> dict:
    """Read-only aggregate health of the source_calls ledger.

    Surfaces what's visible WITHOUT inferring source quality / ranking.
    Per operator gates (BL-NEW-SOURCE-CALL-CRON-TICK-WATCHDOG +
    BL-NEW-SOURCE-CALL-PRICE-COVERAGE-EXPANSION): no per-source ranking
    is exposed. The endpoint communicates "what's there + why it's not
    rankable yet" honestly.

    Returns dict with shape:
        {
          "row_count": int,
          "row_count_by_source_type": {"tg": int, "x": int},
          "unresolvable_rate": float | None,  # null if 0 rows
          "duplicate_rate": float | None,
          "outcome_status_counts": {<status>: int, ...},
          "price_coverage": {
              "with_price_at_call": int,
              "with_forward_30m_pct": int,
              "with_forward_1h_pct": int,
              "with_forward_6h_pct": int,
              "with_forward_24h_pct": int,
          },
          "rankability": {
              "source_count": int,
              "rankable": int,  # rank_status='rankable_resolvable_cg_board_cohort'
              "insufficient_sample": int,
              "biased_low_coverage": int,
              "not_rankable_label": str,  # operator-facing prose
          },
          "writer_freshness": {
              "max_observed_at": str | None,
              "minutes_since_last_observed": float | None,
              "lag_threshold_minutes": 30,
          },
          "checked_at": str  # ISO8601
        }

    Defensive: returns a "schema_missing"-flagged dict if the
    source_calls table doesn't exist (fresh DB or pre-PR-#206 rollback).
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    base_response = {
        "row_count": 0,
        "row_count_by_source_type": {"tg": 0, "x": 0},
        "unresolvable_rate": None,
        "duplicate_rate": None,
        "outcome_status_counts": {},
        "price_coverage": {
            "with_price_at_call": 0,
            "with_forward_30m_pct": 0,
            "with_forward_1h_pct": 0,
            "with_forward_6h_pct": 0,
            "with_forward_24h_pct": 0,
        },
        "rankability": {
            "source_count": 0,
            "rankable": 0,
            "insufficient_sample": 0,
            "biased_low_coverage": 0,
            "not_rankable_label": "no source_calls rows yet",
        },
        "writer_freshness": {
            "max_observed_at": None,
            "minutes_since_last_observed": None,
            "lag_threshold_minutes": 30,
        },
        "checked_at": now.isoformat(),
    }

    async with _ro_db(db_path) as conn:
        try:
            row = await (await conn.execute(
                "SELECT COUNT(*) FROM source_calls"
            )).fetchone()
            row_count = row[0] if row else 0
        except aiosqlite.OperationalError as exc:
            msg = str(exc)
            if "no such table" not in msg or "source_calls" not in msg:
                raise
            structlog.get_logger().warning(
                "dashboard_source_calls_health_schema_missing",
                err=msg,
            )
            base_response["rankability"]["not_rankable_label"] = (
                "source_calls table does not exist on this DB "
                "(fresh install or pre-PR-#206 rollback)"
            )
            return {**base_response, "schema_missing": True}

        if row_count == 0:
            return base_response

        # Row counts by source_type
        cur = await conn.execute(
            "SELECT source_type, COUNT(*) FROM source_calls GROUP BY source_type"
        )
        for stype, cnt in await cur.fetchall():
            if stype in ("tg", "x"):
                base_response["row_count_by_source_type"][stype] = cnt
        base_response["row_count"] = row_count

        # Outcome status distribution
        cur = await conn.execute(
            "SELECT COALESCE(outcome_status, 'unknown'), COUNT(*) "
            "FROM source_calls GROUP BY outcome_status"
        )
        status_counts = {row[0]: row[1] for row in await cur.fetchall()}
        base_response["outcome_status_counts"] = status_counts

        unresolvable = status_counts.get("unresolvable", 0)
        base_response["unresolvable_rate"] = (
            round(unresolvable / row_count, 4) if row_count else None
        )

        # Duplicate rate: rows where duplicate_rank_in_cluster > 1
        cur = await conn.execute(
            "SELECT COUNT(*) FROM source_calls "
            "WHERE duplicate_rank_in_cluster IS NOT NULL "
            "AND duplicate_rank_in_cluster > 1"
        )
        dup_row = await cur.fetchone()
        duplicate_count = dup_row[0] if dup_row else 0
        base_response["duplicate_rate"] = (
            round(duplicate_count / row_count, 4) if row_count else None
        )

        # Price coverage by horizon
        cur = await conn.execute("""
            SELECT
              SUM(CASE WHEN price_at_call IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN forward_30m_pct IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN forward_1h_pct IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN forward_6h_pct IS NOT NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN forward_24h_pct IS NOT NULL THEN 1 ELSE 0 END)
            FROM source_calls
        """)
        cov = await cur.fetchone()
        if cov:
            base_response["price_coverage"] = {
                "with_price_at_call": cov[0] or 0,
                "with_forward_30m_pct": cov[1] or 0,
                "with_forward_1h_pct": cov[2] or 0,
                "with_forward_6h_pct": cov[3] or 0,
                "with_forward_24h_pct": cov[4] or 0,
            }

        # Rankability rollup: count sources at each rank_status, NOT exposing
        # which sources are rankable. Per operator gate: no per-source ranking.
        # Uses the same gate (min_sample=10, min_coverage_rate=0.50) as
        # scout.source_quality.ledger.compute_source_quality_summary.
        try:
            from scout.source_quality.ledger import compute_source_quality_summary
            summaries = await compute_source_quality_summary(conn)
            rankable = sum(
                1 for s in summaries
                if s.rank_status == "rankable_resolvable_cg_board_cohort"
            )
            insufficient = sum(
                1 for s in summaries if s.rank_status == "insufficient_sample"
            )
            biased = sum(
                1 for s in summaries if s.rank_status == "biased_low_coverage"
            )
            source_count = len(summaries)
            if rankable == 0:
                reasons = []
                if insufficient > 0:
                    reasons.append(f"{insufficient} below min_sample=10")
                if biased > 0:
                    reasons.append(f"{biased} below min_coverage_rate=0.50")
                label = (
                    "no sources rankable yet — "
                    + (", ".join(reasons) if reasons else "no qualifying sources")
                )
            else:
                label = (
                    f"{rankable}/{source_count} sources meet gate "
                    "(min_sample=10 AND coverage>=0.50) — rankable cohort "
                    "available but per-source ranking deliberately not "
                    "exposed pending operator review (see "
                    "BL-NEW-DASHBOARD-SOURCE-CALL-QUALITY-SURFACE)"
                )
            base_response["rankability"] = {
                "source_count": source_count,
                "rankable": rankable,
                "insufficient_sample": insufficient,
                "biased_low_coverage": biased,
                "not_rankable_label": label,
            }
        except Exception as exc:
            structlog.get_logger().warning(
                "dashboard_source_calls_rankability_summary_failed",
                err=str(exc)[:200],
            )

        # Writer freshness: max(observed_at) → minutes since.
        cur = await conn.execute(
            "SELECT MAX(observed_at) FROM source_calls"
        )
        max_row = await cur.fetchone()
        max_observed = max_row[0] if max_row else None
        if max_observed:
            try:
                last_dt = datetime.fromisoformat(
                    max_observed.replace("Z", "+00:00")
                )
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_min = (now - last_dt).total_seconds() / 60.0
                base_response["writer_freshness"] = {
                    "max_observed_at": max_observed,
                    "minutes_since_last_observed": round(age_min, 1),
                    "lag_threshold_minutes": 30,
                }
            except (ValueError, TypeError):
                base_response["writer_freshness"]["max_observed_at"] = max_observed

        return base_response

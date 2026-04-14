"""Read-only database queries for the dashboard against scout.db."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite

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
    """Open a read-only connection to the database."""
    db = await aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
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
               LEFT JOIN outcomes o ON a.id = o.id
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
    """Open a read-write connection to the database."""
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
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
        return [dict(row) for row in rows]


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
        cursor = await conn.execute(
            """SELECT
                SUM(CASE WHEN outcome_class='HIT' AND is_control=0 THEN 1 ELSE 0 END) as agent_hits,
                SUM(CASE WHEN is_control=0 AND outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as agent_total,
                SUM(CASE WHEN outcome_class='HIT' AND is_control=1 THEN 1 ELSE 0 END) as ctrl_hits,
                SUM(CASE WHEN is_control=1 AND outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as ctrl_total,
                COUNT(*) as total_predictions,
                SUM(CASE WHEN outcome_class IS NOT NULL
                     AND outcome_class != 'UNRESOLVED' THEN 1 ELSE 0 END) as resolved
               FROM predictions"""
        )
        row = await cursor.fetchone()
        d = dict(row) if row else {}

        agent_hits = d.get("agent_hits") or 0
        agent_total = d.get("agent_total") or 0
        ctrl_hits = d.get("ctrl_hits") or 0
        ctrl_total = d.get("ctrl_total") or 0
        total_predictions = d.get("total_predictions") or 0
        resolved = d.get("resolved") or 0

        agent_rate = round((agent_hits / agent_total * 100) if agent_total > 0 else 0, 1)
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
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM signal_events"
        )
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


async def _table_stats(conn, table: str, time_col: str) -> dict:
    """Return count and latest timestamp for a table; tolerate missing tables."""
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
                           - COALESCE(p.counter_risk_score, 0) * 0.5
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
            cursor = await conn.execute(
                """SELECT
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
                   LIMIT 10"""
            )
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
        if s["signal_type"] == "narrative_prediction" or s["signal_type"] == "category_heating":
            s["is_meme"] = False
        elif s.get("chain") and s["chain"] not in ("coingecko", None, ""):
            s["is_meme"] = True
        else:
            s["is_meme"] = False

    merged.sort(key=lambda r: (r.get("quality_score") or 0), reverse=True)
    return merged[:limit]


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

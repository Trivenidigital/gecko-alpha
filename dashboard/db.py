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
    """Recent alerts ordered by alerted_at DESC."""
    async with _ro_db(db_path) as db:
        cursor = await db.execute(
            """SELECT a.contract_address, a.chain, a.conviction_score, a.alerted_at,
                      c.token_name, c.ticker, c.market_cap_usd
               FROM alerts a
               LEFT JOIN candidates c ON a.contract_address = c.contract_address
               ORDER BY a.alerted_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


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

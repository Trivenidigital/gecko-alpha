"""Async SQLite database layer for CoinPump Scout."""

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from scout.models import CandidateToken

# Columns that map 1:1 from CandidateToken to the candidates table.
_CANDIDATE_COLUMNS = [
    "contract_address",
    "chain",
    "token_name",
    "ticker",
    "token_age_days",
    "market_cap_usd",
    "liquidity_usd",
    "volume_24h_usd",
    "holder_count",
    "holder_growth_1h",
    "social_mentions_24h",
    "quant_score",
    "narrative_score",
    "conviction_score",
    "mirofish_report",
    "virality_class",
    "signals_fired",
    "alerted_at",
    "first_seen_at",
    "counter_risk_score",
    "counter_flags",
    "counter_argument",
    "counter_data_completeness",
    "counter_scored_at",
]


class Database:
    """Thin async wrapper around an aiosqlite connection."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open connection and create tables."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_tables(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                contract_address TEXT PRIMARY KEY,
                chain            TEXT NOT NULL,
                token_name       TEXT NOT NULL,
                ticker           TEXT NOT NULL,
                token_age_days   REAL    DEFAULT 0,
                market_cap_usd   REAL    DEFAULT 0,
                liquidity_usd    REAL    DEFAULT 0,
                volume_24h_usd   REAL    DEFAULT 0,
                holder_count     INTEGER DEFAULT 0,
                holder_growth_1h INTEGER DEFAULT 0,
                social_mentions_24h INTEGER DEFAULT 0,
                quant_score      INTEGER,
                narrative_score  INTEGER,
                conviction_score REAL,
                mirofish_report  TEXT,
                virality_class   TEXT,
                signals_fired    TEXT,
                alerted_at       TEXT,
                first_seen_at    TEXT NOT NULL,
                counter_risk_score       INTEGER,
                counter_flags            TEXT,
                counter_argument         TEXT,
                counter_data_completeness TEXT,
                counter_scored_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                chain             TEXT NOT NULL,
                conviction_score  REAL NOT NULL,
                alert_market_cap  REAL,
                alerted_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mirofish_jobs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                created_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS holder_snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                holder_count      INTEGER NOT NULL,
                scanned_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS volume_snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                volume_24h_usd    REAL NOT NULL,
                scanned_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS score_history (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                score             REAL NOT NULL,
                scanned_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outcomes (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address  TEXT NOT NULL,
                alert_price       REAL,
                check_price       REAL,
                check_time        TEXT,
                price_change_pct  REAL
            );

            CREATE TABLE IF NOT EXISTS category_snapshots (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id           TEXT NOT NULL,
                name                  TEXT NOT NULL,
                market_cap            REAL,
                market_cap_change_24h REAL,
                volume_24h            REAL,
                coin_count            INTEGER,
                market_regime         TEXT,
                snapshot_at           TEXT NOT NULL,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_cat_snap_category
                ON category_snapshots(category_id, snapshot_at);

            CREATE TABLE IF NOT EXISTS narrative_signals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id       TEXT NOT NULL,
                category_name     TEXT NOT NULL,
                acceleration      REAL NOT NULL,
                volume_growth_pct REAL NOT NULL,
                coin_count_change INTEGER,
                trigger_count     INTEGER DEFAULT 1,
                detected_at       TEXT NOT NULL,
                cooling_down_until TEXT NOT NULL,
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_narr_sig_category
                ON narrative_signals(category_id, cooling_down_until);

            CREATE TABLE IF NOT EXISTS predictions (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id             TEXT NOT NULL,
                category_name           TEXT NOT NULL,
                coin_id                 TEXT NOT NULL,
                symbol                  TEXT NOT NULL,
                name                    TEXT NOT NULL,
                market_cap_at_prediction REAL NOT NULL,
                price_at_prediction     REAL NOT NULL,
                narrative_fit_score     INTEGER NOT NULL,
                staying_power           TEXT NOT NULL,
                confidence              TEXT NOT NULL,
                reasoning               TEXT NOT NULL,
                market_regime           TEXT,
                trigger_count           INTEGER,
                is_control              INTEGER DEFAULT 0,
                is_holdout              INTEGER DEFAULT 0,
                strategy_snapshot       TEXT NOT NULL,
                strategy_snapshot_ab    TEXT,
                predicted_at            TEXT NOT NULL,
                outcome_6h_price        REAL,
                outcome_6h_change_pct   REAL,
                outcome_6h_class        TEXT,
                outcome_24h_price       REAL,
                outcome_24h_change_pct  REAL,
                outcome_24h_class       TEXT,
                outcome_48h_price       REAL,
                outcome_48h_change_pct  REAL,
                outcome_48h_class       TEXT,
                peak_price              REAL,
                peak_change_pct         REAL,
                peak_at                 TEXT,
                outcome_class           TEXT,
                outcome_reason          TEXT,
                eval_retry_count        INTEGER DEFAULT 0,
                counter_risk_score       INTEGER,
                counter_flags            TEXT,
                counter_argument         TEXT,
                counter_data_completeness TEXT,
                counter_scored_at        TEXT,
                evaluated_at            TEXT,
                created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(category_id, coin_id, predicted_at)
            );
            CREATE INDEX IF NOT EXISTS idx_pred_category
                ON predictions(category_id);
            CREATE INDEX IF NOT EXISTS idx_pred_predicted
                ON predictions(predicted_at);
            CREATE INDEX IF NOT EXISTS idx_pred_outcome
                ON predictions(outcome_class);

            CREATE TABLE IF NOT EXISTS agent_strategy (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                reason     TEXT,
                locked     INTEGER DEFAULT 0,
                min_bound  REAL,
                max_bound  REAL
            );

            CREATE TABLE IF NOT EXISTS learn_logs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_number     INTEGER NOT NULL,
                cycle_type       TEXT NOT NULL,
                reflection_text  TEXT NOT NULL,
                changes_made     TEXT NOT NULL,
                hit_rate_before  REAL,
                hit_rate_after   REAL,
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS second_wave_candidates (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address         TEXT NOT NULL,
                chain                    TEXT NOT NULL,
                token_name               TEXT NOT NULL,
                ticker                   TEXT NOT NULL,
                coingecko_id             TEXT,
                peak_quant_score         INTEGER NOT NULL,
                peak_signals_fired       TEXT,
                first_seen_at            TEXT NOT NULL,
                original_alert_at        TEXT,
                original_market_cap      REAL,
                alert_market_cap         REAL,
                days_since_first_seen    REAL,
                price_drop_from_peak_pct REAL,
                current_price            REAL,
                current_market_cap       REAL,
                current_volume_24h       REAL,
                price_vs_alert_pct       REAL,
                volume_vs_cooldown_avg   REAL,
                price_is_stale           INTEGER NOT NULL DEFAULT 0,
                reaccumulation_score     INTEGER NOT NULL,
                reaccumulation_signals   TEXT NOT NULL,
                detected_at              TEXT NOT NULL,
                alerted_at               TEXT,
                created_at               TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sw_contract
                ON second_wave_candidates(contract_address, detected_at);
            CREATE INDEX IF NOT EXISTS idx_sw_score
                ON second_wave_candidates(reaccumulation_score);
            """
        )

        # Migrate alerts table: add price_usd, token_name, ticker if missing
        cursor = await self._conn.execute("PRAGMA table_info(alerts)")
        existing_cols = {row[1] for row in await cursor.fetchall()}
        for col, ddl in (
            ("price_usd", "ALTER TABLE alerts ADD COLUMN price_usd REAL"),
            ("token_name", "ALTER TABLE alerts ADD COLUMN token_name TEXT"),
            ("ticker", "ALTER TABLE alerts ADD COLUMN ticker TEXT"),
        ):
            if col not in existing_cols:
                await self._conn.execute(ddl)
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Candidates
    # ------------------------------------------------------------------

    async def upsert_candidate(self, token: CandidateToken) -> None:
        """INSERT OR REPLACE candidate by contract_address."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        placeholders = ", ".join("?" for _ in _CANDIDATE_COLUMNS)
        cols = ", ".join(_CANDIDATE_COLUMNS)
        values = []
        for col in _CANDIDATE_COLUMNS:
            v = getattr(token, col)
            # Serialize datetimes to ISO strings
            if isinstance(v, datetime):
                v = v.isoformat()
            # Serialize lists to JSON strings
            elif isinstance(v, list):
                v = json.dumps(v)
            values.append(v)
        await self._conn.execute(
            f"INSERT OR REPLACE INTO candidates ({cols}) VALUES ({placeholders})",
            values,
        )
        await self._conn.commit()

    async def get_candidates_above_score(self, min_score: int) -> list[dict]:
        """Get candidates with quant_score >= min_score."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT * FROM candidates WHERE quant_score IS NOT NULL AND quant_score >= ?",
            (min_score,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    async def log_alert(
        self, contract_address: str, chain: str, conviction_score: float,
        alert_market_cap: float | None = None,
        price_usd: float | None = None,
        token_name: str | None = None,
        ticker: str | None = None,
    ) -> None:
        """Log a fired alert with market cap, price, and token identity."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO alerts
               (contract_address, chain, conviction_score, alert_market_cap,
                price_usd, token_name, ticker, alerted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (contract_address, chain, conviction_score, alert_market_cap,
             price_usd, token_name, ticker, now),
        )
        await self._conn.commit()

    async def get_unchecked_alerts(self) -> list[dict]:
        """Get alerts that don't have an outcome recorded yet."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT a.id, a.contract_address, a.chain, a.alert_market_cap, a.alerted_at
               FROM alerts a
               LEFT JOIN outcomes o ON a.id = o.id
               WHERE o.id IS NULL AND a.alert_market_cap IS NOT NULL""",
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def log_outcome(
        self, alert_id: int, contract_address: str,
        alert_price: float, check_price: float, price_change_pct: float,
    ) -> None:
        """Record an outcome for an alert."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO outcomes
               (id, contract_address, alert_price, check_price, check_time, price_change_pct)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (alert_id, contract_address, alert_price, check_price, now, price_change_pct),
        )
        await self._conn.commit()

    async def was_recently_alerted(self, contract_address: str, hours: int = 4) -> bool:
        """Check if a token was alerted within the last N hours."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE contract_address = ? AND alerted_at >= datetime('now', ?)",
            (contract_address, f"-{hours} hours"),
        )
        row = await cursor.fetchone()
        return row[0] > 0 if row else False

    async def get_daily_alert_count(self) -> int:
        """Count alerts fired today (UTC)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE date(alerted_at) = ?",
            (today,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_recent_alerts(self, days: int = 30) -> list[dict]:
        """Get alerts from the last N days."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT * FROM alerts WHERE date(alerted_at) >= date('now', ?)",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # MiroFish jobs
    # ------------------------------------------------------------------

    async def log_mirofish_job(self, contract_address: str) -> None:
        """Log a MiroFish simulation job."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO mirofish_jobs (contract_address, created_at) VALUES (?, ?)",
            (contract_address, now),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Holder snapshots
    # ------------------------------------------------------------------

    async def log_holder_snapshot(self, contract_address: str, holder_count: int) -> None:
        """Log a holder count snapshot for growth tracking."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO holder_snapshots (contract_address, holder_count, scanned_at) VALUES (?, ?, ?)",
            (contract_address, holder_count, now),
        )
        await self._conn.commit()

    async def get_previous_holder_count(self, contract_address: str) -> int | None:
        """Get the most recent holder count for a contract, or None if no history."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT holder_count FROM holder_snapshots WHERE contract_address = ? ORDER BY scanned_at DESC LIMIT 1",
            (contract_address,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Score history
    # ------------------------------------------------------------------

    async def log_score(self, contract_address: str, score: float) -> None:
        """Log a quant score for velocity tracking."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
            (contract_address, score, now),
        )
        await self._conn.commit()

    async def get_recent_scores(self, contract_address: str, limit: int = 3) -> list[float]:
        """Get the most recent scores for a contract, newest first."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT score FROM score_history WHERE contract_address = ? ORDER BY scanned_at DESC LIMIT ?",
            (contract_address, limit),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_vol_7d_avg(self, contract_address: str) -> float | None:
        """Compute rolling 7-day average of volume_24h_usd for a contract.

        Returns None if fewer than 3 historical rows exist (insufficient data).
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT volume_24h_usd FROM volume_snapshots
               WHERE contract_address = ? AND scanned_at >= datetime('now', '-7 days')
               ORDER BY scanned_at DESC""",
            (contract_address,),
        )
        rows = await cursor.fetchall()
        if len(rows) < 3:
            return None
        return sum(r[0] for r in rows) / len(rows)

    async def log_volume_snapshot(self, contract_address: str, volume: float) -> None:
        """Log a volume snapshot for 7-day average computation."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
            (contract_address, volume, now),
        )
        await self._conn.commit()

    async def get_daily_summary_data(self) -> dict:
        """Gather data for the daily Telegram summary."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Alerts fired today
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE date(alerted_at) = ?", (today,),
        )
        alerts_today = (await cursor.fetchone())[0]

        # Win rate for alerts older than 4 hours
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM outcomes o
               JOIN alerts a ON o.id = a.id
               WHERE date(a.alerted_at) = ?
               AND a.alerted_at <= datetime('now', '-4 hours')""",
            (today,),
        )
        outcomes_total = (await cursor.fetchone())[0]

        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM outcomes o
               JOIN alerts a ON o.id = a.id
               WHERE date(a.alerted_at) = ?
               AND a.alerted_at <= datetime('now', '-4 hours')
               AND o.price_change_pct > 0""",
            (today,),
        )
        outcomes_wins = (await cursor.fetchone())[0]

        # Top signal combination (most common non-empty signals_fired pattern today)
        cursor = await self._conn.execute(
            """SELECT signals_fired, COUNT(*) as cnt FROM candidates
               WHERE date(first_seen_at) = ? AND signals_fired IS NOT NULL
               AND signals_fired != '[]' AND signals_fired != 'null'
               GROUP BY signals_fired ORDER BY cnt DESC LIMIT 1""",
            (today,),
        )
        top_combo_row = await cursor.fetchone()
        top_signal_combo = top_combo_row[0] if top_combo_row else None

        # Top 3 highest conviction tokens today
        cursor = await self._conn.execute(
            """SELECT token_name, ticker, chain, quant_score, narrative_score,
                      conviction_score, signals_fired
               FROM candidates
               WHERE date(first_seen_at) = ? AND conviction_score IS NOT NULL
               ORDER BY conviction_score DESC LIMIT 3""",
            (today,),
        )
        top_tokens = [dict(row) for row in await cursor.fetchall()]

        return {
            "alerts_today": alerts_today,
            "outcomes_total": outcomes_total,
            "outcomes_wins": outcomes_wins,
            "win_rate_pct": round((outcomes_wins / outcomes_total * 100) if outcomes_total > 0 else 0, 1),
            "top_signal_combo": top_signal_combo,
            "top_tokens": top_tokens,
        }

    async def prune_old_candidates(self, keep_days: int = 7) -> int:
        """Delete candidates older than keep_days. Returns rows deleted."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "DELETE FROM candidates WHERE first_seen_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def get_daily_mirofish_count(self) -> int:
        """Count MiroFish jobs run today (UTC)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM mirofish_jobs WHERE date(created_at) = ?",
            (today,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Second-Wave Detection
    # ------------------------------------------------------------------

    async def get_secondwave_scan_candidates(
        self,
        min_age_days: int = 3,
        max_age_days: int = 14,
        min_peak_score: int = 60,
        dedup_days: int = 7,
    ) -> list[dict]:
        """Get alerted tokens in the cooldown window whose peak quant_score
        exceeded min_peak_score and that haven't been second-wave alerted recently.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT a.contract_address,
                      a.chain,
                      COALESCE(a.token_name, '') AS token_name,
                      COALESCE(a.ticker, '')     AS ticker,
                      a.alert_market_cap,
                      a.price_usd                AS alert_price,
                      a.alerted_at,
                      MAX(sh.score)              AS peak_quant_score
               FROM alerts a
               LEFT JOIN score_history sh ON sh.contract_address = a.contract_address
               WHERE a.alerted_at <= datetime('now', '-' || ? || ' days')
                 AND a.alerted_at >= datetime('now', '-' || ? || ' days')
                 AND a.contract_address NOT IN (
                     SELECT contract_address FROM second_wave_candidates
                     WHERE detected_at >= datetime('now', '-' || ? || ' days')
                 )
               GROUP BY a.contract_address
               HAVING peak_quant_score >= ?""",
            (int(min_age_days), int(max_age_days), int(dedup_days), int(min_peak_score)),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_coingecko_id_by_symbol(self, symbol: str) -> str | None:
        """Look up a CoinGecko coin_id from the predictions table by ticker symbol.

        Symbol-to-coin_id mapping requires the narrative agent to be enabled
        (``NARRATIVE_ENABLED=true``). When disabled, the predictions table is
        empty and every caller will receive ``None``. In the second-wave
        detector this causes tokens to fall back to the stale-price path,
        where alerts are suppressed entirely.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not symbol:
            return None
        cursor = await self._conn.execute(
            """SELECT coin_id FROM predictions
               WHERE symbol = ?
               ORDER BY predicted_at DESC
               LIMIT 1""",
            (symbol,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def was_secondwave_alerted(
        self, contract_address: str, days: int = 7
    ) -> bool:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM second_wave_candidates
               WHERE contract_address = ?
                 AND detected_at >= datetime('now', '-' || ? || ' days')""",
            (contract_address, int(days)),
        )
        row = await cursor.fetchone()
        return row[0] > 0 if row else False

    async def insert_secondwave_candidate(self, candidate: dict) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        await self._conn.execute(
            """INSERT INTO second_wave_candidates
               (contract_address, chain, token_name, ticker, coingecko_id,
                peak_quant_score, peak_signals_fired, first_seen_at,
                original_alert_at, original_market_cap, alert_market_cap,
                days_since_first_seen, price_drop_from_peak_pct,
                current_price, current_market_cap, current_volume_24h,
                price_vs_alert_pct, volume_vs_cooldown_avg, price_is_stale,
                reaccumulation_score, reaccumulation_signals,
                detected_at, alerted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate["contract_address"],
                candidate["chain"],
                candidate["token_name"],
                candidate["ticker"],
                candidate.get("coingecko_id"),
                candidate["peak_quant_score"],
                json.dumps(candidate.get("peak_signals_fired") or []),
                candidate["first_seen_at"],
                candidate.get("original_alert_at"),
                candidate.get("original_market_cap"),
                candidate.get("alert_market_cap"),
                candidate.get("days_since_first_seen"),
                candidate.get("price_drop_from_peak_pct"),
                candidate.get("current_price"),
                candidate.get("current_market_cap"),
                candidate.get("current_volume_24h"),
                candidate.get("price_vs_alert_pct"),
                candidate.get("volume_vs_cooldown_avg"),
                1 if candidate.get("price_is_stale") else 0,
                candidate["reaccumulation_score"],
                json.dumps(candidate.get("reaccumulation_signals") or []),
                candidate["detected_at"],
                candidate.get("alerted_at"),
            ),
        )
        await self._conn.commit()

    async def get_recent_secondwave_candidates(self, days: int = 7) -> list[dict]:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT * FROM second_wave_candidates
               WHERE detected_at >= datetime('now', '-' || ? || ' days')
               ORDER BY reaccumulation_score DESC""",
            (int(days),),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        for r in rows:
            r["peak_signals_fired"] = json.loads(r.get("peak_signals_fired") or "[]")
            r["reaccumulation_signals"] = json.loads(r.get("reaccumulation_signals") or "[]")
            r["price_is_stale"] = bool(r.get("price_is_stale", 0))
        return rows

    async def get_volume_history(
        self, contract_address: str, days: int = 14
    ) -> list[float]:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT volume_24h_usd FROM volume_snapshots
               WHERE contract_address = ?
                 AND scanned_at >= datetime('now', '-' || ? || ' days')
               ORDER BY scanned_at DESC""",
            (contract_address, int(days)),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

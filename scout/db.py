"""Async SQLite database layer for CoinPump Scout."""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import structlog

_db_log = structlog.get_logger(__name__)

from scout.models import CandidateToken

if TYPE_CHECKING:
    from scout.news.schemas import CryptoPanicPost
    from scout.perp.schemas import PerpAnomaly

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
        self._txn_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open connection and create tables."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        self._txn_lock = asyncio.Lock()
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # BL-055 spec §3.2: foreign_keys=ON is REQUIRED on every connection.
        # Default is OFF in SQLite; without it, ON DELETE RESTRICT is a no-op.
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self._migrate_feedback_loop_schema()
        await self._migrate_live_trading_schema()
        await self._migrate_signal_params_schema()

    async def connect(self) -> None:
        """Alias for :meth:`initialize` — preferred in tests and async context managers."""
        await self.initialize()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # BL-076: shared metadata resolver
    # ------------------------------------------------------------------

    async def lookup_symbol_name_by_coin_id(
        self, coin_id: str | None
    ) -> tuple[str, str]:
        """BL-076: pure metadata lookup. Returns (symbol, name) for a
        CoinGecko coin_id, resolving via 3 sequential prioritized SELECTs.

        chain_matches table carries no symbol/name. This helper bridges
        that gap by querying snapshot tables that DO have it (all keyed
        by coin_id). Lives on Database (not signals.py) so future callers
        (dashboard, backfill scripts) reuse the resolver instead of
        reimplementing the JOIN.

        Lookup order (gainers_snapshots is the most authoritative source,
        populated from CoinGecko's /coins/markets endpoint):
          1. gainers_snapshots (canonical CoinGecko metadata)
          2. volume_history_cg (CoinGecko volume telemetry)
          3. volume_spikes (DexScreener-side spikes)

        Each SELECT in its own ``except aiosqlite.OperationalError``:
        a column rename or table lock in any one table fails ONLY that
        lookup; the next table still works. Other exception types
        (programming errors, etc.) propagate. Returns ("", "") if nothing
        found — caller decides whether to log + still proceed.

        Refactor triggers:
        - Add a 4th source OR source priority becomes dynamic per-chain →
          refactor to MetadataSource plugin pattern.
        - Cardinality exceeds ~500/cycle → refactor to UNION ALL with
          per-table OperationalError fallback for happy-path single
          round-trip.
        """
        # F16 mitigation + SF-1 fix (PR #67 silent-failure-hunter):
        # defensive None/empty coin_id guard with breadcrumb. Without
        # the log, the F16 caller-bug stays invisible forever — violates
        # explicit-fallback project rule.
        if not coin_id:
            _db_log.warning("lookup_symbol_name_called_with_empty_coin_id")
            return "", ""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        # 1. gainers_snapshots — primary source (canonical CoinGecko)
        try:
            cur = await self._conn.execute(
                "SELECT symbol, name FROM gainers_snapshots "
                "WHERE coin_id = ? AND symbol IS NOT NULL AND name IS NOT NULL "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (coin_id,),
            )
            row = await cur.fetchone()
            if row and row["symbol"] and row["name"]:
                return row["symbol"], row["name"]
        except aiosqlite.OperationalError as exc:
            # F3 (schema drift) + F17 (table locked) — fall through.
            # Other exceptions (e.g. ProgrammingError from a logic bug)
            # propagate per A11. MF-1 fix (PR #67 silent-failure-hunter):
            # log a debug breadcrumb so connection-drop / lock signature
            # is greppable, distinguishing infra failure from F6 orphans.
            _db_log.debug(
                "lookup_symbol_name_table_unavailable",
                table="gainers_snapshots",
                coin_id=coin_id,
                error=str(exc),
            )
        # 2. volume_history_cg — fallback
        try:
            cur = await self._conn.execute(
                "SELECT symbol, name FROM volume_history_cg "
                "WHERE coin_id = ? AND symbol IS NOT NULL AND name IS NOT NULL "
                "ORDER BY recorded_at DESC LIMIT 1",
                (coin_id,),
            )
            row = await cur.fetchone()
            if row and row["symbol"] and row["name"]:
                return row["symbol"], row["name"]
        except aiosqlite.OperationalError as exc:
            _db_log.debug(
                "lookup_symbol_name_table_unavailable",
                table="volume_history_cg",
                coin_id=coin_id,
                error=str(exc),
            )
        # 3. volume_spikes — last resort
        try:
            cur = await self._conn.execute(
                "SELECT symbol, name FROM volume_spikes "
                "WHERE coin_id = ? AND symbol IS NOT NULL AND name IS NOT NULL "
                "ORDER BY detected_at DESC LIMIT 1",
                (coin_id,),
            )
            row = await cur.fetchone()
            if row and row["symbol"] and row["name"]:
                return row["symbol"], row["name"]
        except aiosqlite.OperationalError as exc:
            _db_log.debug(
                "lookup_symbol_name_table_unavailable",
                table="volume_spikes",
                coin_id=coin_id,
                error=str(exc),
            )
        return "", ""

    async def coin_id_resolves(self, coin_id: str | None) -> bool:
        """narrative_prediction-fix: explicit token_id existence probe.

        Returns True iff coin_id appears in any of the canonical sources
        (price_cache + 3 snapshot tables). Replaces the fragile truthiness
        probe on `lookup_symbol_name_by_coin_id` (which returns ("", "")
        on miss; future evolution to default-fill placeholders would
        silently invert the gate's semantics — arch-A2 fix).

        Empty / whitespace coin_id → False (defensive; matches
        _is_tradeable_candidate shape).

        Raises `RuntimeError` on `aiosqlite.OperationalError` so the
        caller can fail-CLOSED. Caller controls whether infra exception
        triggers reject + telemetry or accept-with-degraded-confidence.
        """
        if not coin_id or not coin_id.strip():
            return False
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        for table in (
            "price_cache",
            "gainers_snapshots",
            "volume_history_cg",
            "volume_spikes",
        ):
            try:
                cur = await self._conn.execute(
                    f"SELECT 1 FROM {table} WHERE coin_id = ? LIMIT 1",
                    (coin_id,),
                )
                if (await cur.fetchone()) is not None:
                    return True
            except aiosqlite.OperationalError as exc:
                raise RuntimeError(
                    f"coin_id_resolves OperationalError on {table}: {exc}"
                ) from exc
        return False

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_tables(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        await self._conn.executescript("""
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

            CREATE INDEX IF NOT EXISTS idx_alerts_alerted_at ON alerts(alerted_at);
            CREATE INDEX IF NOT EXISTS idx_candidates_first_seen ON candidates(first_seen_at);

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

            CREATE INDEX IF NOT EXISTS idx_holder_snap_addr
                ON holder_snapshots(contract_address, scanned_at);
            CREATE INDEX IF NOT EXISTS idx_volume_snap_addr
                ON volume_snapshots(contract_address, scanned_at);
            CREATE INDEX IF NOT EXISTS idx_score_hist_addr
                ON score_history(contract_address, scanned_at);

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
            CREATE INDEX IF NOT EXISTS idx_cat_snap_at
                ON category_snapshots(snapshot_at);

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
                watchlist_users         INTEGER,
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

            CREATE TABLE IF NOT EXISTS signal_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id       TEXT NOT NULL,
                pipeline       TEXT NOT NULL,
                event_type     TEXT NOT NULL,
                event_data     TEXT NOT NULL,
                source_module  TEXT NOT NULL,
                created_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sig_events_token
                ON signal_events(token_id, pipeline, created_at);
            CREATE INDEX IF NOT EXISTS idx_sig_events_type
                ON signal_events(event_type, created_at);

            CREATE TABLE IF NOT EXISTS chain_patterns (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL UNIQUE,
                description          TEXT NOT NULL,
                steps_json           TEXT NOT NULL,
                min_steps_to_trigger INTEGER NOT NULL,
                conviction_boost     INTEGER NOT NULL DEFAULT 0,
                alert_priority       TEXT NOT NULL DEFAULT 'low',
                is_active            INTEGER NOT NULL DEFAULT 1,
                historical_hit_rate  REAL,
                total_triggers       INTEGER DEFAULT 0,
                total_hits           INTEGER DEFAULT 0,
                created_at           TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS active_chains (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id       TEXT NOT NULL,
                pipeline       TEXT NOT NULL,
                pattern_id     INTEGER NOT NULL REFERENCES chain_patterns(id),
                pattern_name   TEXT NOT NULL,
                steps_matched  TEXT NOT NULL,
                step_events    TEXT NOT NULL,
                anchor_time    TEXT NOT NULL,
                last_step_time TEXT NOT NULL,
                is_complete    INTEGER DEFAULT 0,
                completed_at   TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(token_id, pipeline, pattern_id, anchor_time)
            );
            CREATE INDEX IF NOT EXISTS idx_active_chains_token
                ON active_chains(token_id, pipeline, is_complete);
            CREATE INDEX IF NOT EXISTS idx_active_chains_prune
                ON active_chains(is_complete, completed_at, anchor_time);

            CREATE TABLE IF NOT EXISTS chain_matches (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id             TEXT NOT NULL,
                pipeline             TEXT NOT NULL,
                pattern_id           INTEGER NOT NULL REFERENCES chain_patterns(id),
                pattern_name         TEXT NOT NULL,
                steps_matched        INTEGER NOT NULL,
                total_steps          INTEGER NOT NULL,
                anchor_time          TEXT NOT NULL,
                completed_at         TEXT NOT NULL,
                chain_duration_hours REAL NOT NULL,
                conviction_boost     INTEGER NOT NULL,
                outcome_class        TEXT,
                outcome_change_pct   REAL,
                evaluated_at         TEXT,
                created_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_chain_matches_pattern
                ON chain_matches(pattern_id, outcome_class);
            CREATE INDEX IF NOT EXISTS idx_chain_matches_token
                ON chain_matches(token_id, pipeline, completed_at);
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

            CREATE TABLE IF NOT EXISTS trending_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                market_cap_rank INTEGER,
                trending_score REAL,
                snapshot_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_trending_snap
                ON trending_snapshots(coin_id, snapshot_at);

            CREATE TABLE IF NOT EXISTS trending_comparisons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                appeared_on_trending_at TEXT NOT NULL,
                detected_by_narrative INTEGER DEFAULT 0,
                narrative_detected_at TEXT,
                narrative_lead_minutes REAL,
                detected_by_pipeline INTEGER DEFAULT 0,
                pipeline_detected_at TEXT,
                pipeline_lead_minutes REAL,
                detected_by_chains INTEGER DEFAULT 0,
                chains_detected_at TEXT,
                chains_lead_minutes REAL,
                is_gap INTEGER DEFAULT 1,
                detected_price REAL,
                peak_price REAL,
                peak_gain_pct REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_trending_comp
                ON trending_comparisons(coin_id);

            CREATE TABLE IF NOT EXISTS price_cache (
                coin_id          TEXT PRIMARY KEY,
                current_price    REAL,
                price_change_24h REAL,
                price_change_7d  REAL,
                market_cap       REAL,
                updated_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS volume_history_cg (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                volume_24h REAL NOT NULL,
                market_cap REAL,
                price REAL,
                recorded_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_vol_hist_cg
                ON volume_history_cg(coin_id, recorded_at);

            CREATE TABLE IF NOT EXISTS volume_spikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                current_volume REAL NOT NULL,
                avg_volume_7d REAL NOT NULL,
                spike_ratio REAL NOT NULL,
                market_cap REAL,
                price REAL,
                price_change_24h REAL,
                detected_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_vol_spikes
                ON volume_spikes(coin_id, detected_at);

            CREATE TABLE IF NOT EXISTS momentum_7d (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_7d REAL NOT NULL,
                price_change_24h REAL,
                market_cap REAL,
                current_price REAL,
                volume_24h REAL,
                detected_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_momentum_7d
                ON momentum_7d(coin_id, detected_at);

            CREATE TABLE IF NOT EXISTS gainers_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_24h REAL NOT NULL,
                market_cap REAL,
                volume_24h REAL,
                price_at_snapshot REAL,
                snapshot_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_gainers_snap
                ON gainers_snapshots(coin_id, snapshot_at);

            CREATE TABLE IF NOT EXISTS gainers_comparisons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_24h REAL,
                appeared_on_gainers_at TEXT NOT NULL,
                detected_by_narrative INTEGER DEFAULT 0,
                narrative_lead_minutes REAL,
                detected_by_pipeline INTEGER DEFAULT 0,
                pipeline_lead_minutes REAL,
                detected_by_chains INTEGER DEFAULT 0,
                chains_lead_minutes REAL,
                detected_by_spikes INTEGER DEFAULT 0,
                spikes_lead_minutes REAL,
                is_gap INTEGER DEFAULT 1,
                detected_price REAL,
                peak_price REAL,
                peak_gain_pct REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_gainers_comp
                ON gainers_comparisons(coin_id);

            CREATE TABLE IF NOT EXISTS losers_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_24h REAL NOT NULL,
                market_cap REAL,
                volume_24h REAL,
                price_at_snapshot REAL,
                snapshot_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_losers_snap
                ON losers_snapshots(coin_id, snapshot_at);

            CREATE TABLE IF NOT EXISTS losers_comparisons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_24h REAL,
                appeared_on_losers_at TEXT NOT NULL,
                detected_by_narrative INTEGER DEFAULT 0,
                narrative_lead_minutes REAL,
                detected_by_pipeline INTEGER DEFAULT 0,
                pipeline_lead_minutes REAL,
                detected_by_chains INTEGER DEFAULT 0,
                chains_lead_minutes REAL,
                detected_by_spikes INTEGER DEFAULT 0,
                spikes_lead_minutes REAL,
                is_gap INTEGER DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_losers_comp
                ON losers_comparisons(coin_id);

            -- Note: paper_trades.token_id references candidates.contract_address or
            -- price_cache.coin_id logically, but FK constraints are intentionally
            -- omitted because tokens may appear in trades before being fully
            -- ingested into the candidates pipeline.
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                chain TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_data TEXT NOT NULL,

                entry_price REAL NOT NULL,
                amount_usd REAL NOT NULL,
                quantity REAL NOT NULL,

                tp_pct REAL NOT NULL DEFAULT 20.0,
                sl_pct REAL NOT NULL DEFAULT 10.0,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,

                status TEXT NOT NULL DEFAULT 'open',

                exit_price REAL,
                exit_reason TEXT,
                pnl_usd REAL,
                pnl_pct REAL,

                checkpoint_1h_price REAL,
                checkpoint_1h_pct REAL,
                checkpoint_6h_price REAL,
                checkpoint_6h_pct REAL,
                checkpoint_24h_price REAL,
                checkpoint_24h_pct REAL,
                checkpoint_48h_price REAL,
                checkpoint_48h_pct REAL,

                peak_price REAL,
                peak_pct REAL,

                opened_at TEXT NOT NULL,
                closed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),

                would_be_live INTEGER,

                UNIQUE(token_id, signal_type, opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_opened ON paper_trades(opened_at);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_signal ON paper_trades(signal_type);
            -- NOTE: idx_paper_trades_would_be_live_status is created in _migrate_feedback_loop_schema
            -- AFTER the ALTER TABLE adds the would_be_live column. Keeping it here would break
            -- upgrade from a pre-BL-060 DB where paper_trades exists without would_be_live.

            CREATE TABLE IF NOT EXISTS paper_daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                trades_opened INTEGER NOT NULL DEFAULT 0,
                trades_closed INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                total_pnl_usd REAL NOT NULL DEFAULT 0,
                best_trade_pnl REAL,
                worst_trade_pnl REAL,
                avg_pnl_pct REAL,
                win_rate_pct REAL,
                by_signal_type TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                briefing_type TEXT NOT NULL,
                raw_data TEXT NOT NULL,
                synthesis TEXT NOT NULL,
                model_used TEXT,
                tokens_used INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_briefings_created ON briefings(created_at);

            CREATE TABLE IF NOT EXISTS social_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                fired_social_volume_24h  INTEGER NOT NULL DEFAULT 0,
                fired_galaxy_jump        INTEGER NOT NULL DEFAULT 0,
                fired_interactions_accel INTEGER NOT NULL DEFAULT 0,
                galaxy_score REAL,
                social_volume_24h REAL,
                social_volume_baseline REAL,
                social_spike_ratio REAL,
                interactions_24h REAL,
                sentiment REAL,
                social_dominance REAL,
                price_change_1h REAL,
                price_change_24h REAL,
                market_cap REAL,
                current_price REAL,
                detected_at TEXT NOT NULL,
                alerted_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(coin_id, detected_at)
            );
            CREATE INDEX IF NOT EXISTS idx_social_signals_coin_detected
                ON social_signals(coin_id, detected_at);
            CREATE INDEX IF NOT EXISTS idx_social_signals_symbol
                ON social_signals(symbol);

            CREATE TABLE IF NOT EXISTS social_baselines (
                coin_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                avg_social_volume_24h REAL NOT NULL,
                avg_galaxy_score REAL NOT NULL,
                last_galaxy_score REAL,
                interactions_ring TEXT NOT NULL DEFAULT '[]',
                sample_count INTEGER NOT NULL,
                last_poll_at TEXT,
                last_updated TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS social_credit_ledger (
                utc_date TEXT PRIMARY KEY,
                credits_used INTEGER NOT NULL,
                last_updated TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS velocity_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price_change_1h REAL NOT NULL,
                price_change_24h REAL,
                market_cap REAL,
                volume_24h REAL,
                vol_mcap_ratio REAL,
                current_price REAL,
                detected_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_velocity_alerts
                ON velocity_alerts(coin_id, detected_at);

            CREATE TABLE IF NOT EXISTS cryptopanic_posts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id         INTEGER UNIQUE NOT NULL,
                title           TEXT NOT NULL,
                url             TEXT NOT NULL,
                published_at    TEXT NOT NULL,
                currencies_json TEXT NOT NULL,
                is_macro        INTEGER NOT NULL,
                sentiment       TEXT NOT NULL,
                votes_positive  INTEGER NOT NULL DEFAULT 0,
                votes_negative  INTEGER NOT NULL DEFAULT 0,
                fetched_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_cryptopanic_published_at
                ON cryptopanic_posts(published_at DESC);
            """)

        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS perp_anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                ticker TEXT NOT NULL,
                kind TEXT NOT NULL,
                magnitude REAL NOT NULL,
                baseline REAL,
                observed_at TEXT NOT NULL,
                UNIQUE(exchange, symbol, kind, observed_at)
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_perp_anomalies_ticker_observed "
            "ON perp_anomalies (ticker, observed_at DESC)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_perp_anomalies_observed "
            "ON perp_anomalies (observed_at)"
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

        # Migrate social_signals: add alerted_at if missing (Telegram-dispatch
        # gating column; dedup treats NULL as "not yet delivered" so we retry).
        cursor = await self._conn.execute("PRAGMA table_info(social_signals)")
        ss_cols = {row[1] for row in await cursor.fetchall()}
        if "alerted_at" not in ss_cols:
            await self._conn.execute(
                "ALTER TABLE social_signals ADD COLUMN alerted_at TEXT"
            )

        # Migrate gainers_snapshots: add price_at_snapshot if missing
        cursor = await self._conn.execute("PRAGMA table_info(gainers_snapshots)")
        gs_cols = {row[1] for row in await cursor.fetchall()}
        if "price_at_snapshot" not in gs_cols:
            await self._conn.execute(
                "ALTER TABLE gainers_snapshots ADD COLUMN price_at_snapshot REAL"
            )

        # Migrate losers_snapshots: add price_at_snapshot if missing (L2)
        cursor = await self._conn.execute("PRAGMA table_info(losers_snapshots)")
        ls_cols = {row[1] for row in await cursor.fetchall()}
        if "price_at_snapshot" not in ls_cols:
            await self._conn.execute(
                "ALTER TABLE losers_snapshots ADD COLUMN price_at_snapshot REAL"
            )

        # Migrate trending_comparisons: add peak tracking + social tier columns
        cursor = await self._conn.execute("PRAGMA table_info(trending_comparisons)")
        tc_cols = {row[1] for row in await cursor.fetchall()}
        for col, ddl in (
            (
                "detected_price",
                "ALTER TABLE trending_comparisons ADD COLUMN detected_price REAL",
            ),
            (
                "peak_price",
                "ALTER TABLE trending_comparisons ADD COLUMN peak_price REAL",
            ),
            (
                "peak_gain_pct",
                "ALTER TABLE trending_comparisons ADD COLUMN peak_gain_pct REAL",
            ),
            (
                "detected_by_social",
                "ALTER TABLE trending_comparisons ADD COLUMN detected_by_social INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "social_detected_at",
                "ALTER TABLE trending_comparisons ADD COLUMN social_detected_at TEXT",
            ),
            (
                "social_lead_minutes",
                "ALTER TABLE trending_comparisons ADD COLUMN social_lead_minutes REAL",
            ),
        ):
            if col not in tc_cols:
                await self._conn.execute(ddl)

        # Migrate gainers_comparisons: add peak tracking columns
        cursor = await self._conn.execute("PRAGMA table_info(gainers_comparisons)")
        gc_cols = {row[1] for row in await cursor.fetchall()}
        for col, ddl in (
            (
                "detected_price",
                "ALTER TABLE gainers_comparisons ADD COLUMN detected_price REAL",
            ),
            (
                "peak_price",
                "ALTER TABLE gainers_comparisons ADD COLUMN peak_price REAL",
            ),
            (
                "peak_gain_pct",
                "ALTER TABLE gainers_comparisons ADD COLUMN peak_gain_pct REAL",
            ),
        ):
            if col not in gc_cols:
                await self._conn.execute(ddl)

        await self._conn.commit()

    async def _migrate_feedback_loop_schema(self) -> None:
        """Per-column additive migration for feedback loop. Idempotent. Atomic."""
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        try:
            await conn.execute("BEGIN EXCLUSIVE")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS combo_performance (
                    combo_key TEXT NOT NULL,
                    window TEXT NOT NULL,
                    trades INTEGER NOT NULL,
                    wins INTEGER NOT NULL,
                    losses INTEGER NOT NULL,
                    total_pnl_usd REAL NOT NULL,
                    avg_pnl_pct REAL NOT NULL,
                    win_rate_pct REAL NOT NULL,
                    suppressed INTEGER NOT NULL DEFAULT 0,
                    suppressed_at TEXT,
                    parole_at TEXT,
                    parole_trades_remaining INTEGER,
                    refresh_failures INTEGER NOT NULL DEFAULT 0,
                    last_refreshed TEXT NOT NULL,
                    PRIMARY KEY (combo_key, window)
                )
            """)

            expected_cols = {
                "signal_combo": "TEXT",
                "lead_time_vs_trending_min": "REAL",
                "lead_time_vs_trending_status": "TEXT",
                "would_be_live": "INTEGER",
                # BL-061 ladder state
                "leg_1_filled_at": "TEXT",
                "leg_1_exit_price": "REAL",
                "leg_2_filled_at": "TEXT",
                "leg_2_exit_price": "REAL",
                "remaining_qty": "REAL",
                "floor_armed": "INTEGER",
                "realized_pnl_usd": "REAL",
                # BL-062 peak-fade exit marker (NULL until fire)
                "peak_fade_fired_at": "TEXT",
                # BL-063 moonshot exit upgrade — NULL until armed when peak_pct
                # crosses the moonshot threshold; original_trail snapshot at
                # arm time for post-mortem analysis.
                "moonshot_armed_at": "TEXT",
                "original_trail_drawdown_pct": "REAL",
            }
            cur = await conn.execute("PRAGMA table_info(paper_trades)")
            existing = {row[1] for row in await cur.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing:
                    _log.info(
                        "schema_migration_column_action", col=col, action="skip_exists"
                    )
                else:
                    await conn.execute(
                        f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}"
                    )
                    _log.info("schema_migration_column_action", col=col, action="added")

            # BL-061: cutover timestamp captured once per schema version
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY,
                    cutover_ts TEXT NOT NULL
                )
            """)
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl061_ladder", datetime.now(timezone.utc).isoformat()),
            )

            # BL-062: peak-fade cutover row + index on fire-time column
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl062_peak_fade", datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_peak_fade_fired_at "
                "ON paper_trades(peak_fade_fired_at) "
                "WHERE peak_fade_fired_at IS NOT NULL"
            )

            # BL-063: moonshot cutover row + partial index on arm time.
            # Per BL-060 lesson: CREATE INDEX lives in this migration step,
            # NOT in _create_tables (which is a no-op for existing tables).
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl063_moonshot", datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_moonshot_armed_at "
                "ON paper_trades(moonshot_armed_at) "
                "WHERE moonshot_armed_at IS NOT NULL"
            )

            # BL-064: TG social signals — six tables, indexes in migration step.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_social_channels (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_handle  TEXT NOT NULL UNIQUE,
                    display_name    TEXT NOT NULL,
                    trade_eligible  INTEGER NOT NULL DEFAULT 1,
                    safety_required INTEGER NOT NULL DEFAULT 1,
                    added_at        TEXT NOT NULL,
                    removed_at      TEXT
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_social_watermarks (
                    channel_handle    TEXT PRIMARY KEY,
                    last_seen_msg_id  INTEGER NOT NULL DEFAULT 0,
                    updated_at        TEXT NOT NULL
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_social_messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_handle  TEXT NOT NULL,
                    msg_id          INTEGER NOT NULL,
                    posted_at       TEXT NOT NULL,
                    sender          TEXT,
                    text            TEXT,
                    cashtags        TEXT,
                    contracts       TEXT,
                    urls            TEXT,
                    parsed_at       TEXT NOT NULL,
                    UNIQUE(channel_handle, msg_id)
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_social_signals (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_pk             INTEGER NOT NULL,
                    token_id               TEXT NOT NULL,
                    symbol                 TEXT NOT NULL,
                    contract_address       TEXT,
                    chain                  TEXT,
                    mcap_at_sighting       REAL,
                    resolution_state       TEXT NOT NULL,
                    source_channel_handle  TEXT NOT NULL,
                    alert_sent_at          TEXT,
                    paper_trade_id         INTEGER,
                    created_at             TEXT NOT NULL,
                    FOREIGN KEY (message_pk) REFERENCES tg_social_messages(id),
                    -- BL-055 contract: paper_trades is append-only; mirror the
                    -- ON DELETE RESTRICT pattern from live_trades.paper_trade_id
                    -- so an accidental DELETE on paper_trades cannot orphan
                    -- tg_social_signals references.
                    FOREIGN KEY (paper_trade_id) REFERENCES paper_trades(id) ON DELETE RESTRICT
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_social_health (
                    component        TEXT PRIMARY KEY,
                    listener_state   TEXT NOT NULL,
                    last_message_at  TEXT,
                    updated_at       TEXT NOT NULL,
                    detail           TEXT
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_social_dlq (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_handle  TEXT NOT NULL,
                    msg_id          INTEGER NOT NULL,
                    raw_text        TEXT,
                    error_class     TEXT NOT NULL,
                    error_text      TEXT NOT NULL,
                    failed_at       TEXT NOT NULL,
                    retried_at      TEXT
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_social_signals_token_created "
                "ON tg_social_signals(token_id, created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_social_signals_channel_created "
                "ON tg_social_signals(source_channel_handle, created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_social_signals_paper_trade_id "
                "ON tg_social_signals(paper_trade_id) "
                "WHERE paper_trade_id IS NOT NULL"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_social_messages_channel_msgid "
                "ON tg_social_messages(channel_handle, msg_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_social_dlq_failed_at "
                "ON tg_social_dlq(failed_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl064_tg_social", datetime.now(timezone.utc).isoformat()),
            )

            # Per-channel safety_required column (added 2026-04-28).
            # Pre-existing rows backfill to 1 (strict) by the NOT NULL DEFAULT,
            # preserving fail-closed behavior for already-deployed channels.
            cur = await conn.execute("PRAGMA table_info(tg_social_channels)")
            tg_chan_cols = {row[1] for row in await cur.fetchall()}
            if "safety_required" in tg_chan_cols:
                _log.info(
                    "schema_migration_column_action",
                    col="safety_required",
                    action="skip_exists",
                )
            else:
                await conn.execute(
                    "ALTER TABLE tg_social_channels "
                    "ADD COLUMN safety_required INTEGER NOT NULL DEFAULT 1"
                )
                _log.info(
                    "schema_migration_column_action",
                    col="safety_required",
                    action="added",
                )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (
                    "bl064_safety_required_per_channel",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            # BL-071b (Bundle A 2026-05-03): convert pre-stamped EXPIRED
            # narrative rows to NULL so the hydrator can re-evaluate them
            # against the predictions table. Bounded scope: narrative pipeline
            # only, EXPIRED-with-no-evaluated_at only. Memecoin EXPIRED rows
            # are left alone (their outcome path is BL-071a, not BL-071b).
            # Idempotent: gated by paper_migrations row; second run is a no-op.
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl071b_unstamp_expired_narrative",),
            )
            if not await cur.fetchone():
                await conn.execute("""UPDATE chain_matches
                          SET outcome_class = NULL
                        WHERE pipeline = 'narrative'
                          AND outcome_class = 'EXPIRED'
                          AND evaluated_at IS NULL""")
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (
                        "bl071b_unstamp_expired_narrative",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # BL-071a partial (Bundle A 2026-05-03): add mcap_at_completion
            # column to chain_matches. Hydrator (Task 3) reads it; writers
            # (BL-071a' follow-up) will populate it. PRAGMA-guarded ALTER,
            # idempotent. Mirrors the BL-071b pattern above: gate BOTH the
            # ALTER and the paper_migrations insert on the same condition
            # for internal consistency (per PR-review R3 #1). The else
            # branch covers the partial-state recovery case (column landed
            # on a previous startup but the marker insert didn't).
            cur = await conn.execute("PRAGMA table_info(chain_matches)")
            cm_cols = {row[1] for row in await cur.fetchall()}
            if "mcap_at_completion" not in cm_cols:
                await conn.execute(
                    "ALTER TABLE chain_matches ADD COLUMN mcap_at_completion REAL"
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (
                        "bl071a_chain_matches_mcap_at_completion",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            else:
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (
                        "bl071a_chain_matches_mcap_at_completion",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # BL-065 v3 (Bundle B 2026-05-04): per-channel cashtag dispatch
            # opt-in. Default 0 = fail-closed; operators explicitly UPDATE
            # to 1 per known-good curator. Independent of trade_eligible
            # (the CA-path flag) and safety_required (the no-record-pass
            # flag) — three flags = three independent concerns.
            cur = await conn.execute("PRAGMA table_info(tg_social_channels)")
            tg_chan_cols2 = {row[1] for row in await cur.fetchall()}
            if "cashtag_trade_eligible" not in tg_chan_cols2:
                await conn.execute(
                    "ALTER TABLE tg_social_channels "
                    "ADD COLUMN cashtag_trade_eligible INTEGER NOT NULL DEFAULT 0"
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (
                        "bl065_cashtag_trade_eligible",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            # else: column already exists; paper_migrations row was
            # inserted at that prior run (R2#4 NIT v2 — matches BL-061..
            # BL-064 pattern, no need to re-INSERT every cold-start).

            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_combo_opened "
                "ON paper_trades(signal_combo, opened_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_token_opened "
                "ON paper_trades(token_id, opened_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_would_be_live_status "
                "ON paper_trades(would_be_live, status)"
            )

            # POST-ASSERTION — run BEFORE commit so a failure triggers ROLLBACK
            # (per D18: partial schema must not persist on assertion failure).
            cur = await conn.execute("PRAGMA table_info(paper_trades)")
            final = {row[1] for row in await cur.fetchall()}
            missing = set(expected_cols) - final
            if missing:
                raise RuntimeError(f"Schema migration incomplete: missing {missing}")

            # BL-063/BL-064/Bundle A defense-in-depth: confirm cutover rows
            # are present. Bundle A added bl071b_unstamp_expired_narrative
            # and bl071a_chain_matches_mcap_at_completion.
            cur = await conn.execute(
                "SELECT name FROM paper_migrations WHERE name IN "
                "('bl061_ladder', 'bl062_peak_fade', 'bl063_moonshot', "
                "'bl064_tg_social', 'bl064_safety_required_per_channel', "
                "'bl071b_unstamp_expired_narrative', "
                "'bl071a_chain_matches_mcap_at_completion', "
                "'bl065_cashtag_trade_eligible')"
            )
            recorded = {row[0] for row in await cur.fetchall()}
            missing_migrations = {
                "bl061_ladder",
                "bl062_peak_fade",
                "bl063_moonshot",
                "bl064_tg_social",
                "bl064_safety_required_per_channel",
                "bl071b_unstamp_expired_narrative",
                "bl071a_chain_matches_mcap_at_completion",
                "bl065_cashtag_trade_eligible",
            } - recorded
            if missing_migrations:
                raise RuntimeError(
                    f"paper_migrations missing rows: {missing_migrations}"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (20260418, datetime.now(timezone.utc).isoformat(), "feedback_loop_v1"),
            )
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED")
            raise

    async def _migrate_live_trading_schema(self) -> None:
        """BL-055: shadow/live ledgers, kill events, venue overrides, resolver
        cache, daily metrics. One atomic migration. Idempotent via IF NOT EXISTS.

        Note: ``paper_trades`` becomes append-only by contract — the two new
        ledger tables reference it via ``ON DELETE RESTRICT``. Existing rows are
        untouched; only new DELETE attempts from foreign-key-bearing children
        are blocked.

        Implementation pattern mirrors :meth:`_migrate_feedback_loop_schema`:
        ``BEGIN EXCLUSIVE`` + per-statement ``execute`` + explicit
        ``commit``/``ROLLBACK``. Do NOT use ``executescript`` —
        ``aiosqlite.Connection.executescript`` issues an implicit COMMIT before
        running the script, which defeats rollback semantics. See
        ``feedback_ddl_before_alter.md`` for the BL-060 crash pattern this
        migration style avoids.

        All indexes MUST live in this migration (never in ``_create_tables``)
        because ``CREATE TABLE IF NOT EXISTS`` is a no-op on pre-existing
        tables, so any paired index declaration would silently skip on the
        upgrade path.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn

        # Spec §3.1 — one statement per list entry, in the order shown in the
        # spec. CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS throughout
        # for idempotency. CHECK constraints are mandatory (status enum,
        # live_control singleton, venue_overrides.disabled, resolver_cache outcome,
        # kill_events.triggered_by / cleared_by).
        ddl_statements: list[str] = [
            # 0. schema_version — normally created by _migrate_feedback_loop_schema,
            #    but defensively ensure it exists so this migration is self-
            #    contained (needed for tests that skip the feedback migration).
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT
            )
            """,
            # 1. shadow_trades — append-only shadow ledger.
            """
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id      INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE RESTRICT,
                coin_id             TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                venue               TEXT NOT NULL,
                pair                TEXT NOT NULL,
                signal_type         TEXT NOT NULL,
                size_usd            TEXT NOT NULL,
                entry_walked_vwap   TEXT,
                mid_at_entry        TEXT,
                entry_slippage_bps  INTEGER,
                status              TEXT NOT NULL CHECK (status IN (
                    'open','closed_tp','closed_sl','closed_duration','closed_via_reconciliation',
                    'rejected','needs_manual_review'
                )),
                reject_reason       TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
                    'no_venue','insufficient_depth','slippage_exceeds_cap','insufficient_balance',
                    'daily_cap_hit','kill_switch','exposure_cap','override_disabled',
                    'venue_unavailable'
                )),
                exit_walked_vwap    TEXT,
                realized_pnl_usd    TEXT,
                realized_pnl_pct    TEXT,
                review_retries      INTEGER NOT NULL DEFAULT 0,
                next_review_at      TEXT,
                kill_event_id       INTEGER REFERENCES kill_events(id),
                created_at          TEXT NOT NULL,
                closed_at           TEXT
            )
            """,
            # 2. live_trades — append-only live ledger (same shape, separate table
            #    per spec Q3=C three-table isolation).
            """
            CREATE TABLE IF NOT EXISTS live_trades (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id      INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE RESTRICT,
                coin_id             TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                venue               TEXT NOT NULL,
                pair                TEXT NOT NULL,
                signal_type         TEXT NOT NULL,
                size_usd            TEXT NOT NULL,
                entry_order_id      TEXT,
                entry_fill_price    TEXT,
                entry_fill_qty      TEXT,
                mid_at_entry        TEXT,
                entry_slippage_bps  INTEGER,
                status              TEXT NOT NULL CHECK (status IN (
                    'open','closed_tp','closed_sl','closed_duration','closed_via_reconciliation',
                    'rejected','needs_manual_review'
                )),
                reject_reason       TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
                    'no_venue','insufficient_depth','slippage_exceeds_cap','insufficient_balance',
                    'daily_cap_hit','kill_switch','exposure_cap','override_disabled',
                    'venue_unavailable'
                )),
                exit_order_id       TEXT,
                exit_fill_price     TEXT,
                realized_pnl_usd    TEXT,
                realized_pnl_pct    TEXT,
                kill_event_id       INTEGER REFERENCES kill_events(id),
                created_at          TEXT NOT NULL,
                closed_at           TEXT
            )
            """,
            # 3. kill_events — append-only audit log of daily-loss-cap trips etc.
            """
            CREATE TABLE IF NOT EXISTS kill_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                triggered_at    TEXT NOT NULL,
                triggered_by    TEXT NOT NULL CHECK (triggered_by IN ('daily_loss_cap','manual','ops_maintenance')),
                reason          TEXT,
                killed_until    TEXT NOT NULL,
                cleared_at      TEXT,
                cleared_by      TEXT CHECK (cleared_by IS NULL OR cleared_by IN ('manual','auto_expired'))
            )
            """,
            # 4. live_control — single-row pointer (id=1 always exists after
            #    migration; singleton enforced by CHECK (id=1)).
            """
            CREATE TABLE IF NOT EXISTS live_control (
                id                          INTEGER PRIMARY KEY CHECK (id = 1),
                active_kill_event_id        INTEGER REFERENCES kill_events(id)
            )
            """,
            # 5. venue_overrides — operator-controlled fallback for resolver.
            """
            CREATE TABLE IF NOT EXISTS venue_overrides (
                symbol          TEXT PRIMARY KEY,
                venue           TEXT NOT NULL,
                pair            TEXT NOT NULL,
                note            TEXT,
                disabled        INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0,1)),
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """,
            # 6. resolver_cache — persistent resolver cache (1h positive / 60s
            #    negative, managed by caller).
            """
            CREATE TABLE IF NOT EXISTS resolver_cache (
                symbol          TEXT PRIMARY KEY,
                outcome         TEXT NOT NULL CHECK (outcome IN ('positive','negative')),
                venue           TEXT,
                pair            TEXT,
                resolved_at     TEXT NOT NULL,
                expires_at      TEXT NOT NULL
            )
            """,
            # 7. live_metrics_daily — UPSERT-friendly daily counters.
            """
            CREATE TABLE IF NOT EXISTS live_metrics_daily (
                date    TEXT NOT NULL,
                metric  TEXT NOT NULL,
                value   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, metric)
            )
            """,
            # 8-10. Indexes per spec §3.1. MUST live in this migration — see
            # feedback_ddl_before_alter.md.
            (
                "CREATE INDEX IF NOT EXISTS idx_shadow_status_evaluated "
                "ON shadow_trades(status, next_review_at) "
                "WHERE status IN ('open','needs_manual_review')"
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_shadow_closed_at_utc "
                "ON shadow_trades(closed_at) WHERE closed_at IS NOT NULL"
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_kill_events_active "
                "ON kill_events(cleared_at) WHERE cleared_at IS NULL"
            ),
        ]

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            for stmt in ddl_statements:
                await conn.execute(stmt)
            # Seed live_control with id=1 ONLY if not already present (idempotent
            # via INSERT OR IGNORE — safe to re-run on every startup).
            await conn.execute(
                "INSERT OR IGNORE INTO live_control (id, active_kill_event_id) "
                "VALUES (1, NULL)"
            )
            # Bump schema_version inside the same transaction so migration +
            # version stamp commit atomically.
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (
                    20260423,
                    datetime.now(timezone.utc).isoformat(),
                    "bl055_live_trading_v1",
                ),
            )
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED")
            raise

    async def _migrate_signal_params_schema(self) -> None:
        """Tier 1a + 1b: per-signal-type ladder/SL params + audit log.

        Mirrors the BL-055 / BL-061 migration style: ``BEGIN EXCLUSIVE`` +
        per-statement execute + explicit commit/ROLLBACK. No ``executescript``
        (implicit COMMIT defeats rollback). No explicit ``BEGIN IMMEDIATE``
        (per BL-064 lesson — matches project _txn_lock pattern).

        Idempotent on every dimension: ``CREATE TABLE IF NOT EXISTS``,
        ``CREATE INDEX IF NOT EXISTS``, ``INSERT OR IGNORE`` on the seed rows
        and the cutover marker.

        Seed values come from current Settings, so the first ``--apply``
        diff is a no-op until the operator actually wants new values.
        """
        import structlog

        from scout.config import Settings

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn

        # Local import to avoid a config -> db -> trading.params -> db cycle.
        # DEFAULT_SIGNAL_TYPES lives in trading.params; importing it here keeps
        # the seed list in one place.
        from scout.trading.params import DEFAULT_SIGNAL_TYPES

        ddl_statements: list[str] = [
            # Defensive create — if upstream feedback migration was skipped
            # or monkey-patched (test_trading_db_migration.py exercises this),
            # paper_migrations may not exist yet. CREATE IF NOT EXISTS keeps
            # the cutover INSERT safe and is idempotent against the canonical
            # creation in _migrate_feedback_loop_schema.
            """
            CREATE TABLE IF NOT EXISTS paper_migrations (
                name TEXT PRIMARY KEY,
                cutover_ts TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS signal_params (
                signal_type             TEXT PRIMARY KEY,
                leg_1_pct               REAL    NOT NULL,
                leg_1_qty_frac          REAL    NOT NULL,
                leg_2_pct               REAL    NOT NULL,
                leg_2_qty_frac          REAL    NOT NULL,
                trail_pct               REAL    NOT NULL,
                trail_pct_low_peak      REAL    NOT NULL,
                low_peak_threshold_pct  REAL    NOT NULL,
                sl_pct                  REAL    NOT NULL,
                max_duration_hours      INTEGER NOT NULL,
                enabled                 INTEGER NOT NULL DEFAULT 1,
                suspended_at            TEXT,
                suspended_reason        TEXT,
                updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_by              TEXT    NOT NULL,
                last_calibration_at     TEXT,
                last_calibration_reason TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS signal_params_audit (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type     TEXT NOT NULL,
                field_name      TEXT NOT NULL,
                old_value       TEXT,
                new_value       TEXT,
                reason          TEXT NOT NULL,
                applied_by      TEXT NOT NULL,
                applied_at      TEXT NOT NULL
            )
            """,
            (
                "CREATE INDEX IF NOT EXISTS idx_signal_params_audit_signal_at "
                "ON signal_params_audit(signal_type, applied_at)"
            ),
        ]

        # Seed values come from Settings *class-level defaults* (not a fresh
        # Settings() — that would require env vars at migration time, including
        # secrets the test environment doesn't have). model_fields stays in
        # sync with the Settings class without needing .env. This is a one-shot
        # seed; subsequent calibration/operator updates are the source of truth.
        fields = Settings.model_fields
        defaults = {
            name: fields[name].default
            for name in (
                "PAPER_LADDER_LEG_1_PCT",
                "PAPER_LADDER_LEG_1_QTY_FRAC",
                "PAPER_LADDER_LEG_2_PCT",
                "PAPER_LADDER_LEG_2_QTY_FRAC",
                "PAPER_LADDER_TRAIL_PCT",
                "PAPER_LADDER_TRAIL_PCT_LOW_PEAK",
                "PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT",
                "PAPER_SL_PCT",
                "PAPER_MAX_DURATION_HOURS",
            )
        }
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            for stmt in ddl_statements:
                await conn.execute(stmt)

            # Seed one row per known signal_type.
            for signal_type in sorted(DEFAULT_SIGNAL_TYPES):
                await conn.execute(
                    """INSERT OR IGNORE INTO signal_params (
                        signal_type, leg_1_pct, leg_1_qty_frac,
                        leg_2_pct, leg_2_qty_frac, trail_pct,
                        trail_pct_low_peak, low_peak_threshold_pct,
                        sl_pct, max_duration_hours,
                        enabled, updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'seed')""",
                    (
                        signal_type,
                        defaults["PAPER_LADDER_LEG_1_PCT"],
                        defaults["PAPER_LADDER_LEG_1_QTY_FRAC"],
                        defaults["PAPER_LADDER_LEG_2_PCT"],
                        defaults["PAPER_LADDER_LEG_2_QTY_FRAC"],
                        defaults["PAPER_LADDER_TRAIL_PCT"],
                        defaults["PAPER_LADDER_TRAIL_PCT_LOW_PEAK"],
                        defaults["PAPER_LADDER_LOW_PEAK_THRESHOLD_PCT"],
                        defaults["PAPER_SL_PCT"],
                        defaults["PAPER_MAX_DURATION_HOURS"],
                        now_iso,
                    ),
                )

            # Behavioural cutover marker (matches BL-061..BL-064 pattern).
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("signal_params_v1", now_iso),
            )
            # Code-level schema version stamp (matches BL-055 pattern).
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260429, now_iso, "tier_1a_signal_params_v1"),
            )

            # BL-067 conviction-lock: add conviction_lock_enabled column on
            # signal_params + conviction_locked_at/conviction_locked_stack
            # columns on paper_trades. Idempotent guards via PRAGMA.
            #
            # design-v2 adv-M4: INSERT OR IGNORE INTO paper_migrations is
            # OUTSIDE the column-existence guard. Otherwise, partial-failure
            # on first run (column applied + cutover row absent) would leave
            # the post-migration assertion permanently failing on every
            # subsequent run because PRAGMA sees the column → skips the
            # entire `if` block including the marker INSERT.
            cur_pragma = await conn.execute(
                "PRAGMA table_info(signal_params)"
            )
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "conviction_lock_enabled" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params "
                    "ADD COLUMN conviction_lock_enabled INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            # Marker INSERT — UNCONDITIONAL per M4 fix.
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations "
                "(name, cutover_ts) VALUES (?, ?)",
                ("bl067_conviction_lock_enabled", now_iso),
            )

            # design-v2 arch-D1: paper_trades.conviction_locked_at +
            # conviction_locked_stack added in same migration. Avoids
            # unreliable backfill of historical locked rows once source
            # tables age out.
            cur_pragma_pt = await conn.execute(
                "PRAGMA table_info(paper_trades)"
            )
            existing_pt_cols = {
                row[1] for row in await cur_pragma_pt.fetchall()
            }
            if "conviction_locked_at" not in existing_pt_cols:
                await conn.execute(
                    "ALTER TABLE paper_trades "
                    "ADD COLUMN conviction_locked_at TEXT"
                )
            if "conviction_locked_stack" not in existing_pt_cols:
                await conn.execute(
                    "ALTER TABLE paper_trades "
                    "ADD COLUMN conviction_locked_stack INTEGER"
                )

            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="signal_params_v1")
            raise

        # Post-assertion — cutover row must exist.
        # Limitation: INSERT OR IGNORE makes the marker idempotent, so on a
        # second run this assertion passes regardless of body completion. It
        # only catches the "row never existed" case (first-run failure where
        # the entire migration body silently no-op'd). Stronger assertion
        # would be a row count on signal_params, but that re-creates a
        # different fragility class — the marker pattern matches BL-061..64.
        cur = await conn.execute(
            "SELECT 1 FROM paper_migrations WHERE name = ?",
            ("signal_params_v1",),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("signal_params_v1 cutover row missing after migration")

        # BL-067 post-migration assertion (M3) — paralleling
        # signal_params_v1 above. design-v2 adv-M4 makes the cutover-row
        # INSERT unconditional so this assertion is the catch for the
        # "INSERT-OR-IGNORE was somehow not applied" pathological case
        # (rare but loud-on-startup).
        cur = await conn.execute(
            "SELECT 1 FROM paper_migrations WHERE name = ?",
            ("bl067_conviction_lock_enabled",),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "bl067_conviction_lock_enabled cutover row missing after migration"
            )

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
        self,
        contract_address: str,
        chain: str,
        conviction_score: float,
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
            (
                contract_address,
                chain,
                conviction_score,
                alert_market_cap,
                price_usd,
                token_name,
                ticker,
                now,
            ),
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
        self,
        alert_id: int,
        contract_address: str,
        alert_price: float,
        check_price: float,
        price_change_pct: float,
    ) -> None:
        """Record an outcome for an alert."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT OR REPLACE INTO outcomes
               (id, contract_address, alert_price, check_price, check_time, price_change_pct)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                alert_id,
                contract_address,
                alert_price,
                check_price,
                now,
                price_change_pct,
            ),
        )
        await self._conn.commit()

    async def was_recently_alerted(self, contract_address: str, hours: int = 4) -> bool:
        """Check if a token was alerted within the last N hours."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE contract_address = ? AND datetime(alerted_at) >= datetime('now', ?)",
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

    async def log_holder_snapshot(
        self, contract_address: str, holder_count: int
    ) -> None:
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
            "SELECT holder_count FROM holder_snapshots WHERE contract_address = ? ORDER BY scanned_at DESC, id DESC LIMIT 1",
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

    async def get_recent_scores(
        self, contract_address: str, limit: int = 3
    ) -> list[float]:
        """Get the most recent scores for a contract, newest first."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT score FROM score_history WHERE contract_address = ? ORDER BY scanned_at DESC, id DESC LIMIT ?",
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
               WHERE contract_address = ? AND datetime(scanned_at) >= datetime('now', '-7 days')
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
            "SELECT COUNT(*) FROM alerts WHERE date(alerted_at) = ?",
            (today,),
        )
        alerts_today = (await cursor.fetchone())[0]

        # Win rate for alerts older than 4 hours
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM outcomes o
               JOIN alerts a ON o.id = a.id
               WHERE date(a.alerted_at) = ?
               AND datetime(a.alerted_at) <= datetime('now', '-4 hours')""",
            (today,),
        )
        outcomes_total = (await cursor.fetchone())[0]

        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM outcomes o
               JOIN alerts a ON o.id = a.id
               WHERE date(a.alerted_at) = ?
               AND datetime(a.alerted_at) <= datetime('now', '-4 hours')
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
            "win_rate_pct": round(
                (outcomes_wins / outcomes_total * 100) if outcomes_total > 0 else 0, 1
            ),
            "top_signal_combo": top_signal_combo,
            "top_tokens": top_tokens,
        }

    async def prune_old_candidates(self, keep_days: int = 7) -> int:
        """Delete candidates older than keep_days. Returns rows deleted."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "DELETE FROM candidates WHERE datetime(first_seen_at) < datetime('now', ?)",
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
               WHERE datetime(a.alerted_at) <= datetime('now', '-' || ? || ' days')
                 AND datetime(a.alerted_at) >= datetime('now', '-' || ? || ' days')
                 AND a.contract_address NOT IN (
                     SELECT contract_address FROM second_wave_candidates
                     WHERE datetime(detected_at) >= datetime('now', '-' || ? || ' days')
                 )
               GROUP BY a.contract_address
               HAVING peak_quant_score >= ?""",
            (
                int(min_age_days),
                int(max_age_days),
                int(dedup_days),
                int(min_peak_score),
            ),
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
                 AND datetime(detected_at) >= datetime('now', '-' || ? || ' days')""",
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
               WHERE datetime(detected_at) >= datetime('now', '-' || ? || ' days')
               ORDER BY reaccumulation_score DESC""",
            (int(days),),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        for r in rows:
            r["peak_signals_fired"] = json.loads(r.get("peak_signals_fired") or "[]")
            r["reaccumulation_signals"] = json.loads(
                r.get("reaccumulation_signals") or "[]"
            )
            r["price_is_stale"] = bool(r.get("price_is_stale", 0))
        return rows

    # ------------------------------------------------------------------
    # Price cache (for dashboard enrichment)
    # ------------------------------------------------------------------

    async def cache_prices(self, raw_coins: list[dict]) -> int:
        """Bulk-upsert price data from a CoinGecko /coins/markets response.

        Returns the number of rows upserted.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for coin in raw_coins:
            cid = coin.get("id")
            if not cid:
                continue
            await self._conn.execute(
                """INSERT OR REPLACE INTO price_cache
                   (coin_id, current_price, price_change_24h, price_change_7d,
                    market_cap, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    coin.get("current_price"),
                    coin.get("price_change_percentage_24h"),
                    coin.get("price_change_percentage_7d_in_currency"),
                    coin.get("market_cap"),
                    now,
                ),
            )
            count += 1
        if count:
            await self._conn.commit()
        return count

    async def get_cached_prices(self, coin_ids: list[str]) -> dict[str, dict]:
        """Read price cache rows for the given coin IDs.

        Returns {coin_id: {usd, change_24h, change_7d, market_cap}} mapping.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not coin_ids:
            return {}
        placeholders = ",".join("?" * len(coin_ids))
        cursor = await self._conn.execute(
            f"SELECT coin_id, current_price, price_change_24h, price_change_7d, market_cap "
            f"FROM price_cache WHERE coin_id IN ({placeholders})",
            coin_ids,
        )
        rows = await cursor.fetchall()
        return {
            row[0]: {
                "usd": row[1],
                "change_24h": row[2],
                "change_7d": row[3],
                "market_cap": row[4],
            }
            for row in rows
        }

    async def get_volume_history(
        self, contract_address: str, days: int = 14
    ) -> list[float]:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT volume_24h_usd FROM volume_snapshots
               WHERE contract_address = ?
                 AND datetime(scanned_at) >= datetime('now', '-' || ? || ' days')
               ORDER BY scanned_at DESC""",
            (contract_address, int(days)),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Briefings
    # ------------------------------------------------------------------

    async def store_briefing(
        self,
        briefing_type: str,
        raw_data: str,
        synthesis: str,
        model_used: str,
        tokens_used: int | None = None,
        created_at: str | None = None,
    ) -> int:
        """Insert a briefing row and return its id."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if created_at is None:
            created_at = datetime.now(timezone.utc).isoformat()
        cursor = await self._conn.execute(
            """INSERT INTO briefings (briefing_type, raw_data, synthesis, model_used, tokens_used, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (briefing_type, raw_data, synthesis, model_used, tokens_used, created_at),
        )
        await self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_latest_briefing(self) -> dict | None:
        """Return the most recent briefing row, or None."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT * FROM briefings ORDER BY created_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_briefing_history(self, limit: int = 10) -> list[dict]:
        """Return recent briefings (most recent first)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            "SELECT id, briefing_type, synthesis, model_used, tokens_used, created_at FROM briefings ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_last_briefing_time(self) -> str | None:
        """Return the created_at of the most recent briefing, or None."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute("SELECT MAX(created_at) FROM briefings")
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    # ------------------------------------------------------------------
    # Perp anomalies
    # ------------------------------------------------------------------

    async def insert_perp_anomaly(self, anomaly: "PerpAnomaly") -> None:
        """Insert a single anomaly. Kept for tests; prefer batch in hot path.

        Uses INSERT OR IGNORE to preserve idempotency across reconnect/replay
        -- the UNIQUE(exchange, symbol, kind, observed_at) constraint prevents
        duplicate rows.
        """
        await self._conn.execute(
            "INSERT OR IGNORE INTO perp_anomalies "
            "(exchange, symbol, ticker, kind, magnitude, baseline, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                anomaly.exchange,
                anomaly.symbol,
                anomaly.ticker,
                anomaly.kind,
                anomaly.magnitude,
                anomaly.baseline,
                anomaly.observed_at.isoformat(),
            ),
        )
        await self._conn.commit()

    async def insert_perp_anomalies_batch(self, rows: list["PerpAnomaly"]) -> int:
        """Primary write path. Single transaction via executemany.

        Returns the count of input rows, NOT the count of rows actually written
        (INSERT OR IGNORE silently de-dupes against the UNIQUE constraint, so
        some input rows may be skipped). Callers that need exact write counts
        should query the table afterwards.

        Uses INSERT OR IGNORE against the UNIQUE constraint on
        (exchange, symbol, kind, observed_at) so replays after a WS reconnect
        do not create duplicate rows.
        """
        if not rows:
            return 0
        # Pre-validate rows: skip any with non-finite magnitude or naive observed_at
        # to prevent one bad row from nuking the whole executemany transaction.
        payload = []
        for a in rows:
            if not math.isfinite(a.magnitude):
                _db_log.warning(
                    "perp_anomaly_batch_skip_bad_row",
                    reason="non_finite_magnitude",
                    exchange=a.exchange,
                    symbol=a.symbol,
                    magnitude=a.magnitude,
                )
                continue
            if a.observed_at.tzinfo is None:
                _db_log.warning(
                    "perp_anomaly_batch_skip_bad_row",
                    reason="naive_observed_at",
                    exchange=a.exchange,
                    symbol=a.symbol,
                )
                continue
            payload.append(
                (
                    a.exchange,
                    a.symbol,
                    a.ticker,
                    a.kind,
                    a.magnitude,
                    a.baseline,
                    a.observed_at.isoformat(),
                )
            )
        if not payload:
            return 0
        await self._conn.executemany(
            "INSERT OR IGNORE INTO perp_anomalies "
            "(exchange, symbol, ticker, kind, magnitude, baseline, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        await self._conn.commit()
        return len(rows)

    async def fetch_recent_perp_anomalies(
        self,
        *,
        tickers: list[str],
        since: datetime,
        limit: int = 100,
    ) -> list["PerpAnomaly"]:
        """Fetch recent anomalies for ``tickers`` after ``since``.

        ``limit`` caps row count to protect against pathological lookups on
        a tickers list that unexpectedly matches tens of thousands of rows.
        Callers that need an exhaustive read should pass an explicit value.
        """
        from scout.perp.schemas import PerpAnomaly

        if not tickers:
            return []
        placeholders = ",".join(["?"] * len(tickers))
        cur = await self._conn.execute(
            f"SELECT exchange, symbol, ticker, kind, magnitude, baseline, observed_at "
            f"FROM perp_anomalies "
            f"WHERE ticker IN ({placeholders}) AND observed_at >= ? "
            f"ORDER BY observed_at DESC "
            f"LIMIT ?",
            (*tickers, since.isoformat(), limit),
        )
        fetched = await cur.fetchall()
        return [
            PerpAnomaly(
                exchange=r[0],
                symbol=r[1],
                ticker=r[2],
                kind=r[3],
                magnitude=r[4],
                baseline=r[5],
                observed_at=datetime.fromisoformat(r[6]),
            )
            for r in fetched
        ]

    async def prune_perp_anomalies(self, *, keep_days: int) -> int:
        """Delete anomaly rows older than ``keep_days``. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM perp_anomalies WHERE observed_at <= ?", (cutoff,)
        )
        await self._conn.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    # CryptoPanic posts
    # ------------------------------------------------------------------

    async def insert_cryptopanic_post(
        self,
        post: "CryptoPanicPost",
        *,
        is_macro: bool,
        sentiment: str,
    ) -> int:
        """INSERT OR IGNORE a CryptoPanic post. Returns rowcount (0 or 1)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        fetched_at = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.execute(
            """
            INSERT OR IGNORE INTO cryptopanic_posts (
                post_id, title, url, published_at, currencies_json,
                is_macro, sentiment, votes_positive, votes_negative, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post.post_id,
                post.title,
                post.url,
                post.published_at,
                json.dumps(post.currencies),
                1 if is_macro else 0,
                sentiment,
                post.votes_positive,
                post.votes_negative,
                fetched_at,
            ),
        )
        await self._conn.commit()
        return cur.rowcount

    async def fetch_all_cryptopanic_posts(self) -> list[dict]:
        """Return all rows (test helper)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cur = await self._conn.execute(
            "SELECT * FROM cryptopanic_posts ORDER BY published_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def prune_cryptopanic_posts(self, *, keep_days: int) -> int:
        """Delete rows with published_at at or older than keep_days. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        # Use <= so that published_at == cutoff (boundary) prunes.
        # Rationale: keep_days=0 means "retain nothing as old as now",
        # and ISO-string comparisons can tie on low-resolution clocks
        # (observed on Windows). Semantics: "prune rows at or older
        # than keep_days."
        cur = await self._conn.execute(
            "DELETE FROM cryptopanic_posts WHERE published_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount

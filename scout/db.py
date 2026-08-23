"""Async SQLite database layer for CoinPump Scout."""

import asyncio
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import structlog

_db_log = structlog.get_logger(__name__)


# PR #72 H2 — distinct exception classes for `coin_id_resolves` so callers
# can emit `reason="db_not_initialized"` (catastrophic, page) vs
# `reason="resolution_check_error"` (transient, dashboard-aggregated).
class DbNotInitializedError(RuntimeError):
    """Raised when a Database method is called before initialize()."""


class CoinIdResolutionError(RuntimeError):
    """Raised by Database.coin_id_resolves on aiosqlite.OperationalError
    (column rename / table lock). Caller decides fail-CLOSED vs fail-OPEN."""


from scout.models import CandidateToken

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.news.schemas import CryptoPanicPost
    from scout.perp.schemas import PerpAnomaly

# F3 `alert_events` DDL, hoisted to module scope for ONE reason: the migration
# has to compare a live table's CHECK vocabulary against the current one, and a
# second copy of that vocabulary written out for comparison is a copy that can
# drift. The migration creates from this string, the drift check parses this
# string, and the tripwire test in tests/test_alert_events_migration.py reads
# this string. There is exactly one place the vocabulary lives.
_ALERT_EVENTS_DDL = """
CREATE TABLE alert_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    event_type       TEXT NOT NULL CHECK (event_type IN (
        'suppression_transition',
        'parole_slot_spent',
        'parole_slot_refunded',
        'reversal_pending_recorded',
        'alert_dispatched',
        'alert_delivered',
        'alert_failed',
        'marker_stamped',
        'marker_cleared',
        'marker_anomaly',
        'refresh_completed',
        'ledger_installed',
        'parole_denied'
    )),
    combo_key        TEXT,
    signal_type      TEXT,
    alert_source     TEXT,
    transition       TEXT,
    detected_at      TEXT,
    delivery_result  TEXT,
    retry            INTEGER,
    payload_hash     TEXT,
    state_json       TEXT,
    detail           TEXT
)
"""

_ALERT_EVENTS_COLUMNS = (
    "id, created_at, event_type, combo_key, signal_type, alert_source, "
    "transition, detected_at, delivery_result, retry, payload_hash, "
    "state_json, detail"
)

# The PREIMAGE substrate behind `alert_events.payload_hash`.
#
# WHAT THE DIGEST ALONE CANNOT DO. `payload_hash` proves that a body you already
# hold is the body that went out. It cannot hand you that body. For the
# suppression-axis pages the delivered text was recoverable from neither
# journald (~59h retention on this box, and it never logged the body) nor the
# ledger, so the ledger could prove integrity and not reconstruct what the
# operator was told.
#
# CONTENT-ADDRESSED, not one body per event row. The dispatched/delivered/failed
# triplet for one page shares one digest, and a retried page re-derives the same
# digest from the same bytes, so keying on the digest pays for the body exactly
# once no matter how many rows reference it. `alert_events` is unchanged and
# keeps referencing the digest.
#
# BLOB, not TEXT, and the justification is the requirement: exact-byte fidelity.
# `payload_digest` is defined over `body.encode("utf-8")`, so the stored value is
# those same bytes and verification is a re-hash with no decode step in between.
# A TEXT column would route the body through the driver's text handling on the
# way out (`text_factory`, and truncation at an embedded NUL), which is exactly
# the class of silent mutation a preimage cannot tolerate.
#
# No secondary index: `payload_hash` is the primary key and the only access path
# is "resolve this digest".
_ALERT_PAYLOADS_DDL = """
CREATE TABLE IF NOT EXISTS alert_payloads (
    payload_hash   TEXT PRIMARY KEY,
    payload        BLOB NOT NULL,
    byte_length    INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL
)
"""

# Serves the `parole_denied` first-occurrence probe
# (`WHERE event_type = ? AND payload_hash = ?`), which runs inside the locked
# reservation on every dispatch attempt against a table that is never pruned.
# This tuple is its ONLY definition, and TWO separate paths depend on that.
#
# The one that carries it on THIS deploy is the UNCONDITIONAL loop further down
# (`for index_sql in _ALERT_EVENTS_INDEXES`), which re-executes every statement
# here on every `initialize()`, outside the drift branch. Prod's `alert_events`
# CHECK already contains `parole_denied`, so the vocabulary-drift rebuild does
# NOT run on this deploy — that loop is the sole delivery mechanism, and
# "simplifying" it away would silently drop the index on every non-rebuild boot.
#
# The second path matters later: when the vocabulary DOES next grow, the rebuild
# DROPs `alert_events` and its indexes and re-attaches exactly what is listed
# here. An index created anywhere else would vanish at that point.
#
# `_migrate_alert_events_dedup_index_v1` therefore verifies rather than creates.
_ALERT_EVENTS_DEDUP_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_alert_events_dedup "
    "ON alert_events(event_type, payload_hash)"
)

_ALERT_EVENTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_alert_events_combo_created "
    "ON alert_events(combo_key, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_alert_events_type_created "
    "ON alert_events(event_type, created_at)",
    _ALERT_EVENTS_DEDUP_INDEX,
)


def _alert_event_check_vocabulary(ddl: str | None) -> set[str]:
    """The `event_type` CHECK enum as a set, parsed out of a CREATE statement.

    Scoped to the CHECK clause rather than the whole statement so an unrelated
    string literal elsewhere in the DDL could never be mistaken for a member of
    the vocabulary. Returns an empty set when there is no CHECK to read, which
    the caller treats as drift — a table without the constraint is exactly as
    wrong as one with the wrong constraint.
    """
    if not ddl:
        return set()
    import re

    match = re.search(
        r"event_type\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*event_type\s+IN\s*\((.*?)\)\s*\)",
        ddl,
        re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        return set()
    return set(re.findall(r"'([^']+)'", match.group(1)))


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
    # BL-NEW-QUOTE-PAIR (schema_version 20260513)
    "quote_symbol",
    "dex_id",
]


class Database:
    """Thin async wrapper around an aiosqlite connection."""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 90_000) -> None:
        # GA-22: canonical source is Settings.SQLITE_BUSY_TIMEOUT_MS (wired
        # by scout/main.py); the keyword default mirrors it so short-lived
        # CLI/script constructors keep the same behavior without plumbing.
        self._db_path = str(db_path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._conn: aiosqlite.Connection | None = None
        self._txn_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, *, retire_dead_tables: bool = False) -> None:
        """Open connection and create tables."""
        self._conn = await aiosqlite.connect(self._db_path)
        # Own the connection from the moment it exists. Every line below can
        # raise -- `_create_tables` and ~40 migrations all execute against a
        # live database that the pipeline is concurrently writing, and a
        # migration that exceeds busy_timeout raises OperationalError.
        #
        # Before this, such a failure left `_conn` open with its aiosqlite
        # worker thread still running. A short-lived caller then had no way to
        # close it: `solana_lane.main()` calls `initialize()` OUTSIDE the
        # try/finally that owns `db.close()`, so the exception skipped straight
        # past it. That is a resource-ownership defect on its own terms: this
        # method opened the connection, so this method must not lose it.
        #
        # 2026-08-08: the Solana watchdog log carried 74 `database is locked`
        # failures raised out of this path, and 35 of its invocations (70
        # processes counting shell wrappers) were found resident, ~1.5 GB RSS on
        # a 3.8 GB box, the oldest 5.45 days old.
        #
        # Those resident processes are CONSISTENT with this leak but not
        # explained by it alone. On the pinned aiosqlite 0.22.1,
        # `Connection.__del__` calls `stop()`, so a leaked connection that
        # becomes unreachable has its worker thread reaped by GC and the process
        # exits -- confirmed by mutation: a subprocess that leaks this way still
        # terminates. Persistence therefore needs some additional mechanism
        # keeping the connection REACHABLE (a retained traceback frame holding
        # main()'s locals is the obvious candidate), and that link is unproven:
        # the specimens were terminated during containment before capture.
        #
        # The original exception is re-raised; a failure to close is logged and
        # swallowed so it can never mask the real cause.
        try:
            self._conn.row_factory = aiosqlite.Row
            self._txn_lock = asyncio.Lock()
            await self._conn.execute("PRAGMA journal_mode=WAL")
            # GA-22: explicit connection-level busy_timeout at bootstrap. Before
            # this, the 90s timeout existed only as an incidental side-effect of
            # four migration-site PRAGMAs — a connection whose migrations were
            # all no-ops ran with timeout 0. The migration-site PRAGMAs are kept
            # (re-asserting is harmless) but now source the same configured value
            # so they can't clobber an operator override.
            await self._conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            # BL-055 spec §3.2: foreign_keys=ON is REQUIRED on every connection.
            # Default is OFF in SQLite; without it, ON DELETE RESTRICT is a no-op.
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._create_tables()
            await self._migrate_feedback_loop_schema()
            await self._migrate_live_trading_schema()
            await self._migrate_signal_params_schema()
            await self._migrate_high_peak_fade_columns_and_audit_table()
            await self._migrate_autosuspend_baseline_column()
            await self._migrate_moonshot_opt_out_column()
            await self._migrate_live_eligible_column()
            await self._migrate_per_venue_services()
            await self._migrate_live_trades_telemetry()
            await self._migrate_live_client_order_id()
            await self._migrate_reject_reason_extend()
            await self._migrate_bl_quote_pair_v1()
            await self._migrate_reject_reason_extend_v2()
            await self._migrate_bl_slow_burn_v1()
            await self._migrate_tg_alert_eligible_v1()
            await self._migrate_tg_alert_log_m1_5c_outcome()
            # BL-NEW-TG-ALERT-NOISE-DEDUP: widen outcome CHECK with
            # 'blocked_dedup_24h'. MUST run AFTER the m1_5c widening so it
            # operates on the already-widened CHECK and preserves all values.
            await self._migrate_tg_alert_log_dedup_outcome()
            await self._migrate_tg_alert_operator_actions_v1()
            await self._migrate_narrative_scanner_v1()
            await self._migrate_minara_alert_emissions_v1()
            # BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION (2026-05-20):
            # ordered AFTER minara_alert_emissions so the paper_trades FK target
            # is guaranteed to exist + already-migrated (Vector A I4).
            await self._migrate_actionability_entry_snapshot_v1()
            await self._migrate_source_calls_v1()
            await self._migrate_chain_pattern_provenance_v1()
            await self._migrate_score_history_scanned_at_index()
            await self._migrate_volume_snapshots_scanned_at_index()
            # BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (cycle 2): 6 narrative-owned
            # tables. Same pattern as cycle 1, parameterized via _migrate_scanned_at_index
            # `column` kwarg (D3 plan-review fold). Order: alphabetical by table.
            # V12 PR-review SHOULD-FIX #1: chain_matches index promoted from
            # deferred (V9 NICE-TO-HAVE) — 5-line cost vs structural table-scan
            # on every hourly prune.
            await self._migrate_chain_matches_completed_at_index()
            await self._migrate_holder_snapshots_scanned_at_index()
            await self._migrate_learn_logs_created_at_index()
            await self._migrate_momentum_7d_detected_at_index()
            await self._migrate_trending_snapshots_snapshot_at_index()
            await self._migrate_volume_spikes_detected_at_index()
            # BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST (cycle 5): cohort_digest_state
            # singleton + paper_trades(closed_at) partial index.
            await self._migrate_cohort_digest_state_v1()
            # BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-INDEX: functional indexes on
            # UPPER(symbol) for volume_history_cg + gainers_snapshots so the
            # x_alerts symbol resolver stops scanning 2.5M-row table.
            await self._migrate_symbol_upper_indexes_v1()
            await self._migrate_gainers_comparisons_appeared_idx_v1()
            await self._migrate_trade_decision_events_v1()
            await self._migrate_predictions_coin_predicted_id_idx_v1()
            # Gap-fill 2026-06-02: gainer_acceleration table + gainers_comparisons
            # surface columns (acceleration/momentum/slow_burn/velocity).
            await self._migrate_gainer_acceleration_v1()
            # BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS Phase 1a-i (2026-05-29):
            # 4 nullable enrichment columns on `candidates` + paper_migrations
            # marker. Read-only writer (cron) populates later in Phase 1a-ii.
            # No DEFAULT clauses — preserves absence-vs-zero semantics so the
            # dashboard read path can distinguish "never written" from "written
            # but resolved to no-match".
            await self._migrate_liquidity_enrichment_v1()
            # DEX-outcome instrumentation substrate (observe-only; I1/I2/I3).
            await self._migrate_dex_instrumentation_v1()
            # Narrative resolution observability: resolution_status column + backfill.
            await self._migrate_narrative_resolution_status_v1()
            # REC-02: durable narrative CA-resolver error counter so the pipeline
            # watchdog can feed a REAL count into narrative_resolution_alarms'
            # resolver_error branch (previously fed a hardcoded 0 — silent §12a).
            await self._migrate_narrative_resolver_errors_v1()
            # DEX-first Phase 1: GT new-pools discovery record (research-only).
            await self._migrate_dex_discovery_v1()
            # C2 (#392): forward-only price snapshots for CA-keyed source calls.
            # Additive table; the snapshot-writer cron populates it. No source_calls
            # columns are added. The (separate) C3 pricing hookup reads it.
            await self._migrate_source_call_price_snapshots_v1()
            # C4 (#392): per-cycle snapshot-writer run stats (§12a watchdog substrate).
            await self._migrate_source_call_price_snapshot_runs_v1()
            # B1-residual: durable POST-COMMIT visibility markers, so an as-of
            # reader cannot see rows that were inserted before its `as_of` but
            # committed after it.
            await self._migrate_source_call_snapshot_batches_v1()
            # GA-19: durable per-source consecutive-miss counters so the
            # ingest-starvation watchdog survives gecko-pipeline restarts.
            await self._migrate_ingest_watchdog_state_v1()
            # 2026-08-21 monthly-budget repair: durable CoinGecko credit
            # accounting. Module counters reset on restart, and a budget you can
            # zero by bouncing the service is not a budget.
            await self._migrate_cg_credit_ledger_v1()
            # Retention option F: derived first-seen substrate, so
            # consumers stop depending on unbounded signal_events history.
            await self._migrate_signal_first_seen_v1()
            # P0 edge-audit 2026-07-02: signal_outcome_ledger — every emission
            # (candidate alert / paper-trade dispatch / sampled gate-block)
            # self-labels with forward returns from in-DB price sources.
            await self._migrate_signal_outcome_ledger_v1()
            # Phase 6 slices 2+3: price_source at open + exit_provenance at close
            # + stale-onset mark-provenance columns on paper_trades.
            await self._migrate_price_provenance_v1()
            # Entry-snapshot liquidity provenance (PR #381): 2 nullable columns on
            # paper_trade_entry_snapshots. Runs AFTER the actionability snapshot
            # migration (its ALTER target table) — additive, idempotent.
            await self._migrate_entry_snapshot_liquidity_provenance_v1()
            # BL-NEW-LEDGER-EVICTION-DB-MARKER (#406 ruling): durable per-token
            # record of cap evictions from ledger_enrollments, so evicted-truncated
            # vs liquidity-death separates via DB state alone (surviving journald
            # rotation). New table only — additive, idempotent.
            await self._migrate_ledger_enrollment_evictions_v1()

            # DASH-05 moved-already/too-late postmortem substrate: one bare-additive
            # table (#424-style). schema_version 20260713 — 20260710 (#448), 20260711
            # (ddl-retire) and 20260712 (#400-renumber) are claimed in-flight, so this
            # takes the next free literal.
            await self._migrate_moved_already_postmortems_v1()

            # ALR-02 decision-receipt audit substrate: one bare-additive table
            # (#424-style). schema_version 20260726. Forward-only observability that
            # records one receipt per evaluated detection-lane candidate so the
            # gate-FAILER comparison cohort is recoverable (behavior-neutral).
            await self._migrate_detection_decision_receipts_v1()

            # Receipt lifecycle-archive substrate (hot cohort/manifest tables) for
            # the approved hot+cold storage architecture. schema_version 20260727.
            await self._migrate_detection_receipt_archive_v1()

            # Solana DEX lane execution state machine. schema_version 20260801.
            # Bare-additive new table: the 13 execution states are a lane-specific
            # LIFECYCLE axis, orthogonal to live_trades.status which means "what
            # happened to the trade" and is read cross-venue. Four of the states
            # exist before any money does, so they cannot live on a money row.
            await self._migrate_solana_executions_v1()

            # Venue-neutral execution binding. schema_version 20260802.
            # Adds live_trades.intent_hash + live_trades.mandate_mode and widens the
            # reject_reason CHECK with the three refusals the mandate + capability gate
            # can produce. MUST run after `_migrate_reject_reason_extend_v2` so it
            # operates on the already-widened CHECK and preserves every value.
            await self._migrate_venue_neutral_execution_v1()

            # Adverse-excursion instrumentation. schema_version 20260803.
            # Bare-additive: paper_trades gains `trough_price` + `mae_pct`, the
            # low-water mark since entry, maintained symmetrically to peak_price /
            # peak_pct on the same evaluator tick.
            #
            # Why this exists: the 2026-08-03 exit-mechanics analysis could not
            # evaluate stop width at all. For the 342 rows that closed at stop_loss
            # the SAVING from a tighter stop is computable, but for the other 1,768
            # closes the COST -- trades a tighter stop would newly convert into
            # losses -- is not, because nothing recorded how far a position dipped
            # before recovering. A backtest on peak_pct alone yields a one-sided
            # estimate that makes tightening look strictly beneficial.
            #
            # Forward-only by construction: the low-water mark of a closed trade is
            # unrecoverable. Existing rows stay NULL and MUST be excluded from any
            # MAE analysis rather than treated as zero.
            await self._migrate_trade_adverse_excursion_v1()

            # Pre-leg-1 adverse excursion. schema_version 20260808.
            # Bare-additive: paper_trades gains `pre_leg1_trough_price` +
            # `pre_leg1_mae_pct` -- the low-water mark restricted to the window in
            # which the INITIAL stop-loss is actually eligible to fire.
            #
            # Why `mae_pct` alone is not enough. `mae_pct` above is a whole-life
            # low-water mark: the evaluator updates it on every valid tick until
            # close, unconditionally. But the initial SL is gated
            # `if not floor_armed and ... current_price <= sl_price` -- once leg 1
            # arms the floor, the SL is structurally out of the picture and the
            # downside protection becomes a breakeven floor at entry instead.
            #
            # So a trade that runs entry -> +10% (leg 1 arms) -> -15% -> closes
            # profitable records mae_pct = -15%, and a naive counterfactual reads
            # that as "a -12% stop would have killed this winner". It would not
            # have: the -12% stop was no longer eligible when the dip happened.
            # Measured on the only cohort that has the column (2026-08), 7 of 47
            # floor-armed closes dipped past -12% and ALL SEVEN were winners, while
            # 0 of the 19 not-yet-armed trades that dipped past -12% won. The bias
            # is not marginal and it runs in the direction that makes tightening
            # look worse than it is.
            #
            # This column freezes at arm time, so it answers the question the
            # whole-life column cannot: how far did this position dip while the
            # initial stop could still have fired?
            #
            # Forward-only, same NULL discipline as mae_pct: NULL means never
            # measured, 0.0 means never dipped below entry while the SL was live.
            await self._migrate_pre_leg1_adverse_excursion_v1()

            # TG actionability shadow (Stage A). schema_version 20260812.
            # Additive: `tg_social_signals` gains `resolution_snapshot_json`
            # plus two new tables (`tg_act_shadow`, `tg_act_shadow_generations`).
            #
            # Ordered after `_migrate_feedback_loop_schema`, which is what
            # actually creates `tg_social_signals` (NOT `_create_tables`).
            # The migration is nonetheless written to tolerate that table
            # being absent — see its docstring.
            #
            # Deploy-dark by construction: the migration installs the tables,
            # `TG_SHADOW_ENABLED=false` means no generation row is ever created,
            # and the eligibility scan only ever sees rows whose created_at is
            # at or after an activation that has not happened yet. Existing
            # rows keep `resolution_snapshot_json = NULL`, which is the correct
            # pre-cutover state, not a gap to backfill.
            await self._migrate_tg_act_shadow_v1()

            # F3 control-plane event ledger. schema_version 20260814.
            # Purely additive: one new append-only table plus its two indexes,
            # no column added to any existing table and no backfill.
            #
            # NOT deploy-inert, and the earlier claim that it was is wrong:
            # `cron/gecko-alpha.crontab` sets ALERT_CHANNEL_WATCHDOG_ENABLED=true
            # INLINE on the hourly :50 line, so the watchdog is live the moment
            # the managed cron block is installed. "Gated on the flag" is true of
            # the script and false of the deployment.
            #
            # What actually prevents a spurious page at the deploy boundary is
            # the `ledger_installed` epoch row this migration seeds: the
            # watchdog ages from it while no `refresh_completed` heartbeat
            # exists yet, so the first nightly refresh has one full SLO to
            # arrive. §12a is preserved rather than waived — if that refresh
            # never runs, the epoch row itself goes stale and pages truthfully.
            await self._migrate_alert_events_v1()

            # Index-only, schema_version 20260816. Must run AFTER the step
            # above: it asserts on an index that step attaches, and on a fresh
            # install the table does not exist until then.
            await self._migrate_alert_events_dedup_index_v1()

            # Content-addressed alert-body preimages, schema_version 20260817.
            # Purely additive: one new table keyed by the digest `alert_events`
            # already records. No column is added to `alert_events` and no
            # backfill is possible — the bodies behind existing digests are
            # gone, which is the gap this closes going forward.
            await self._migrate_alert_payloads_v1()

            # INF-08: signal_events index migration. schema_version 20260821.
            # Option D from retention rulings: drop idx_sig_events_type (466MB),
            # add idx_sig_events_created_at. load_recent_events queries created_at;
            # idx_sig_events_type serves NO WHERE clause anywhere. Measure NET
            # occupied-page reduction. Run BEFORE retire_dead_tables as a schema
            # index change (not destructive data-level pruning).
            await self._migrate_signal_events_indexes_v1()

            # NAR-06 + INF-07 (opt-in-destructive): retire four dead tables. Gated
            # on RETIRE_DEAD_TABLES_ENABLED (plumbed from scout/main.py) because the
            # DROPs are irreversible — the flag IS the recorded-approval hook. Runs
            # LAST so every additive migration above has already settled.
            await self._migrate_retire_dead_tables_v1(enabled=retire_dead_tables)
        except BaseException:
            conn, self._conn = self._conn, None
            try:
                await conn.close()
            except Exception:
                # `_db_log`, not `_log`: this module binds the logger under that
                # name at module scope, and several methods below shadow it with
                # a local `_log`. A wrong name here raises NameError from inside
                # the handler that exists to contain errors, replacing the real
                # OperationalError with a confusing one.
                _db_log.exception(
                    "db_initialize_cleanup_close_failed",
                    db_path=str(self._db_path),
                )
            raise

    async def connect(self) -> None:
        """Alias for :meth:`initialize` — preferred in tests and async context managers."""
        await self.initialize()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _migrate_dex_discovery_v1(self) -> None:
        """Migration dex_discovery_v1, schema_version 20260720.

        DEX-first Phase 1 (design_dex_first_discovery_2026_07_20): additive
        table recording every first-seen GT new-pool. Research-only — never
        read by scorer/gate/alert/paper paths. UNIQUE(network, pool_address)
        makes discovery idempotent across polls.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dex_pool_discoveries (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    network            TEXT NOT NULL,
                    pool_address       TEXT NOT NULL,
                    base_token_address TEXT NOT NULL,
                    base_token_symbol  TEXT,
                    quote_token_symbol TEXT,
                    pool_created_at    TEXT,
                    first_seen_at      TEXT NOT NULL,
                    fdv_usd            REAL,
                    liquidity_usd      REAL,
                    volume_h1_usd      REAL,
                    goplus_safe        INTEGER,
                    UNIQUE(network, pool_address)
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dex_pool_disc_token "
                "ON dex_pool_discoveries(base_token_address, first_seen_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260720, now_iso, "dex_discovery_v1"),
            )
            await conn.commit()
        except Exception:
            _log.exception("schema_migration_failed", migration="dex_discovery_v1")
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed", migration="dex_discovery_v1"
                )
            _log.error("SCHEMA_DRIFT_DETECTED", migration="dex_discovery_v1")
            raise

    async def record_pool_discovery(
        self,
        network: str,
        pool_address: str,
        base_token_address: str,
        base_token_symbol: str | None,
        quote_token_symbol: str | None,
        pool_created_at: str | None,
        fdv_usd: float | None,
        liquidity_usd: float | None,
        volume_h1_usd: float | None,
    ) -> bool:
        """Insert a first-seen pool; returns True when the row is new.

        Idempotent via UNIQUE(network, pool_address) + INSERT OR IGNORE, so
        re-polling the same GT page never duplicates a discovery.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cur = await self._conn.execute(
            """INSERT OR IGNORE INTO dex_pool_discoveries
               (network, pool_address, base_token_address, base_token_symbol,
                quote_token_symbol, pool_created_at, first_seen_at,
                fdv_usd, liquidity_usd, volume_h1_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                network,
                pool_address,
                base_token_address,
                base_token_symbol,
                quote_token_symbol,
                pool_created_at,
                datetime.now(timezone.utc).isoformat(),
                fdv_usd,
                liquidity_usd,
                volume_h1_usd,
            ),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def _migrate_dex_instrumentation_v1(self) -> None:
        """Migration dex_instrumentation_v1, schema_version 20260629.

        Observe-only substrate for measuring DEX-stage outcomes (I1/I2/I3). None
        of these tables feed the scorer or gate; they capture linkage, entry mcap,
        and a raw buy/sell proxy so the under-gate cohort can be re-measured.
        See tasks/spec_dex_outcome_instrumentation_i1_i2_i3_2026_06_28.md.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
            """)
            # I1 — durable contract<->coin_id linkage (retroactive; B1).
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS contract_coin_map (
                    contract_address TEXT NOT NULL,
                    chain            TEXT NOT NULL,
                    coin_id          TEXT,
                    resolved_at      TEXT NOT NULL,
                    source           TEXT NOT NULL,
                    confidence       TEXT,
                    address_type     TEXT,
                    PRIMARY KEY (contract_address, chain)
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contract_coin_map_coin "
                "ON contract_coin_map(coin_id)"
            )
            # I2 — non-pruned earliest DEX-side entry mcap (write-once).
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS entry_mcap_snapshots (
                    contract_address        TEXT PRIMARY KEY,
                    chain                   TEXT NOT NULL,
                    first_seen_at           TEXT NOT NULL,
                    mcap_usd_at_entry       REAL,
                    liquidity_usd_at_entry  REAL,
                    token_age_days_at_entry REAL,
                    captured_at             TEXT
                )
            """)
            # I3 — raw buy/sell proxy snapshots (captured-not-scored; B4).
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS txns_h1_buys_snapshots (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_address TEXT NOT NULL,
                    txns_h1_buys     INTEGER,
                    txns_h1_sells    INTEGER,
                    source           TEXT NOT NULL,
                    scanned_at       TEXT NOT NULL
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_txns_h1_buys_contract_scanned "
                "ON txns_h1_buys_snapshots(contract_address, scanned_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260629, now_iso, "dex_instrumentation_v1"),
            )
            await conn.commit()
        except Exception:
            _log.exception(
                "schema_migration_failed", migration="dex_instrumentation_v1"
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="dex_instrumentation_v1",
                )
            _log.error("SCHEMA_DRIFT_DETECTED", migration="dex_instrumentation_v1")
            raise

        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?", (20260629,)
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "dex_instrumentation_v1 schema_version row missing after migration"
            )

    async def record_entry_mcap(
        self,
        contract_address: str,
        chain: str,
        first_seen_at: str,
        mcap_usd: float | None,
        liquidity_usd: float | None,
        token_age_days: float | None,
    ) -> None:
        """I2: persist the earliest DEX-side entry mcap (observe-only).

        Write-once: a row is *finalized* (``captured_at`` set) only when a
        positive mcap is observed — DEX-mcap is preferred over CG-side zero
        placeholders, so a zero/placeholder first sighting only holds the slot
        open until a non-zero mcap arrives. CG-native slugs are skipped (they
        have no DEX-stage entry). Never pruned. Does not feed the scorer/gate.
        """
        from scout.instrumentation.classify import is_dex

        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not is_dex(contract_address):
            return
        conn = self._conn
        cur = await conn.execute(
            "SELECT captured_at FROM entry_mcap_snapshots WHERE contract_address = ?",
            (contract_address,),
        )
        existing = await cur.fetchone()
        if existing is not None and existing[0] is not None:
            return  # already finalized -> write-once

        earliest_merge = (
            "first_seen_at = CASE "
            "WHEN datetime(excluded.first_seen_at) "
            "< datetime(entry_mcap_snapshots.first_seen_at) "
            "THEN excluded.first_seen_at "
            "ELSE entry_mcap_snapshots.first_seen_at END"
        )
        if mcap_usd and mcap_usd > 0:
            now = datetime.now(timezone.utc).isoformat()
            await conn.execute(
                "INSERT INTO entry_mcap_snapshots "
                "(contract_address, chain, first_seen_at, mcap_usd_at_entry, "
                "liquidity_usd_at_entry, token_age_days_at_entry, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(contract_address) DO UPDATE SET "
                f"{earliest_merge}, "
                "mcap_usd_at_entry = excluded.mcap_usd_at_entry, "
                "liquidity_usd_at_entry = excluded.liquidity_usd_at_entry, "
                "token_age_days_at_entry = excluded.token_age_days_at_entry, "
                "captured_at = excluded.captured_at",
                (
                    contract_address,
                    chain,
                    first_seen_at,
                    mcap_usd,
                    liquidity_usd,
                    token_age_days,
                    now,
                ),
            )
        else:
            await conn.execute(
                "INSERT INTO entry_mcap_snapshots "
                "(contract_address, chain, first_seen_at, mcap_usd_at_entry, "
                "liquidity_usd_at_entry, token_age_days_at_entry, captured_at) "
                "VALUES (?, ?, ?, NULL, NULL, NULL, NULL) "
                "ON CONFLICT(contract_address) DO UPDATE SET "
                f"{earliest_merge}",
                (contract_address, chain, first_seen_at),
            )
        await conn.commit()

    async def log_txns_snapshot(
        self,
        contract_address: str,
        txns_h1_buys: int | None,
        txns_h1_sells: int | None,
        source: str,
    ) -> None:
        """I3: append a raw buy/sell-count snapshot (observe-only).

        Stores absolute values + source + timestamp; deltas are computed in
        analysis, never here. If neither count is available (no source provided
        the data), no row is written — so the gap is visible to the non-null
        watchdog instead of being masked by a zero. Captured-not-scored.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if txns_h1_buys is None and txns_h1_sells is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO txns_h1_buys_snapshots "
            "(contract_address, txns_h1_buys, txns_h1_sells, source, scanned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (contract_address, txns_h1_buys, txns_h1_sells, source, now),
        )
        await self._conn.commit()

    async def prune_txns_snapshots(self, *, keep_days: int) -> int:
        """Prune raw proxy snapshots older than keep_days. Returns rows deleted."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM txns_h1_buys_snapshots WHERE scanned_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def compute_dex_coverage_metrics(self) -> dict:
        """C5: substrate-health + analysis-readiness coverage metrics (B1/B2).

        Computed over CG-listed DEX tokens only (never-listing fizzles are
        invisible — see the survivorship caveat in the spec):

          listed_dex                 = DEX contracts (classifier) with a resolved coin_id
          covered                    = listed_dex with an entry-mcap row AND >=1
                                       coin_id-keyed outcome-surface match
          dex_resolution_health      = covered / listed_dex   (0.0 if none listed)
          dex_measurable_cohort_size = covered

        Observe-only; reads existing tables, writes nothing.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        # Filter on the PERSISTED address_type column (B2) — not query-time
        # inference and not the pruned candidates.chain.
        cur = await self._conn.execute(
            "SELECT "
            "(SELECT 1 FROM entry_mcap_snapshots e "
            " WHERE e.contract_address = m.contract_address LIMIT 1) AS has_entry, "
            "(CASE WHEN m.coin_id IN ("
            "   SELECT coin_id FROM gainers_snapshots "
            "   UNION SELECT coin_id FROM momentum_7d "
            "   UNION SELECT coin_id FROM conviction_watchlist_snapshots"
            " ) THEN 1 ELSE 0 END) AS has_outcome "
            "FROM contract_coin_map m "
            "WHERE m.coin_id IS NOT NULL AND m.address_type IN ('evm', 'solana')"
        )
        rows = await cur.fetchall()
        listed = len(rows)
        covered = sum(1 for r in rows if r["has_entry"] and r["has_outcome"])
        health = (covered / listed) if listed else 0.0
        return {
            "listed_dex": listed,
            "covered": covered,
            "dex_resolution_health": health,
            "dex_measurable_cohort_size": covered,
        }

    async def dex_quality_stats(self) -> dict:
        """C6: data-quality rates for the instrumentation tables (observe-only).

        Rates are ``None`` when their table is empty (no data yet -> no alarm).
        A non-None rate that is near zero while the table has rows is the
        fresh-but-empty silent-failure signature the watchdog escalates.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        conn = self._conn
        cur = await conn.execute(
            "SELECT count(*), "
            "SUM(CASE WHEN mcap_usd_at_entry > 0 THEN 1 ELSE 0 END) "
            "FROM entry_mcap_snapshots"
        )
        e_total, e_finalized = await cur.fetchone()
        e_total = e_total or 0
        cur = await conn.execute(
            "SELECT count(*), "
            "SUM(CASE WHEN txns_h1_buys IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM txns_h1_buys_snapshots"
        )
        t_total, t_nonnull = await cur.fetchone()
        t_total = t_total or 0
        cur = await conn.execute(
            "SELECT count(*), "
            "SUM(CASE WHEN coin_id IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM contract_coin_map"
        )
        m_total, m_resolved = await cur.fetchone()
        m_total = m_total or 0
        return {
            "entry_total": e_total,
            "entry_nonzero_rate": ((e_finalized or 0) / e_total) if e_total else None,
            "txns_total": t_total,
            "txns_nonnull_rate": ((t_nonnull or 0) / t_total) if t_total else None,
            "map_total": m_total,
            "map_resolved": m_resolved or 0,
        }

    async def record_contract_coin_map(
        self,
        contract_address: str,
        chain: str,
        coin_id: str | None,
        source: str,
        confidence: str | None,
    ) -> None:
        """I1: upsert a contract<->coin_id mapping (observe-only).

        ``coin_id`` may be NULL to mark a resolution as *attempted* (negative
        result), which the resolver's TTL uses to avoid re-hammering the API.
        A later positive resolution upserts over it. Never feeds scorer/gate.
        """
        from scout.instrumentation.classify import classify_contract

        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        # Persist the DEX-vs-CG classification at write time (B2) so metrics
        # filter on a durable column, not query-time inference or the pruned
        # candidates.chain.
        address_type = classify_contract(contract_address)
        await self._conn.execute(
            "INSERT INTO contract_coin_map "
            "(contract_address, chain, coin_id, resolved_at, source, confidence, "
            "address_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(contract_address, chain) DO UPDATE SET "
            "coin_id = excluded.coin_id, resolved_at = excluded.resolved_at, "
            "source = excluded.source, confidence = excluded.confidence, "
            "address_type = excluded.address_type",
            (contract_address, chain, coin_id, now, source, confidence, address_type),
        )
        await self._conn.commit()

    async def contract_coin_map_has(self, contract_address: str) -> bool:
        """True if any resolution row (incl. attempted/NULL) exists — TTL guard."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cur = await self._conn.execute(
            "SELECT 1 FROM contract_coin_map WHERE contract_address = ? LIMIT 1",
            (contract_address,),
        )
        return (await cur.fetchone()) is not None

    async def coin_id_resolved(self, coin_id: str) -> bool:
        """True if a coin_id already has >=1 resolved (non-NULL) mapping.

        Lets the resolver skip re-resolving the same coin_id, honoring the
        per-cycle call budget across cycles.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cur = await self._conn.execute(
            "SELECT 1 FROM contract_coin_map WHERE coin_id = ? LIMIT 1",
            (coin_id,),
        )
        return (await cur.fetchone()) is not None

    async def record_resolver_attempt(self, coin_id: str) -> None:
        """Record a failed/unknown resolution attempt (negative-result TTL marker).

        Lets the resolver skip a coin_id that just failed (404/429/parse) for a
        TTL window instead of re-spending budget on it every cycle, while still
        retrying after the TTL (handles transient failures). Stored as a sentinel
        row (coin_id NULL, address_type 'attempt') so it is excluded from metrics.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO contract_coin_map "
            "(contract_address, chain, coin_id, resolved_at, source, confidence, "
            "address_type) "
            "VALUES (?, '__attempt__', NULL, ?, 'attempted', NULL, 'attempt') "
            "ON CONFLICT(contract_address, chain) DO UPDATE SET "
            "resolved_at = excluded.resolved_at",
            (f"__attempt__:{coin_id}", now),
        )
        await self._conn.commit()

    async def coin_id_attempt_fresh(self, coin_id: str, ttl_seconds: int) -> bool:
        """True if coin_id had a failed attempt within ttl_seconds (TTL guard)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        ).isoformat()
        cur = await self._conn.execute(
            "SELECT 1 FROM contract_coin_map "
            "WHERE contract_address = ? AND chain = '__attempt__' "
            "AND resolved_at > ? LIMIT 1",
            (f"__attempt__:{coin_id}", cutoff),
        )
        return (await cur.fetchone()) is not None

    async def _migrate_narrative_resolution_status_v1(self) -> None:
        """Add narrative_alerts_inbound.resolution_status + backfill (observability).

        Makes 'fresh inbound but zero resolved' legible: classifies every row as
        cashtag_only / ca_resolved / ca_unresolved, and retro-resolves CA rows
        that the contract_coin_map (I1) can now map to a coin_id. Additive +
        idempotent. Does NOT change scorer/gate/trading behavior.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        # narrative_alerts_inbound only exists after the narrative-scanner migration;
        # if absent, nothing to do (idempotent no-op).
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='narrative_alerts_inbound'"
        )
        if await cur.fetchone() is None:
            return

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            cur = await conn.execute("PRAGMA table_info(narrative_alerts_inbound)")
            cols = {row[1] for row in await cur.fetchall()}
            if "resolution_status" not in cols:
                await conn.execute(
                    "ALTER TABLE narrative_alerts_inbound ADD COLUMN resolution_status TEXT"
                )
            # Backfill classification for any unclassified rows.
            await conn.execute(
                "UPDATE narrative_alerts_inbound SET resolution_status = CASE "
                "WHEN extracted_ca IS NULL OR trim(extracted_ca) = '' THEN 'cashtag_only' "
                "WHEN resolved_coin_id IS NOT NULL AND trim(resolved_coin_id) <> '' "
                "THEN 'ca_resolved' ELSE 'ca_unresolved' END "
                "WHERE resolution_status IS NULL"
            )
            # Retro-resolve CA rows the contract_coin_map can now map (idempotent:
            # only touches ca_unresolved rows). Guarded if contract_coin_map absent.
            has_map = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='contract_coin_map'"
            )
            if await has_map.fetchone() is not None:
                await conn.execute(
                    "UPDATE narrative_alerts_inbound SET "
                    "resolved_coin_id = (SELECT m.coin_id FROM contract_coin_map m "
                    "  WHERE LOWER(m.contract_address) = LOWER(narrative_alerts_inbound.extracted_ca) "
                    "  AND m.coin_id IS NOT NULL LIMIT 1), "
                    "resolution_status = 'ca_resolved' "
                    "WHERE resolution_status = 'ca_unresolved' "
                    "AND extracted_ca IS NOT NULL AND EXISTS ("
                    "  SELECT 1 FROM contract_coin_map m "
                    "  WHERE LOWER(m.contract_address) = LOWER(narrative_alerts_inbound.extracted_ca) "
                    "  AND m.coin_id IS NOT NULL)"
                )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (20260630, now_iso, "narrative_resolution_status_v1"),
            )
            await conn.commit()
        except Exception:
            _log.exception(
                "schema_migration_failed", migration="narrative_resolution_status_v1"
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="narrative_resolution_status_v1",
                )
            raise

    async def narrative_resolution_stats(self) -> dict:
        """Composition-aware narrative resolution metrics (observe-only).

        Splits the corpus so 'zero resolved' is explainable: cashtag-only
        (expected-unresolvable) vs ca_resolved vs ca_unresolved. ``resolver_error``
        is tracked separately at the lookup endpoint, not here.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cur = await self._conn.execute(
            "SELECT count(*), "
            "SUM(resolution_status = 'cashtag_only'), "
            "SUM(resolution_status = 'ca_resolved'), "
            "SUM(resolution_status = 'ca_unresolved'), "
            "SUM(resolution_status IS NULL) "
            "FROM narrative_alerts_inbound"
        )
        total, cashtag, resolved, unresolved, unclassified = await cur.fetchone()
        total = total or 0
        cashtag = cashtag or 0
        resolved = resolved or 0
        unresolved = unresolved or 0
        unclassified = unclassified or 0
        ca_bearing = resolved + unresolved
        return {
            "total": total,
            "cashtag_only": cashtag,
            "ca_bearing": ca_bearing,
            "ca_resolved": resolved,
            "ca_unresolved": unresolved,
            "unclassified": unclassified,
            "cashtag_only_rate": (cashtag / total) if total else None,
            "ca_resolve_rate": (resolved / ca_bearing) if ca_bearing else None,
        }

    async def _migrate_narrative_resolver_errors_v1(self) -> None:
        """REC-02: durable narrative CA-resolver error counter (§12a).

        The ``/api/coin/lookup`` endpoint (dashboard process) records one row
        here per ``resolver_error``; the pipeline's narrative watchdog counts
        recent rows to feed ``narrative_resolution_alarms``' ``resolver_error``
        branch, which the ``main.py`` call site previously starved with a
        hardcoded 0. Name-keyed via ``paper_migrations`` (no numeric
        ``schema_version``, so immune to the INF-05 dup class). Additive +
        idempotent; observe-only. The table grows only on genuine DB-side
        resolver failures (rare), so it needs no dedicated prune.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "narrative_resolver_errors_v1"
        try:
            await conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration_name,)
            )
            if await cur.fetchone():
                await conn.execute("COMMIT")
                return
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS narrative_resolver_errors ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_narrative_resolver_errors_occurred_at "
                "ON narrative_resolver_errors(occurred_at)"
            )
            await conn.execute(
                "INSERT INTO paper_migrations(name, cutover_ts) VALUES (?, ?)",
                (migration_name, datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute("COMMIT")
            _db_log.info(
                "table_migrated",
                table="narrative_resolver_errors",
                migration=migration_name,
            )
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception(
                    "narrative_resolver_errors_migration_rollback_failed",
                    err=str(rb_err),
                )
            raise

    async def count_narrative_resolver_errors(self, since_iso: str) -> int:
        """Count narrative CA-resolver errors recorded since ``since_iso`` (REC-02).

        Feeds the ``resolver_error`` branch of ``narrative_resolution_alarms``
        with a REAL, windowed count (was hardcoded 0 at the call site). Returns 0
        if the table is absent (older schema) so the watchdog degrades to silence
        rather than crashing the hourly-maintenance loop.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        try:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM narrative_resolver_errors "
                "WHERE occurred_at > ?",
                (since_iso,),
            )
            row = await cur.fetchone()
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row and row[0] is not None else 0

    async def _migrate_predictions_coin_predicted_id_idx_v1(self) -> None:
        """Index latest-prediction lookups used by Trade Inbox context."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "predictions_coin_predicted_id_idx_v1"

        try:
            await conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration_name,)
            )
            if await cur.fetchone():
                await conn.execute("COMMIT")
                return
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_predictions_coin_predicted_id "
                "ON predictions(coin_id, predicted_at DESC, id DESC)"
            )
            await conn.execute(
                "INSERT INTO paper_migrations(name, cutover_ts) VALUES (?, ?)",
                (migration_name, datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute("COMMIT")
            _db_log.info(
                "index_migrated",
                table="predictions",
                column="coin_id,predicted_at,id",
                migration=migration_name,
            )
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception("index_migration_rollback_failed", err=str(rb_err))
            _db_log.exception("index_migration_failed", migration=migration_name)
            raise

    async def _migrate_trade_decision_events_v1(self) -> None:
        """Append-only trading admission/skip decision log.

        Kept separate from signal_events because tracker comparison code treats
        signal_events as chain-detection evidence.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "trade_decision_events_v1"
        schema_version = 20260526

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                await self._assert_trade_decision_events_schema(conn)
                _db_log.info("trade_decision_events_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version = ?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if existing_version is not None and existing_version[0] != migration_name:
                raise RuntimeError(
                    "trade_decision_events_v1 schema_version description mismatch - "
                    f"version {schema_version} already owned by "
                    f"{existing_version[0]!r}"
                )
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_decision_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_module TEXT NOT NULL,
                    signal_combo TEXT,
                    paper_trade_id INTEGER,
                    event_data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (paper_trade_id)
                        REFERENCES paper_trades(id) ON DELETE SET NULL
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tde_token_created "
                "ON trade_decision_events(token_id, created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tde_signal_created "
                "ON trade_decision_events(signal_type, created_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tde_decision_reason_created "
                "ON trade_decision_events(decision, reason, created_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            await self._assert_trade_decision_events_schema(conn)
            _db_log.info(
                "trade_decision_events_v1_migration_complete",
                table="trade_decision_events",
            )
        except BaseException as e:
            _db_log.exception(
                "schema_migration_failed",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _db_log.error("SCHEMA_DRIFT_DETECTED", migration=migration_name)
            raise

    async def _assert_trade_decision_events_schema(self, conn) -> None:
        required_columns = {
            "id",
            "token_id",
            "signal_type",
            "decision",
            "reason",
            "source_module",
            "signal_combo",
            "paper_trade_id",
            "event_data",
            "created_at",
        }
        cur = await conn.execute("PRAGMA table_info(trade_decision_events)")
        columns = {row[1] for row in await cur.fetchall()}
        missing = sorted(required_columns - columns)
        if missing:
            raise RuntimeError(
                f"trade_decision_events_v1 missing columns: {', '.join(missing)}"
            )

        cur = await conn.execute("PRAGMA index_list(trade_decision_events)")
        indexes = {row[1] for row in await cur.fetchall()}
        required_indexes = {
            "idx_tde_token_created",
            "idx_tde_signal_created",
            "idx_tde_decision_reason_created",
        }
        missing_indexes = sorted(required_indexes - indexes)
        if missing_indexes:
            raise RuntimeError(
                "trade_decision_events_v1 missing indexes: "
                + ", ".join(missing_indexes)
            )

        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?",
            (20260526,),
        )
        row = await cur.fetchone()
        if row is None or row[0] != "trade_decision_events_v1":
            found = None if row is None else row[0]
            raise RuntimeError(
                "trade_decision_events_v1 schema_version description mismatch - "
                f"found {found!r}"
            )

    async def _migrate_moved_already_postmortems_v1(self) -> None:
        """DASH-05 forward-recording postmortem substrate, schema_version 20260713.

        One bare-additive table (#424-style). Records — the FIRST time a token
        crosses into the dashboard's moved-already/"late" state — the T-minus
        evidence still AVAILABLE at that moment (gainers_snapshots is 7-day
        retention, so this is forward-only; there is no backfill path for past
        monsters). Observe-only: nothing here feeds the scorer/gate/trader.

        ``token_id`` is UNIQUE so the recorder's ``INSERT OR IGNORE`` dedups per
        token. See scout/postmortem/moved_already.py and DASH-05 in
        tasks/backlog_fable_analysis_2026_07_10.md.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS moved_already_postmortems (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id      TEXT NOT NULL UNIQUE,
                    detected_at   TEXT NOT NULL,
                    run_pct       REAL,
                    evidence      TEXT NOT NULL,
                    dropping_gate TEXT
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_moved_already_detected "
                "ON moved_already_postmortems(detected_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260713, now_iso, "moved_already_postmortems_v1"),
            )
            await conn.commit()
        except Exception:
            _log.exception(
                "schema_migration_failed", migration="moved_already_postmortems_v1"
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="moved_already_postmortems_v1",
                )
            _log.error(
                "SCHEMA_DRIFT_DETECTED", migration="moved_already_postmortems_v1"
            )
            raise

        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?", (20260713,)
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "moved_already_postmortems_v1 schema_version row missing after migration"
            )

    async def get_recorded_moved_already_token_ids(self) -> set[str]:
        """Return the token_ids that already have a moved-already postmortem row."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cur = await self._conn.execute("SELECT token_id FROM moved_already_postmortems")
        return {r[0] for r in await cur.fetchall()}

    async def insert_moved_already_postmortem(
        self,
        *,
        token_id: str,
        detected_at: str,
        run_pct: float | None,
        evidence: dict,
        dropping_gate: str | None,
    ) -> bool:
        """Insert one postmortem row; dedup per token via UNIQUE(token_id).

        Returns True when a new row was written, False when the token already
        had a postmortem (the ``INSERT OR IGNORE`` no-op'd).
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO moved_already_postmortems "
            "(token_id, detected_at, run_pct, evidence, dropping_gate) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_id, detected_at, run_pct, json.dumps(evidence), dropping_gate),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def _migrate_detection_decision_receipts_v1(self) -> None:
        """ALR-02 decision-receipt audit substrate, schema_version 20260726.

        One bare-additive table (#424-style). Forward-only observability: the
        ALR-02 detection lane (scout/trading/detection_alert.py) records a
        decision-time receipt for EVERY candidate it evaluates — gate passes,
        gate failures, and every other terminal decision (too_old /
        universe_filter / not_early / dedup_24h / rate_limit / dispatch_failed
        / sent). Purpose: recover the gate-FAILER comparison cohort the lane
        previously dropped SILENTLY (unlogged ``continue``), so gate-enrichment
        can be estimated. Observe-only: nothing here feeds the
        scorer/gate/trader/sender — the lane's send/gate/product behavior is
        unchanged (behavior-neutral).

        ``idempotency_key`` is UNIQUE so the writer's ``INSERT OR IGNORE``
        collapses re-evaluations of the same
        (token_id, outcome, source_observation_ts, gate_version) state to a
        single row — repeated polling never inflates the analytical unit
        (LOCK 3). An ignored insert whose stored payload differs from the attempt
        is a CONFLICTING DUPLICATE, surfaced (never hidden) by
        insert_detection_decision_receipt + the caller's
        ``detection_receipt_conflict`` warning (reviewer correction 3). The
        pre-registered primary INDEX decision is the token's FIRST RECORDED
        EVALUATION after cohort start, any outcome (MIN(decided_at) per token_id
        at analysis time); the arm is the outcome AT that index row. See
        tasks/prereg_detection_gate_enrichment_cohort.md for the full
        pre-registration.

        Each receipt persists the RAW decision inputs (reviewer correction 2),
        not merely fired signals: ``score_before``/``score_after`` (the derived
        score before/after ``int(x or 0)`` clipping), the ``comparator`` and
        ``threshold_value`` actually applied, ``gate_version`` + ``code_version``
        (deployed code identity) to resolve the precise decision logic, and a
        ``raw_inputs`` JSON blob carrying the raw component values +
        missingness/default indicators. ``signals_fired`` is retained as a
        convenience only — it cannot substitute for the underlying values.

        §12a freshness expectation: the writer runs ONLY while
        DETECTION_ALERT_LANE_ENABLED is true. Expected write rate then ~ the
        CG-fresh candidate pool evaluated per cycle (tens–low-hundreds of
        receipts/day). Coverage is reconciled PER-CYCLE in-log via the
        ``detection_receipt_summary`` event
        (evaluated_n == receipts_written_n + write_failures_n); a standalone
        cron watchdog is intentionally NOT added for this table — the lane
        itself is already watchdogged and the per-cycle summary makes coverage
        gaps detectable without one. Pruned hourly
        (DETECTION_DECISION_RECEIPTS_RETENTION_DAYS, floor 45d).
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS detection_decision_receipts (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id              TEXT NOT NULL,
                    decided_at            TEXT NOT NULL,
                    outcome               TEXT NOT NULL,
                    reason                TEXT,
                    source_observation_ts TEXT,
                    gate_version          TEXT NOT NULL,
                    code_version          TEXT,
                    score_before          INTEGER,
                    score_after           INTEGER NOT NULL,
                    comparator            TEXT NOT NULL,
                    threshold_value       INTEGER NOT NULL,
                    signals_fired         TEXT,
                    raw_inputs            TEXT,
                    idempotency_key       TEXT NOT NULL
                )
            """)
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ddr_idempotency "
                "ON detection_decision_receipts(idempotency_key)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ddr_token_decided "
                "ON detection_decision_receipts(token_id, decided_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260726, now_iso, "detection_decision_receipts_v1"),
            )
            await conn.commit()
        except Exception:
            _log.exception(
                "schema_migration_failed",
                migration="detection_decision_receipts_v1",
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="detection_decision_receipts_v1",
                )
            _log.error(
                "SCHEMA_DRIFT_DETECTED", migration="detection_decision_receipts_v1"
            )
            raise

        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?", (20260726,)
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "detection_decision_receipts_v1 schema_version row missing "
                "after migration"
            )

    # Decision-content columns that define a receipt's PAYLOAD (everything
    # except id + decided_at). Two receipts with the same idempotency_key but a
    # different payload are a CONFLICTING DUPLICATE — a correctness defect the
    # writer must surface, never silently ignore (reviewer correction 3).
    #
    # Two-identity model (reviewer, 2026-07-26): the idempotency key now
    # incorporates the EVALUATION INSTANCE (the cycle's ``decided_at`` — see
    # detection_alert._receipt_idempotency_key), so cycle-N and cycle-N+1
    # evaluations of the same token are DISTINCT rows (evaluation identity). A
    # same-key hit therefore means the SAME evaluation instance was written
    # twice (an intra-cycle crash/retry or a duplicated input) — NOT a routine
    # cross-cycle re-poll. Within one evaluation instance the payload is
    # deterministic, so a full-payload comparison is exactly right: identical →
    # exact_idempotent_replay; any difference → conflicting_duplicate (a true
    # defect). Time-varying observation fields no longer cause false conflicts
    # because cross-cycle re-polls get DISTINCT keys and never reach this
    # comparison. Ordered to match the SELECT in insert_detection_decision_receipt.
    _RECEIPT_PAYLOAD_COLUMNS = (
        "token_id",
        "outcome",
        "reason",
        "source_observation_ts",
        "gate_version",
        "code_version",
        "score_before",
        "score_after",
        "comparator",
        "threshold_value",
        "signals_fired",
        "raw_inputs",
    )

    async def insert_detection_decision_receipt(
        self,
        *,
        token_id: str,
        decided_at: str,
        outcome: str,
        reason: str | None,
        source_observation_ts: str | None,
        gate_version: str,
        code_version: str | None,
        score_before: int | None,
        score_after: int,
        comparator: str,
        threshold_value: int,
        signals_fired: str | None,
        raw_inputs: str | None,
        idempotency_key: str,
    ) -> str:
        """Insert one ALR-02 decision receipt; dedup per ``idempotency_key`` via
        ``INSERT OR IGNORE`` + the UNIQUE index (LOCK 3), classifying the result
        so an ignored insert can never hide a conflict (reviewer correction 3).

        Because the ``idempotency_key`` incorporates the evaluation instance
        (the cycle ``decided_at``), a same-key hit is a repeat of the SAME
        evaluation, not a cross-cycle re-poll. Returns one of:
          - ``"inserted"``          — a NEW row was written (the normal path;
            every fresh evaluation instance is a new row).
          - ``"idempotent_replay"`` — the same evaluation instance already
            existed AND its payload matches this attempt exactly (an intra-cycle
            crash/retry or duplicated input). ``decided_at`` is in the key, so it
            already matches; the FIRST write's row is preserved.
          - ``"conflict"``          — the same evaluation instance already
            existed but its payload DIFFERS from this attempt (the same
            evaluation produced two different payloads — a genuine defect). The
            caller emits a ``detection_receipt_conflict`` warning and counts it.

        RAISES on a real DB error — the caller
        (detection_alert.notify_early_detections) wraps this fail-soft so a
        receipt-write failure can never change sending behavior (LOCK 4).
        ``signals_fired`` and ``raw_inputs`` are JSON-encoded strings (or None).
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        # Values in the exact order of _RECEIPT_PAYLOAD_COLUMNS.
        payload = (
            token_id,
            outcome,
            reason,
            source_observation_ts,
            gate_version,
            code_version,
            score_before,
            score_after,
            comparator,
            threshold_value,
            signals_fired,
            raw_inputs,
        )
        async with self._txn_lock:
            cur = await self._conn.execute(
                "INSERT OR IGNORE INTO detection_decision_receipts "
                "(token_id, decided_at, outcome, reason, source_observation_ts, "
                " gate_version, code_version, score_before, score_after, "
                " comparator, threshold_value, signals_fired, raw_inputs, "
                " idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token_id,
                    decided_at,
                    outcome,
                    reason,
                    source_observation_ts,
                    gate_version,
                    code_version,
                    score_before,
                    score_after,
                    comparator,
                    threshold_value,
                    signals_fired,
                    raw_inputs,
                    idempotency_key,
                ),
            )
            if cur.rowcount and cur.rowcount > 0:
                await self._conn.commit()
                return "inserted"
            # Ignored: the key already exists. Classify replay vs conflict by
            # comparing the persisted payload to this attempt.
            select_cols = ", ".join(self._RECEIPT_PAYLOAD_COLUMNS)
            cur2 = await self._conn.execute(
                f"SELECT {select_cols} FROM detection_decision_receipts "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            existing = await cur2.fetchone()
            await self._conn.commit()
        if existing is None:
            # Concurrent delete between the ignored insert and the read; treat as
            # a benign replay (nothing to conflict with).
            return "idempotent_replay"
        return "idempotent_replay" if tuple(existing) == payload else "conflict"

    async def prune_detection_decision_receipts(
        self, *, keep_days: int, cohort_closed_at: str | None
    ) -> int:
        """Delete ``detection_decision_receipts`` rows older than ``keep_days``,
        GUARDED by cohort completeness (reviewer correction e).

        A row is pruned ONLY when ALL hold:
          - a cohort-close marker (``cohort_closed_at``) is set — an empty/None
            marker means no cohort has been closed, so NOTHING is pruned (an
            in-flight cohort's receipts must survive manifest freeze + final
            analysis; pruning mid-lifecycle INVALIDATES the cohort); AND
          - the row is older than the retention floor (``keep_days``); AND
          - the row predates the cohort-close marker (rows newer than the marker
            belong to a still-open cohort and are never pruned).

        ``decided_at`` and ``cohort_closed_at`` are ISO-8601 UTC, so the string
        comparisons order-correctly. Both cutoffs are applied as independent SQL
        predicates (not a lexical ``min``) so mixed precisions still compare
        safely. Returns rowcount.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        if not cohort_closed_at:
            # No cohort closed → the guard blocks all pruning.
            return 0
        retention_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_days)
        ).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM detection_decision_receipts "
            "WHERE decided_at <= ? AND decided_at <= ?",
            (retention_cutoff, cohort_closed_at),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def _migrate_detection_receipt_archive_v1(self) -> None:
        """Receipt lifecycle-archive substrate, schema_version 20260727.

        Two bare-additive HOT-tier tables (#424-style) supporting the approved
        hot/cold architecture (tasks/capacity_detection_receipts_2026_07.md):

        - ``detection_receipt_cohorts`` — cohort definitions + status. Carries the
          immutable cohort start (application-ready, post-migration), the
          shakedown exclusion range, the reconciled-through watermark (how far
          per-cycle reconciliation has verified), and the close/analysis status.
          The analytical-index selection embeds a cohort id + shakedown exclusion
          via this table.
        - ``detection_receipt_archive_manifest`` — one row per PUBLISHED immutable
          cold partition. Each carries schema+serialization version, record count,
          min/max evaluation timestamps, cohort ids represented, a deterministic
          content hash, the source hot receipt-id range + a hash of the exact
          source ids, creation+verification timestamps, the archive-tool version,
          the partition path/bytes, and off-host durable-copy confirmation. The
          cold data is queryable via the manifest + the reader in
          scout/trading/receipt_archive.py.

        Observe-only substrate; nothing here feeds the scorer/gate/trader/sender.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS detection_receipt_cohorts (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    cohort_key         TEXT NOT NULL UNIQUE,
                    cohort_start       TEXT NOT NULL,
                    shakedown_start    TEXT,
                    shakedown_end      TEXT,
                    reconciled_through TEXT,
                    status             TEXT NOT NULL,
                    closed_at          TEXT,
                    created_at         TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS detection_receipt_archive_manifest (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    partition_id          TEXT NOT NULL UNIQUE,
                    schema_version        INTEGER NOT NULL,
                    serialization         TEXT NOT NULL,
                    archive_tool_version  TEXT NOT NULL,
                    record_count          INTEGER NOT NULL,
                    min_decided_at        TEXT NOT NULL,
                    max_decided_at        TEXT NOT NULL,
                    cohort_ids            TEXT NOT NULL,
                    content_hash          TEXT NOT NULL,
                    source_min_receipt_id INTEGER NOT NULL,
                    source_max_receipt_id INTEGER NOT NULL,
                    source_ids_hash       TEXT NOT NULL,
                    partition_path        TEXT NOT NULL,
                    compressed_bytes      INTEGER NOT NULL,
                    created_at            TEXT NOT NULL,
                    verified_at           TEXT,
                    offhost_confirmed     INTEGER NOT NULL DEFAULT 0,
                    offhost_confirmed_at  TEXT,
                    status                TEXT NOT NULL
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ddr_manifest_decided "
                "ON detection_receipt_archive_manifest(min_decided_at, max_decided_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ddr_manifest_status "
                "ON detection_receipt_archive_manifest(status)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260727, now_iso, "detection_receipt_archive_v1"),
            )
            await conn.commit()
        except Exception:
            _log.exception(
                "schema_migration_failed", migration="detection_receipt_archive_v1"
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="detection_receipt_archive_v1",
                )
            _log.error(
                "SCHEMA_DRIFT_DETECTED", migration="detection_receipt_archive_v1"
            )
            raise

        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?", (20260727,)
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "detection_receipt_archive_v1 schema_version row missing after migration"
            )

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

        Raises:
        - `CoinIdResolutionError` (RuntimeError subclass) on
          `aiosqlite.OperationalError` so the caller can fail-CLOSED.
        - `DbNotInitializedError` (RuntimeError subclass) when
          `self._conn is None` — distinct class so caller can emit
          `reason="db_not_initialized"` (PR #72 H2) instead of the
          generic resolution_check_error (catastrophic vs transient).

        SQL safety: `table` is interpolated via f-string but values come
        ONLY from the hard-coded tuple below. Settings-driven table list
        is forbidden (would enable injection). PR #72 H3 — fail-loud if
        violated.
        """
        if not coin_id or not coin_id.strip():
            return False
        if self._conn is None:
            raise DbNotInitializedError("Database not initialized.")
        # PR #72 H3 — defense against future caller-driven table values.
        _ALLOWED_TABLES = frozenset(
            {
                "price_cache",
                "gainers_snapshots",
                "volume_history_cg",
                "volume_spikes",
            }
        )
        for table in (
            "price_cache",
            "gainers_snapshots",
            "volume_history_cg",
            "volume_spikes",
        ):
            # Load-bearing identifier guard for the f-string SQL below.
            # An `assert` here would be stripped under `python -O`,
            # silently admitting any future-added table name without
            # allowlist check. Explicit raise survives optimisation.
            if table not in _ALLOWED_TABLES:
                raise ValueError(
                    f"coin_id_resolves: table {table!r} not in allowlist; "
                    "settings-driven tables are forbidden (SQL injection risk)"
                )
            try:
                cur = await self._conn.execute(
                    f"SELECT 1 FROM {table} WHERE coin_id = ? LIMIT 1",
                    (coin_id,),
                )
                if (await cur.fetchone()) is not None:
                    return True
            except aiosqlite.OperationalError as exc:
                raise CoinIdResolutionError(
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
            CREATE INDEX IF NOT EXISTS idx_predictions_coin_predicted_id
                ON predictions(coin_id, predicted_at DESC, id DESC);

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
            CREATE INDEX IF NOT EXISTS idx_sig_events_created_at
                ON signal_events(created_at);

            CREATE TABLE IF NOT EXISTS chain_patterns (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL UNIQUE,
                description          TEXT NOT NULL,
                steps_json           TEXT NOT NULL,
                min_steps_to_trigger INTEGER NOT NULL,
                conviction_boost     INTEGER NOT NULL DEFAULT 0,
                alert_priority       TEXT NOT NULL DEFAULT 'low',
                is_active            INTEGER NOT NULL DEFAULT 1,
                is_protected_builtin INTEGER NOT NULL DEFAULT 0,
                disabled_reason      TEXT,
                disabled_at          TEXT,
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
            CREATE INDEX IF NOT EXISTS idx_vol_hist_cg_recorded
                ON volume_history_cg(recorded_at);

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
                detected_by_acceleration INTEGER DEFAULT 0,
                acceleration_lead_minutes REAL,
                detected_by_momentum INTEGER DEFAULT 0,
                momentum_lead_minutes REAL,
                detected_by_slow_burn INTEGER DEFAULT 0,
                slow_burn_lead_minutes REAL,
                detected_by_velocity INTEGER DEFAULT 0,
                velocity_lead_minutes REAL,
                is_gap INTEGER DEFAULT 1,
                detected_price REAL,
                peak_price REAL,
                peak_gain_pct REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_gainers_comp
                ON gainers_comparisons(coin_id);

            -- BL-NEW-CONVICTION-PROSPECTIVE-SCORE (V1): per-cycle snapshot of
            -- not-yet-pumped CG coins scored by sustained cross-surface early
            -- confirmation. The full history is the prospective-precision event
            -- stream; the latest snapshot_at batch is the live watchlist.
            CREATE TABLE IF NOT EXISTS conviction_watchlist_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_at TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                early_count INTEGER NOT NULL,
                fresh_count INTEGER NOT NULL,
                tier TEXT NOT NULL,
                contributing_surfaces TEXT NOT NULL,
                market_cap REAL,
                mcap_age_minutes REAL,
                first_detection_ages TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_conviction_watchlist_snap
                ON conviction_watchlist_snapshots(snapshot_at);
            CREATE INDEX IF NOT EXISTS idx_conviction_watchlist_snap_tier
                ON conviction_watchlist_snapshots(snapshot_at, tier);

            -- Run heartbeat: one row PER builder run (even a 0-row run), so the
            -- freshness watchdog distinguishes "builder ran, found 0" from
            -- "builder never ran". Freshness keys off run_at, NOT the latest row.
            CREATE TABLE IF NOT EXISTS conviction_watchlist_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_written INTEGER NOT NULL,
                high_tier INTEGER NOT NULL,
                sub30m_high_fresh INTEGER NOT NULL,
                per_surface_contrib TEXT NOT NULL,
                truncated INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_conviction_watchlist_runs_at
                ON conviction_watchlist_runs(run_at);

            CREATE TABLE IF NOT EXISTS gainer_acceleration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                change_1h REAL,
                change_4h REAL,
                vol_expansion REAL,
                market_cap REAL,
                current_price REAL,
                detected_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_gainer_accel
                ON gainer_acceleration(coin_id, detected_at);
            CREATE INDEX IF NOT EXISTS idx_gainer_accel_detected
                ON gainer_acceleration(detected_at);

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
                actionable INTEGER,
                actionability_reason TEXT,
                actionability_version TEXT,

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
            # THE ANCHORED ENTRY BASIS. Established once from the earliest
            # surviving `gainers_snapshots` row and never overwritten.
            #
            # Persisted rather than recomputed because `gainers_snapshots` is
            # pruned at 7 days: "earliest surviving snapshot" is a MOVING
            # target, so a read-time MIN(snapshot_at) silently rebases the
            # entry onto the next row once the original is pruned. Per-row
            # immutability does not make the SELECTION immutable.
            #
            # Forward-only: nullable, never backfilled. A coin whose history
            # was already pruned has no basis and reads as unknown.
            (
                "entry_basis_price",
                "ALTER TABLE gainers_comparisons ADD COLUMN entry_basis_price REAL",
            ),
            (
                "entry_basis_at",
                "ALTER TABLE gainers_comparisons ADD COLUMN entry_basis_at TEXT",
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
                    -- fix/frozen-suppression-lock: dedup marker for the §12b
                    -- permanent-suppression alert. Set once when a suppressed
                    -- combo with no trade in the refresh window is alerted;
                    -- cleared (re-armed) when the combo leaves that state.
                    perm_suppression_alerted_at TEXT,
                    -- D2 retest-terminal-incomplete alert. Set once when a
                    -- parole generation can no longer complete (slots
                    -- exhausted, nothing still open, fewer than the required
                    -- valid resolved outcomes); cleared when the combo leaves
                    -- that state. Distinct from perm_suppression_alerted_at:
                    -- that one marks "suppressed with no trades at all", this
                    -- one marks "retest started and can never finish".
                    retest_incomplete_alerted_at TEXT,
                    -- fix/reversal-alert-durable-retry: the PENDING §12b
                    -- suppression-reversal page, as JSON
                    -- {transition, detected_at, message}. Written when the
                    -- transition is DETECTED, before delivery is attempted, and
                    -- cleared only after a confirmed send — so an outage leaves
                    -- it set and every later refresh re-attempts. Inverted
                    -- polarity from the two markers above: they record "already
                    -- alerted", this one records "still owed". A reversal is
                    -- diffed across a single refresh, so without it a rejected
                    -- page is unrecoverable.
                    reversal_alert_pending_json TEXT,
                    PRIMARY KEY (combo_key, window)
                )
            """)
            # Additive migration for existing DBs — CREATE TABLE IF NOT EXISTS
            # above is a no-op when combo_performance already exists, so new
            # columns must be ALTER-ed in explicitly. Both are nullable with no
            # default, so an upgrade leaves every existing row at NULL, which
            # reads as "never alerted" — the correct pre-cutover state.
            cur_cp = await conn.execute("PRAGMA table_info(combo_performance)")
            cp_cols = {row[1] for row in await cur_cp.fetchall()}
            if "perm_suppression_alerted_at" not in cp_cols:
                await conn.execute(
                    "ALTER TABLE combo_performance "
                    "ADD COLUMN perm_suppression_alerted_at TEXT"
                )
            if "retest_incomplete_alerted_at" not in cp_cols:
                await conn.execute(
                    "ALTER TABLE combo_performance "
                    "ADD COLUMN retest_incomplete_alerted_at TEXT"
                )
            if "reversal_alert_pending_json" not in cp_cols:
                await conn.execute(
                    "ALTER TABLE combo_performance "
                    "ADD COLUMN reversal_alert_pending_json TEXT"
                )

            expected_cols = {
                "signal_combo": "TEXT",
                "lead_time_vs_trending_min": "REAL",
                "lead_time_vs_trending_status": "TEXT",
                "would_be_live": "INTEGER",
                "actionable": "INTEGER",
                "actionability_reason": "TEXT",
                "actionability_version": "TEXT",
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
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (
                    "bl_new_actionability_gate_v1",
                    datetime.now(timezone.utc).isoformat(),
                ),
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
                "'bl065_cashtag_trade_eligible', "
                "'bl_new_actionability_gate_v1')"
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
                "bl_new_actionability_gate_v1",
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
                    'venue_unavailable',
                    'notional_cap_exceeded','signal_disabled','token_aggregate',
                    'dual_signal_aggregate','all_candidates_failed',
                    'master_kill','mode_paper',
                    'live_signed_disabled','api_key_lacks_trade_scope',
                    'mandate_inactive','venue_capability_refused',
                    'no_adapter_for_venue'
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
                    'venue_unavailable',
                    'notional_cap_exceeded','signal_disabled','token_aggregate',
                    'dual_signal_aggregate','all_candidates_failed',
                    'master_kill','mode_paper',
                    'live_signed_disabled','api_key_lacks_trade_scope',
                    'mandate_inactive','venue_capability_refused',
                    'no_adapter_for_venue'
                )),
                exit_order_id       TEXT,
                exit_fill_price     TEXT,
                realized_pnl_usd    TEXT,
                realized_pnl_pct    TEXT,
                kill_event_id       INTEGER REFERENCES kill_events(id),
                created_at          TEXT NOT NULL,
                closed_at           TEXT,
                -- Venue-neutral execution binding (2026-08-02). Both nullable with
                -- no DEFAULT: NULL means "written before intents were bound", which
                -- is what makes the mandate's supervised-history count start at zero
                -- instead of inheriting history that was never mandate-gated.
                intent_hash         TEXT,
                mandate_mode        TEXT
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
            cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
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
            cur_pragma_pt = await conn.execute("PRAGMA table_info(paper_trades)")
            existing_pt_cols = {row[1] for row in await cur_pragma_pt.fetchall()}
            if "conviction_locked_at" not in existing_pt_cols:
                await conn.execute(
                    "ALTER TABLE paper_trades " "ADD COLUMN conviction_locked_at TEXT"
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

    async def _migrate_high_peak_fade_columns_and_audit_table(self) -> None:
        """BL-NEW-HPF: per-signal opt-in column + fire-events audit table.

        Adds:
          - signal_params.high_peak_fade_enabled INTEGER DEFAULT 0
          - high_peak_fade_audit table (records both real and dry-run fires)

        Wrapped in ``BEGIN EXCLUSIVE`` / ``ROLLBACK`` and stamped with a
        ``schema_version`` row + ``paper_migrations`` cutover marker, consistent
        with the pattern established by ``_migrate_feedback_loop_schema``,
        ``_migrate_live_trading_schema``, and ``_migrate_signal_params_schema``.

        Idempotent: column-add is guarded by a PRAGMA existence-check;
        table-create and index-create use IF NOT EXISTS; inserts use
        INSERT OR IGNORE.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")

            # Defensive create — mirrors _migrate_signal_params_schema guard so
            # this migration is safe even if run in isolation (e.g. tests).
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY,
                    cutover_ts TEXT NOT NULL
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
                """)

            # Column-add: guarded by PRAGMA to stay idempotent under
            # BEGIN EXCLUSIVE (ALTER TABLE inside a transaction is valid in
            # SQLite 3.x; the PRAGMA read is also valid inside the txn).
            cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "high_peak_fade_enabled" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params "
                    "ADD COLUMN high_peak_fade_enabled INTEGER NOT NULL DEFAULT 0"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS high_peak_fade_audit (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id      INTEGER NOT NULL,
                    token_id      TEXT    NOT NULL,
                    signal_type   TEXT    NOT NULL,
                    peak_pct      REAL    NOT NULL,
                    peak_price    REAL    NOT NULL,
                    current_price REAL    NOT NULL,
                    threshold_pct REAL    NOT NULL,
                    retrace_pct   REAL    NOT NULL,
                    fired_at      TEXT    NOT NULL,
                    dry_run       INTEGER NOT NULL,
                    UNIQUE(trade_id, threshold_pct, dry_run),
                    FOREIGN KEY (trade_id) REFERENCES paper_trades(id)
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hpf_audit_trade_id "
                "ON high_peak_fade_audit(trade_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hpf_audit_fired_at "
                "ON high_peak_fade_audit(fired_at)"
            )

            # Behavioural cutover marker (matches BL-061..BL-067 pattern).
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_hpf_v1", now_iso),
            )
            # Code-level schema version stamp (matches BL-055 / Tier-1a pattern).
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260505, now_iso, "bl_hpf_v1_high_peak_fade"),
            )

            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_hpf_v1")
            raise

        # Post-assertion — cutover row must exist after a successful migration.
        cur = await conn.execute(
            "SELECT 1 FROM paper_migrations WHERE name = ?",
            ("bl_hpf_v1",),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("bl_hpf_v1 cutover row missing after migration")

    async def _migrate_autosuspend_baseline_column(self) -> None:
        """BL-NEW-AUTOSUSPEND-FIX: per-signal drawdown rolling-window floor.

        Adds:
          - signal_params.drawdown_baseline_at TEXT (nullable)

        Operator revival stamps this column with NOW() so the auto_suspend
        rolling window doesn't carry historical drawdown across the revival
        boundary. Existing rows default to NULL — no behavior change for
        signals that have never been suspended/revived.

        Wrapped in BEGIN EXCLUSIVE / ROLLBACK + paper_migrations cutover row +
        schema_version stamp, matching the BL-NEW-HPF migration pattern.
        Idempotent: column-add is guarded by PRAGMA existence-check.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY,
                    cutover_ts TEXT NOT NULL
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
                """)

            cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "drawdown_baseline_at" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params " "ADD COLUMN drawdown_baseline_at TEXT"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_autosuspend_baseline_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260506, now_iso, "bl_autosuspend_baseline_v1"),
            )

            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_autosuspend_baseline_v1")
            raise

        # Post-assertion — cutover row must exist after a successful migration.
        cur = await conn.execute(
            "SELECT 1 FROM paper_migrations WHERE name = ?",
            ("bl_autosuspend_baseline_v1",),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "bl_autosuspend_baseline_v1 cutover row missing after migration"
            )

    async def _migrate_moonshot_opt_out_column(self) -> None:
        """BL-NEW-MOONSHOT-OPT-OUT: per-signal moonshot regime opt-out flag.

        Adds:
          - signal_params.moonshot_enabled INTEGER NOT NULL DEFAULT 1

        When 0, the evaluator skips the
        ``max(PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)`` floor in
        moonshot regime (peak >= 40%) and uses ``sp.trail_pct`` directly.
        Default 1 preserves current behavior for all existing rows.

        Resolves the structural finding documented in
        ``tasks/findings_moonshot_floor_nullification.md``: the moonshot
        floor silently dominated per-signal ``trail_pct`` even when a
        signal would benefit from a tighter / wider trail.

        Wrapped in BEGIN EXCLUSIVE / ROLLBACK + paper_migrations cutover
        row (``bl_moonshot_opt_out_v1``) + schema_version 20260507,
        consistent with the BL-NEW-HPF + BL-NEW-AUTOSUSPEND migration
        patterns. Idempotent: column-add is guarded by PRAGMA
        existence-check; INSERT OR IGNORE on both stamps.

        Note: a moonshot-opted-out trade that closes via trail still
        carries the ``closed_moonshot_trail`` exit-status (the close-
        labeling path checks ``moonshot_armed_at is not None``, which
        is independent of ``moonshot_enabled``). This is a minor
        semantic oddity, not a correctness bug — flagged by the
        design-stage data-soundness reviewer.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY,
                    cutover_ts TEXT NOT NULL
                )
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
                """)

            cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "moonshot_enabled" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params "
                    "ADD COLUMN moonshot_enabled INTEGER NOT NULL DEFAULT 1"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_moonshot_opt_out_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260507, now_iso, "bl_moonshot_opt_out_v1"),
            )

            # Post-assertion INSIDE the try block (per design-stage
            # correctness reviewer RECOMMEND): if the cutover row is
            # missing, ROLLBACK + log SCHEMA_DRIFT before raising.
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_moonshot_opt_out_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError(
                    "bl_moonshot_opt_out_v1 cutover row missing after migration"
                )

            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_moonshot_opt_out_v1")
            raise

    async def _migrate_live_eligible_column(self) -> None:
        """BL-NEW-LIVE-HYBRID M1: per-signal live-execution opt-in.

        Adds signal_params.live_eligible INTEGER NOT NULL DEFAULT 0.
        Layer 3 of the 4-layer kill stack."""
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL
                )""")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )""")
            cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "live_eligible" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params "
                    "ADD COLUMN live_eligible INTEGER NOT NULL DEFAULT 0"
                )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_live_eligible_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260508, now_iso, "bl_live_eligible_v1"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_live_eligible_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError("bl_live_eligible_v1 cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_live_eligible_v1")
            raise

    async def _migrate_per_venue_services(self) -> None:
        """BL-NEW-LIVE-HYBRID M1: 5 per-venue services tables + 2 cross-venue views.

        Tables: venue_health, wallet_snapshots, venue_listings,
        venue_rate_state, symbol_aliases. All idempotent via
        ``CREATE TABLE IF NOT EXISTS``.

        Views: ``cross_venue_exposure`` (M1 — Gate 7 blocker, sums open
        exposure across binance + minara_<chain>) and ``cross_venue_pnl``
        (M1 scaffold; M2 fills in). Views are created here (not in
        ``_create_tables``) because they reference ``live_trades`` which
        is defined in :meth:`_migrate_live_trading_schema` — that migration
        runs before this one in :meth:`initialize`, so both source tables
        are guaranteed to exist by the time the view DDL runs.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL
                )""")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )""")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS venue_health (
                    venue                   TEXT NOT NULL,
                    probe_at                TEXT NOT NULL,
                    rest_responsive         INTEGER NOT NULL,
                    rest_latency_ms         INTEGER,
                    ws_connected            INTEGER NOT NULL,
                    rate_limit_headroom_pct REAL,
                    auth_ok                 INTEGER NOT NULL,
                    last_balance_fetch_ok   INTEGER NOT NULL,
                    last_quote_mid_price    REAL,
                    last_quote_at           TEXT,
                    last_depth_at_size_bps  REAL,
                    fills_30d_count         INTEGER NOT NULL DEFAULT 0,
                    is_dormant              INTEGER NOT NULL DEFAULT 0,
                    error_text              TEXT,
                    PRIMARY KEY (venue, probe_at)
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_venue_health_recent "
                "ON venue_health(venue, probe_at DESC)"
            )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS wallet_snapshots (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue         TEXT NOT NULL,
                    asset         TEXT NOT NULL,
                    balance       REAL NOT NULL,
                    balance_usd   REAL,
                    snapshot_at   TEXT NOT NULL
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_venue_recent "
                "ON wallet_snapshots(venue, snapshot_at DESC)"
            )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS venue_listings (
                    venue         TEXT NOT NULL,
                    canonical     TEXT NOT NULL,
                    venue_pair    TEXT NOT NULL,
                    quote         TEXT NOT NULL,
                    asset_class   TEXT NOT NULL CHECK (
                        asset_class IN ('spot','perp','option','equity','forex')),
                    listed_at     TEXT,
                    delisted_at   TEXT,
                    refreshed_at  TEXT NOT NULL,
                    PRIMARY KEY (venue, canonical, asset_class)
                )""")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS venue_rate_state (
                    venue                TEXT PRIMARY KEY,
                    last_updated_at      TEXT NOT NULL,
                    requests_per_min_cap INTEGER NOT NULL,
                    requests_seen_60s    INTEGER NOT NULL DEFAULT 0,
                    headroom_pct         REAL NOT NULL DEFAULT 100.0
                )""")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS symbol_aliases (
                    canonical    TEXT NOT NULL,
                    venue        TEXT NOT NULL,
                    venue_symbol TEXT NOT NULL,
                    PRIMARY KEY (canonical, venue)
                )""")

            # Task 4: cross-venue views. Defined here (not in
            # _create_tables) because they reference live_trades, which is
            # created by _migrate_live_trading_schema (runs earlier in
            # initialize()). M1 ships cross_venue_pnl as a placeholder
            # scaffold; M2 fills in the realized/unrealized math.
            await conn.execute("""
                CREATE VIEW IF NOT EXISTS cross_venue_exposure AS
                SELECT
                    'binance' AS venue,
                    COALESCE(SUM(CAST(size_usd AS REAL)), 0) AS open_exposure_usd,
                    COUNT(*) AS open_count
                FROM live_trades
                WHERE status = 'open'
                UNION ALL
                SELECT
                    'minara_' || COALESCE(chain, 'unknown') AS venue,
                    COALESCE(SUM(amount_usd), 0) AS open_exposure_usd,
                    COUNT(*) AS open_count
                FROM paper_trades
                WHERE status = 'open'
                  AND chain != 'coingecko'
                  AND chain != ''
                GROUP BY chain""")

            await conn.execute("""
                CREATE VIEW IF NOT EXISTS cross_venue_pnl AS
                SELECT
                    'placeholder_m1' AS venue,
                    0.0 AS realized_pnl_usd,
                    0.0 AS unrealized_pnl_usd""")

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_per_venue_services_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260510, now_iso, "bl_per_venue_services_v1"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_per_venue_services_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError("bl_per_venue_services_v1 cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_per_venue_services_v1")
            raise

    async def _migrate_live_trades_telemetry(self) -> None:
        """BL-NEW-LIVE-HYBRID M1 v2.1: telemetry plumbing for
        approval-removal criteria (per plan-stage policy reviewer).
        Adds:
          - live_trades.fill_slippage_bps REAL
          - live_trades.correction_at TEXT
          - live_trades.correction_reason TEXT
          - signal_venue_correction_count table (running counters)
        Layer 4-prep: approval-removal gate reads these. M1 ships schema;
        runtime population deferred to Task 12 (slippage compute) + Task
        11 (correction counter wiring)."""
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL)""")

            cur_pragma = await conn.execute("PRAGMA table_info(live_trades)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            for col, ddl in [
                ("fill_slippage_bps", "REAL"),
                ("correction_at", "TEXT"),
                ("correction_reason", "TEXT"),
            ]:
                if col not in existing_cols:
                    await conn.execute(
                        f"ALTER TABLE live_trades ADD COLUMN {col} {ddl}"
                    )

            await conn.execute(
                """CREATE TABLE IF NOT EXISTS signal_venue_correction_count (
                    signal_type              TEXT NOT NULL,
                    venue                    TEXT NOT NULL,
                    consecutive_no_correction INTEGER NOT NULL DEFAULT 0,
                    last_corrected_at        TEXT,
                    last_updated_at          TEXT NOT NULL,
                    PRIMARY KEY (signal_type, venue)
                )"""
            )

            # Task 13/13.5: ephemeral operator-set overrides
            # (/allow-stack, /auto-approve, /approval-required, /venue-revive).
            # Read by approval_thresholds.should_require_approval (gate 4).
            await conn.execute("""CREATE TABLE IF NOT EXISTS live_operator_overrides (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    override_type TEXT NOT NULL CHECK (override_type IN (
                        'allow_stack','auto_approve','approval_required','venue_revive'
                    )),
                    venue         TEXT,
                    canonical     TEXT,
                    set_at        TEXT NOT NULL,
                    expires_at    TEXT NOT NULL,
                    set_by        TEXT
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_operator_overrides_active "
                "ON live_operator_overrides(override_type, venue, expires_at)"
            )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_live_trades_telemetry_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260511, now_iso, "bl_live_trades_telemetry_v1"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_live_trades_telemetry_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError("bl_live_trades_telemetry_v1 cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error(
                "SCHEMA_DRIFT_DETECTED",
                migration="bl_live_trades_telemetry_v1",
            )
            raise

    async def _migrate_live_client_order_id(self) -> None:
        """BL-NEW-LIVE-HYBRID M1 v2.1 (Task 12): client_order_id idempotency.

        Adds live_trades.client_order_id TEXT (UNIQUE) for the
        gecko-side idempotency contract: every order intent computes
        `client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"`,
        which Binance accepts as `newClientOrderId`. Pre-retry dedup
        query checks this column before submitting to the venue;
        retried submits return the existing venue_order_id.

        UNIQUE allows SQLite to enforce exactly-once at the DB layer
        as a backstop. NULL allowed for old rows (pre-migration) and
        for shadow_mode rows that never hit a venue.

        Migration `bl_live_client_order_id_v1`, schema_version 20260509.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL)""")

            cur_pragma = await conn.execute("PRAGMA table_info(live_trades)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "client_order_id" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE live_trades ADD COLUMN client_order_id TEXT"
                )
                # UNIQUE INDEX (partial, ignores NULLs) gives us the
                # dedup guarantee without ALTER-CHECK rebuild costs.
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_live_trades_client_order_id "
                    "ON live_trades(client_order_id) "
                    "WHERE client_order_id IS NOT NULL"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_live_client_order_id_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260509, now_iso, "bl_live_client_order_id_v1"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_live_client_order_id_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError("bl_live_client_order_id_v1 cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error(
                "SCHEMA_DRIFT_DETECTED",
                migration="bl_live_client_order_id_v1",
            )
            raise

    async def _migrate_reject_reason_extend(self) -> None:
        """V3 reviewer C-1: extend reject_reason CHECK constraint on
        existing prod tables via SQLite table-rename pattern.

        CREATE TABLE IF NOT EXISTS in `_create_tables` is a no-op for
        existing tables, so the BL-NEW-LIVE-HYBRID M1 v2.1 CHECK
        extension (7 new reject_reasons) only applies to fresh DBs.
        On prod (BL-055 shadow soak DB exists since 2026-04-23), the
        old 9-value CHECK persists. First INSERT with a new reason
        (`signal_disabled` from Gate 9 / `notional_cap_exceeded` from
        Gate 8 / `master_kill` / `mode_paper` / etc.) raises
        sqlite3.IntegrityError + crashes the engine path.

        This migration rebuilds shadow_trades + live_trades with the
        16-value CHECK via CREATE-NEW-COPY-DROP-RENAME. Tables are
        append-only and small (LIVE_MODE was 'paper' until M1.5 wires
        balance_gate), so COPY cost is bounded.

        Migration `bl_reject_reason_extend_v1`, schema_version 20260512.
        Idempotent — checks current CHECK constraint via PRAGMA table_info
        + sqlite_master.sql before rebuilding.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL)""")

            # Idempotency check: skip if marker already present.
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_reject_reason_extend_v1",),
            )
            if (await cur.fetchone()) is not None:
                await conn.commit()
                return

            # Determine if rebuild is needed by checking sqlite_master.sql
            # for one of the new reasons. If 'notional_cap_exceeded' is
            # already present in the CHECK (fresh DB), skip the rebuild
            # and just stamp the marker.
            for table in ("shadow_trades", "live_trades"):
                cur = await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                row = await cur.fetchone()
                if row is None:
                    continue  # table doesn't exist yet (fresh DB pre-_create_tables)
                table_sql = row[0] or ""
                if "notional_cap_exceeded" in table_sql:
                    continue  # already has the new CHECK; nothing to do

                # Rebuild via table-rename. The new CHECK has 16 values
                # matching the latest CREATE TABLE in _create_tables.
                # Preserve column order + types by extracting via PRAGMA.
                cur = await conn.execute(f"PRAGMA table_info({table})")
                cols = await cur.fetchall()  # (cid, name, type, notnull, default, pk)
                if not cols:
                    continue
                col_names = [c[1] for c in cols]
                col_list = ", ".join(col_names)

                # Build the NEW CHECK CREATE TABLE by string-replacing the
                # CHECK clause. The old shape is:
                #   reject_reason TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
                #       ...9 values...
                #   ))
                # Build the new clause inline.
                new_check = (
                    "CHECK (reject_reason IS NULL OR reject_reason IN ("
                    "'no_venue','insufficient_depth','slippage_exceeds_cap',"
                    "'insufficient_balance','daily_cap_hit','kill_switch',"
                    "'exposure_cap','override_disabled','venue_unavailable',"
                    "'notional_cap_exceeded','signal_disabled','token_aggregate',"
                    "'dual_signal_aggregate','all_candidates_failed',"
                    "'master_kill','mode_paper'))"
                )
                # The new CREATE TABLE keeps everything but replaces just
                # the reject_reason CHECK. Use the existing _create_tables
                # statement reference: we know shadow_trades and live_trades
                # have a `reject_reason TEXT CHECK (...)` clause. Regex the
                # old CHECK out + sub the new one.
                import re as _re

                # Match `reject_reason       TEXT CHECK (...)` with arbitrary
                # whitespace and multi-line CHECK list.
                pattern = _re.compile(
                    r"reject_reason\s+TEXT\s+CHECK\s*\([^)]*\)\s*\)",
                    _re.IGNORECASE | _re.DOTALL,
                )
                new_table_sql = pattern.sub(
                    f"reject_reason       TEXT {new_check}", table_sql
                )
                if new_table_sql == table_sql:
                    _log.warning(
                        "reject_reason_check_pattern_miss",
                        table=table,
                        sql_excerpt=table_sql[:200],
                    )
                    continue

                # Rename the matched _new table name in CREATE TABLE
                # statement to <table>_new for the rebuild.
                new_table_sql_renamed = new_table_sql.replace(
                    f"TABLE {table} (", f"TABLE {table}_new (", 1
                ).replace(
                    f"TABLE IF NOT EXISTS {table} (",
                    f"TABLE {table}_new (",
                    1,
                )

                # SQLite refuses to DROP a table while a view references it.
                # cross_venue_exposure references live_trades; drop both views
                # before the rebuild and recreate them after with the same
                # DDL used in `_migrate_per_venue_services`. Idempotent via
                # CREATE VIEW IF NOT EXISTS — re-runs on already-rebuilt DB
                # are no-ops because the views already exist (this branch
                # short-circuits earlier on the marker check).
                if table == "live_trades":
                    await conn.execute("DROP VIEW IF EXISTS cross_venue_exposure")
                    await conn.execute("DROP VIEW IF EXISTS cross_venue_pnl")

                await conn.execute(new_table_sql_renamed)
                await conn.execute(
                    f"INSERT INTO {table}_new ({col_list}) "
                    f"SELECT {col_list} FROM {table}"
                )
                await conn.execute(f"DROP TABLE {table}")
                await conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
                _log.info(
                    "reject_reason_check_rebuilt",
                    table=table,
                    rows_copied=(
                        await (
                            await conn.execute(f"SELECT COUNT(*) FROM {table}")
                        ).fetchone()
                    )[0],
                )

                if table == "live_trades":
                    # Recreate views (DDL identical to _migrate_per_venue_services)
                    await conn.execute(
                        """CREATE VIEW IF NOT EXISTS cross_venue_exposure AS
                           SELECT
                               'binance' AS venue,
                               COALESCE(SUM(CAST(size_usd AS REAL)), 0) AS open_exposure_usd,
                               COUNT(*) AS open_count
                           FROM live_trades
                           WHERE status = 'open'
                           UNION ALL
                           SELECT
                               'minara_' || COALESCE(chain, 'unknown') AS venue,
                               COALESCE(SUM(amount_usd), 0) AS open_exposure_usd,
                               COUNT(*) AS open_count
                           FROM paper_trades
                           WHERE status = 'open'
                             AND chain != 'coingecko'
                             AND chain != ''
                           GROUP BY chain"""
                    )
                    await conn.execute("""CREATE VIEW IF NOT EXISTS cross_venue_pnl AS
                           SELECT
                               'placeholder_m1' AS venue,
                               0.0 AS realized_pnl_usd,
                               0.0 AS unrealized_pnl_usd""")

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_reject_reason_extend_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260512, now_iso, "bl_reject_reason_extend_v1"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_reject_reason_extend_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError("bl_reject_reason_extend_v1 cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error(
                "SCHEMA_DRIFT_DETECTED",
                migration="bl_reject_reason_extend_v1",
            )
            raise

    async def _migrate_bl_quote_pair_v1(self) -> None:
        """BL-NEW-QUOTE-PAIR: add `quote_symbol` + `dex_id` to candidates.

        Stable-pair liquidity-quality signal capture. DexScreener already
        returns `quoteToken.symbol` + `dexId` per pair; this migration adds
        nullable TEXT columns to persist them. Pre-cutover rows stay NULL
        per ``feedback_mid_flight_flag_migration.md`` discipline.

        Migration `bl_quote_pair_v1`, schema_version 20260513.
        Idempotent: PRAGMA-guarded ALTER per existing
        ``_migrate_high_peak_fade_columns_and_audit_table`` pattern.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")

            # Defensive create — mirrors HPF/Tier-1a pattern; safe in isolation.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
            """)

            expected_cols = {"quote_symbol": "TEXT", "dex_id": "TEXT"}
            cur_pragma = await conn.execute("PRAGMA table_info(candidates)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing_cols:
                    _log.info(
                        "schema_migration_column_action",
                        migration="bl_quote_pair_v1",
                        col=col,
                        action="skip_exists",
                    )
                else:
                    await conn.execute(
                        f"ALTER TABLE candidates ADD COLUMN {col} {coltype}"
                    )
                    _log.info(
                        "schema_migration_column_action",
                        migration="bl_quote_pair_v1",
                        col=col,
                        action="added",
                    )

            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260513, now_iso, "bl_quote_pair_v1_quote_symbol_dex_id"),
            )

            await conn.commit()
        except Exception as e:
            # R6 PR review MUST-FIX: capture original exception with type+str,
            # log original BEFORE the rollback attempt so a rollback failure
            # doesn't bury the root-cause traceback. Distinguish lock-contention
            # from genuine schema drift via exception type so on-call doesn't
            # page on the wrong cause.
            _log.exception(
                "schema_migration_failed",
                migration="bl_quote_pair_v1",
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="bl_quote_pair_v1",
                    err=str(rb_err),
                    err_type=type(rb_err).__name__,
                )
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_quote_pair_v1")
            raise

        # Post-assertion — schema_version row must exist with our description.
        # R6 PR review MUST-FIX: also assert description matches, to surface a
        # version-collision case where some external tool pre-seeded
        # version=20260513 with a different description; INSERT OR IGNORE would
        # silently skip the write and the post-assertion would still pass with
        # the old (wrong) row, producing inconsistent state.
        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?", (20260513,)
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "bl_quote_pair_v1 schema_version row missing after migration"
            )
        if row[0] != "bl_quote_pair_v1_quote_symbol_dex_id":
            raise RuntimeError(
                f"bl_quote_pair_v1 schema_version description mismatch — "
                f"expected 'bl_quote_pair_v1_quote_symbol_dex_id', got {row[0]!r}. "
                f"Possible version-collision; investigate before continuing."
            )

    async def _migrate_liquidity_enrichment_v1(self) -> None:
        """BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS Phase 1a-i (2026-05-29):
        add 4 nullable enrichment columns to ``candidates``.

        Writer is read-only enrichment via DexScreener cron (Phase 1a-ii); this
        migration only lands the persistence shape. All columns are nullable
        with NO DEFAULT so the dashboard read path can distinguish three
        states explicitly:

        - column IS NULL: row never visited by the cron writer.
        - column IS NOT NULL with ``confidence='dex_no_match'``: writer
          visited, DexScreener returned no pair — display as "unavailable".
        - column IS NOT NULL with ``confidence='definite'`` / ``'multi_chain'``:
          writer succeeded; display the value with appropriate advisory.

        Migration ``bl_new_liquidity_enrichment_v1``, schema_version 20260529.
        Idempotent: PRAGMA-guarded ALTER mirrors ``_migrate_bl_quote_pair_v1``.

        Both ``schema_version`` and ``paper_migrations`` rows are written —
        the paper_migrations cutover_ts is the canonical reference point for
        the Phase 1 measurement substrate per
        ``tasks/design_liquidity_enrichment_b2_2026_05_29.md``.

        Anti-scope: NO writer, NO ranking/filtering/alerting/sizing consumer,
        NO modification of existing ``liquidity_usd`` semantics, NO dashboard
        read change in this Phase 1a-i migration.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")

            # Defensive create — mirrors _migrate_bl_quote_pair_v1.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
            """)
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )

            # All columns nullable, no DEFAULT — preserves absence-vs-zero
            # semantics per feedback_mid_flight_flag_migration.md.
            expected_cols = {
                "liquidity_usd_enriched": "REAL",
                "liquidity_enriched_source": "TEXT",
                "liquidity_enriched_at": "TEXT",
                "liquidity_enriched_confidence": "TEXT",
            }
            cur_pragma = await conn.execute("PRAGMA table_info(candidates)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing_cols:
                    _log.info(
                        "schema_migration_column_action",
                        migration="bl_new_liquidity_enrichment_v1",
                        col=col,
                        action="skip_exists",
                    )
                else:
                    await conn.execute(
                        f"ALTER TABLE candidates ADD COLUMN {col} {coltype}"
                    )
                    _log.info(
                        "schema_migration_column_action",
                        migration="bl_new_liquidity_enrichment_v1",
                        col=col,
                        action="added",
                    )

            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (
                    20260529,
                    now_iso,
                    "bl_new_liquidity_enrichment_v1_candidates_enrichment_cols",
                ),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations "
                "(name, cutover_ts) VALUES (?, ?)",
                ("bl_new_liquidity_enrichment_v1", now_iso),
            )

            await conn.commit()
        except Exception as e:
            _log.exception(
                "schema_migration_failed",
                migration="bl_new_liquidity_enrichment_v1",
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="bl_new_liquidity_enrichment_v1",
                    err=str(rb_err),
                    err_type=type(rb_err).__name__,
                )
            _log.error(
                "SCHEMA_DRIFT_DETECTED",
                migration="bl_new_liquidity_enrichment_v1",
            )
            raise

        # Post-assertion mirrors _migrate_bl_quote_pair_v1 (R6 pattern).
        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?",
            (20260529,),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "bl_new_liquidity_enrichment_v1 schema_version row "
                "missing after migration"
            )
        if row[0] != "bl_new_liquidity_enrichment_v1_candidates_enrichment_cols":
            raise RuntimeError(
                "bl_new_liquidity_enrichment_v1 schema_version description "
                "mismatch — expected "
                "'bl_new_liquidity_enrichment_v1_candidates_enrichment_cols', "
                f"got {row[0]!r}. Possible version-collision; investigate "
                "before continuing."
            )
        cur = await conn.execute(
            "SELECT cutover_ts FROM paper_migrations WHERE name = ?",
            ("bl_new_liquidity_enrichment_v1",),
        )
        if (await cur.fetchone()) is None:
            raise RuntimeError(
                "bl_new_liquidity_enrichment_v1 paper_migrations row "
                "missing after migration"
            )

        # Post-assertion 2: all 4 columns present in candidates.
        cur = await conn.execute("PRAGMA table_info(candidates)")
        final_cols = {row[1] for row in await cur.fetchall()}
        missing = set(expected_cols.keys()) - final_cols
        if missing:
            raise RuntimeError(
                "bl_new_liquidity_enrichment_v1 schema migration incomplete: "
                f"missing columns {missing}"
            )

    async def _migrate_reject_reason_extend_v2(self) -> None:
        """BL-NEW-LIVE-HYBRID M1.5a (design-stage R1-I1 + R2-I3): extend
        reject_reason CHECK constraint with 2 new values via SQLite
        table-rename pattern (mirrors `_migrate_reject_reason_extend` from
        M1's V3-C1).

        Adds:
          - 'live_signed_disabled' — Gate 10 emergency-revert kill-switch
            visibility when LIVE_USE_REAL_SIGNED_REQUESTS=False (R1-I1)
          - 'api_key_lacks_trade_scope' — Gate 10 disambiguation of -2015
            (key lacks SPOT) from generic insufficient_balance (R2-I3)

        Migration `bl_reject_reason_extend_v2`, schema_version 20260514.
        Idempotent — checks current CHECK constraint via sqlite_master.sql
        before rebuilding.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL)""")

            # Idempotency check: skip if marker already present.
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_reject_reason_extend_v2",),
            )
            if (await cur.fetchone()) is not None:
                await conn.commit()
                return

            for table in ("shadow_trades", "live_trades"):
                cur = await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                row = await cur.fetchone()
                if row is None:
                    continue
                table_sql = row[0] or ""
                if "live_signed_disabled" in table_sql:
                    continue  # already has the new CHECK; nothing to do

                cur = await conn.execute(f"PRAGMA table_info({table})")
                cols = await cur.fetchall()
                if not cols:
                    continue
                col_names = [c[1] for c in cols]
                col_list = ", ".join(col_names)

                new_check = (
                    "CHECK (reject_reason IS NULL OR reject_reason IN ("
                    "'no_venue','insufficient_depth','slippage_exceeds_cap',"
                    "'insufficient_balance','daily_cap_hit','kill_switch',"
                    "'exposure_cap','override_disabled','venue_unavailable',"
                    "'notional_cap_exceeded','signal_disabled','token_aggregate',"
                    "'dual_signal_aggregate','all_candidates_failed',"
                    "'master_kill','mode_paper',"
                    "'live_signed_disabled','api_key_lacks_trade_scope'))"
                )
                import re as _re

                pattern = _re.compile(
                    r"reject_reason\s+TEXT\s+CHECK\s*\([^)]*\)\s*\)",
                    _re.IGNORECASE | _re.DOTALL,
                )
                new_table_sql = pattern.sub(
                    f"reject_reason       TEXT {new_check}", table_sql
                )
                if new_table_sql == table_sql:
                    _log.warning(
                        "reject_reason_check_v2_pattern_miss",
                        table=table,
                        sql_excerpt=table_sql[:200],
                    )
                    continue

                # Hotfix 2026-05-09: prod's sqlite_master.sql uses
                # CREATE TABLE "shadow_trades" (quoted) after M1's V3-C1
                # rebuild. Original substring replace missed that. Use
                # regex covering both quoted + unquoted + IF-NOT-EXISTS.
                new_table_sql_renamed = _re.sub(
                    rf'TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?{table}["`]?\s*\(',
                    f"TABLE {table}_new (",
                    new_table_sql,
                    count=1,
                    flags=_re.IGNORECASE,
                )
                if new_table_sql_renamed == new_table_sql:
                    _log.warning(
                        "reject_reason_check_v2_rename_pattern_miss",
                        table=table,
                        sql_excerpt=new_table_sql[:200],
                    )
                    continue

                # Drop dependent views before rebuild (M1 hotfix lesson —
                # cross_venue_exposure references live_trades).
                if table == "live_trades":
                    await conn.execute("DROP VIEW IF EXISTS cross_venue_exposure")
                    await conn.execute("DROP VIEW IF EXISTS cross_venue_pnl")

                await conn.execute(new_table_sql_renamed)
                await conn.execute(
                    f"INSERT INTO {table}_new ({col_list}) "
                    f"SELECT {col_list} FROM {table}"
                )
                await conn.execute(f"DROP TABLE {table}")
                await conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")

                if table == "live_trades":
                    # Recreate views (DDL identical to _migrate_per_venue_services
                    # + I1+I2 fixes from M1).
                    await conn.execute(
                        """CREATE VIEW IF NOT EXISTS cross_venue_exposure AS
                           SELECT
                               'binance' AS venue,
                               COALESCE(SUM(CAST(size_usd AS REAL)), 0) AS open_exposure_usd,
                               COUNT(*) AS open_count
                           FROM live_trades
                           WHERE status = 'open'
                           UNION ALL
                           SELECT
                               'minara_' || COALESCE(chain, 'unknown') AS venue,
                               COALESCE(SUM(amount_usd), 0) AS open_exposure_usd,
                               COUNT(*) AS open_count
                           FROM paper_trades
                           WHERE status = 'open'
                             AND chain != 'coingecko'
                             AND chain != ''
                           GROUP BY chain"""
                    )
                    await conn.execute("""CREATE VIEW IF NOT EXISTS cross_venue_pnl AS
                           SELECT
                               'placeholder_m1' AS venue,
                               0.0 AS realized_pnl_usd,
                               0.0 AS unrealized_pnl_usd""")

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_reject_reason_extend_v2", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260514, now_iso, "bl_reject_reason_extend_v2"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_reject_reason_extend_v2",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError("bl_reject_reason_extend_v2 cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error(
                "SCHEMA_DRIFT_DETECTED",
                migration="bl_reject_reason_extend_v2",
            )
            raise

    async def _migrate_bl_slow_burn_v1(self) -> None:
        """BL-075 Phase B: create slow_burn_candidates table.

        Migration bl_slow_burn_v1, schema_version 20260515.
        Mcap nullable (Phase A blind-spot fix); composite index for dedup
        hot-path; canonical BEGIN EXCLUSIVE / try-except-ROLLBACK pattern.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS slow_burn_candidates (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin_id         TEXT    NOT NULL,
                    symbol          TEXT    NOT NULL,
                    name            TEXT,
                    price_change_7d REAL    NOT NULL,
                    price_change_1h REAL    NOT NULL,
                    price_change_24h REAL,
                    market_cap      REAL,
                    current_price   REAL,
                    volume_24h      REAL,
                    also_in_momentum_7d INTEGER NOT NULL DEFAULT 0,
                    detected_at     TEXT    NOT NULL
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_slow_burn_detected "
                "ON slow_burn_candidates(detected_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_slow_burn_coin_date "
                "ON slow_burn_candidates(coin_id, detected_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260515, now_iso, "bl_slow_burn_v1_slow_burn_candidates"),
            )
            await conn.commit()
        except Exception as e:
            _log.exception(
                "schema_migration_failed",
                migration="bl_slow_burn_v1",
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration="bl_slow_burn_v1",
                    err=str(rb_err),
                    err_type=type(rb_err).__name__,
                )
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_slow_burn_v1")
            raise

        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?",
            (20260515,),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "bl_slow_burn_v1 schema_version row missing after migration"
            )
        if row[0] != "bl_slow_burn_v1_slow_burn_candidates":
            raise RuntimeError(
                f"bl_slow_burn_v1 schema_version description mismatch — "
                f"expected 'bl_slow_burn_v1_slow_burn_candidates', got {row[0]!r}"
            )

    async def _migrate_tg_alert_eligible_v1(self) -> None:
        """BL-NEW-TG-ALERT-ALLOWLIST: per-signal TG alert eligibility.

        Schema version 20260516. Adds signal_params.tg_alert_eligible
        (default 0). Sets eligibility=1 for the 4 statistically-validated
        signals (gainers_early, narrative_prediction, losers_contrarian,
        volume_spike). chain_completed stays 0 because the existing
        scout/chains/alerts.py path already alerts on chain pattern
        completion; setting it 1 here would duplicate.

        Creates tg_alert_log for audit + cooldown lookup. CHECK constraint
        admits 'announcement_sent' for first-deploy operator announcement
        sentinel (R2-C1 design fold).

        Per-signal post-assertion (R1-C2 design fold) — robust to future
        signal_params seed-list changes vs COUNT(*)=4.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()
        DEFAULT_ALLOW = (
            "gainers_early",
            "narrative_prediction",
            "losers_contrarian",
            "volume_spike",
        )
        migration_name = "bl_tg_alert_eligible_v1"

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                (migration_name,),
            )
            if await cur.fetchone():
                await conn.commit()
                return
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version = ?",
                (20260516,),
            )
            existing_version = await cur.fetchone()
            if existing_version is not None:
                if existing_version[0] != migration_name:
                    raise RuntimeError(
                        "schema_version collision for bl_tg_alert_eligible_v1: "
                        f"version=20260516 description={existing_version[0]}"
                    )
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    (migration_name, now_iso),
                )
                await conn.commit()
                _log.info(
                    "bl_tg_alert_eligible_v1_sentinel_backfilled",
                    migration=migration_name,
                )
                return
            cur = await conn.execute("PRAGMA table_info(signal_params)")
            cols = {row[1] for row in await cur.fetchall()}
            if "tg_alert_eligible" not in cols:
                await conn.execute(
                    "ALTER TABLE signal_params ADD COLUMN "
                    "tg_alert_eligible INTEGER NOT NULL DEFAULT 0"
                )
            for sig in DEFAULT_ALLOW:
                await conn.execute(
                    "UPDATE signal_params SET tg_alert_eligible=1 "
                    "WHERE signal_type = ?",
                    (sig,),
                )
            await conn.execute("""CREATE TABLE IF NOT EXISTS tg_alert_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_trade_id INTEGER REFERENCES paper_trades(id) ON DELETE SET NULL,
                    signal_type TEXT NOT NULL,
                    token_id    TEXT NOT NULL,
                    alerted_at  TEXT NOT NULL,
                    outcome     TEXT NOT NULL CHECK (outcome IN (
                        'sent','blocked_eligibility',
                        'blocked_cooldown','dispatch_failed',
                        'announcement_sent'
                    )),
                    detail      TEXT
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_alert_log_token "
                "ON tg_alert_log(token_id, alerted_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260516, now_iso, migration_name),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.commit()
        except Exception as e:
            _log.exception(
                "schema_migration_failed",
                migration="bl_tg_alert_eligible_v1",
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_tg_alert_eligible_v1")
            raise

        # Per-signal post-assertion (R1-C2 fold)
        for sig in DEFAULT_ALLOW:
            cur = await conn.execute(
                "SELECT tg_alert_eligible FROM signal_params " "WHERE signal_type = ?",
                (sig,),
            )
            row = await cur.fetchone()
            if not (row and row[0] == 1):
                raise RuntimeError(
                    f"bl_tg_alert_eligible_v1 post-assert: {sig!r} not eligible"
                )

    async def _migrate_tg_alert_log_m1_5c_outcome(self) -> None:
        """BL-NEW-M1.5C (R1-C1 design fold): extend tg_alert_log.outcome
        CHECK constraint to admit 'm1_5c_announcement_sent' sentinel.

        Schema version 20260517. Mirrors _migrate_reject_reason_extend_v2
        table-rename pattern. Idempotent via paper_migrations sentinel +
        sqlite_master.sql substring guard.
        """
        import re as _re
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                       name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")

            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_tg_alert_log_m1_5c_outcome",),
            )
            if (await cur.fetchone()) is not None:
                await conn.commit()
                return

            cur = await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                ("tg_alert_log",),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.execute("ROLLBACK")
                raise RuntimeError(
                    "bl_tg_alert_log_m1_5c_outcome: tg_alert_log missing; "
                    "migration ordering bug"
                )
            table_sql = row[0] or ""

            # Idempotency: skip if CHECK already includes the new value.
            if "m1_5c_announcement_sent" in table_sql:
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    ("bl_tg_alert_log_m1_5c_outcome", now_iso),
                )
                await conn.commit()
                return

            cur = await conn.execute("PRAGMA table_info(tg_alert_log)")
            cols = await cur.fetchall()
            col_names = [c[1] for c in cols]
            col_list = ", ".join(col_names)

            new_check = (
                "CHECK (outcome IN ("
                "'sent','blocked_eligibility',"
                "'blocked_cooldown','dispatch_failed',"
                "'announcement_sent','m1_5c_announcement_sent'"
                "))"
            )

            pattern = _re.compile(
                r"outcome\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*outcome\s+IN\s*\([^)]*\)\s*\)",
                _re.IGNORECASE | _re.DOTALL,
            )
            new_table_sql = pattern.sub(
                f"outcome     TEXT NOT NULL {new_check}", table_sql
            )
            if new_table_sql == table_sql:
                _log.warning(
                    "tg_alert_log_m1_5c_check_pattern_miss",
                    sql_excerpt=table_sql[:200],
                )
                await conn.execute("ROLLBACK")
                return

            # Quoted-identifier hotfix regex (M1.5a precedent db.py:2932-2937).
            new_table_sql_renamed = _re.sub(
                r'TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?tg_alert_log["`]?\s*\(',
                "TABLE tg_alert_log_new (",
                new_table_sql,
                count=1,
                flags=_re.IGNORECASE,
            )
            if new_table_sql_renamed == new_table_sql:
                _log.warning(
                    "tg_alert_log_m1_5c_rename_pattern_miss",
                    sql_excerpt=new_table_sql[:200],
                )
                await conn.execute("ROLLBACK")
                return

            # No views depend on tg_alert_log (verified) — skip view-drop.
            await conn.execute(new_table_sql_renamed)
            await conn.execute(
                f"INSERT INTO tg_alert_log_new ({col_list}) "
                f"SELECT {col_list} FROM tg_alert_log"
            )
            await conn.execute("DROP TABLE tg_alert_log")
            await conn.execute("ALTER TABLE tg_alert_log_new RENAME TO tg_alert_log")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_alert_log_token "
                "ON tg_alert_log(token_id, alerted_at)"
            )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_tg_alert_log_m1_5c_outcome", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260517, now_iso, "bl_tg_alert_log_m1_5c_outcome"),
            )

            cur = await conn.execute(
                "SELECT 1 FROM tg_alert_log WHERE outcome='announcement_sent' LIMIT 1"
            )
            m1_5b_present = (await cur.fetchone()) is not None

            await conn.commit()
            _log.info(
                "tg_alert_log_m1_5c_outcome_migration_complete",
                m1_5b_sentinel_preserved=m1_5b_present,
            )
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_tg_alert_log_dedup_outcome(self) -> None:
        """BL-NEW-TG-ALERT-NOISE-DEDUP: extend tg_alert_log.outcome CHECK
        to admit the 'blocked_dedup_24h' value (strict 24h per-token dedup).

        Schema version 20260530. Mirrors _migrate_tg_alert_log_m1_5c_outcome
        table-rebuild pattern exactly. Idempotent via paper_migrations
        sentinel + sqlite_master.sql substring guard, both UNIQUE to this
        migration (distinct from the m1_5c sentinel/guard). MUST run AFTER
        the m1_5c widening so it preserves m1_5c_announcement_sent.

        CRITICAL: the rebuilt CHECK preserves ALL existing values
        (sent, blocked_eligibility, blocked_cooldown, dispatch_failed,
        announcement_sent, m1_5c_announcement_sent) PLUS blocked_dedup_24h.
        (Codex CRITICAL: a prior draft dropped m1_5c_announcement_sent.)
        The index is recreated INSIDE this migration (the rebuild drops the
        old table) per memory feedback_ddl_before_alter.
        """
        import re as _re
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                       name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")

            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_tg_alert_log_dedup_outcome",),
            )
            if (await cur.fetchone()) is not None:
                await conn.commit()
                return

            cur = await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                ("tg_alert_log",),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.execute("ROLLBACK")
                raise RuntimeError(
                    "bl_tg_alert_log_dedup_outcome: tg_alert_log missing; "
                    "migration ordering bug"
                )
            table_sql = row[0] or ""

            # Idempotency guard UNIQUE to this migration: skip if the CHECK
            # already admits 'blocked_dedup_24h'.
            if "blocked_dedup_24h" in table_sql:
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                    "VALUES (?, ?)",
                    ("bl_tg_alert_log_dedup_outcome", now_iso),
                )
                await conn.commit()
                return

            cur = await conn.execute("PRAGMA table_info(tg_alert_log)")
            cols = await cur.fetchall()
            col_names = [c[1] for c in cols]
            col_list = ", ".join(col_names)

            # Preserve ALL existing values + add blocked_dedup_24h. The
            # m1_5c migration already added m1_5c_announcement_sent; it MUST
            # survive this rebuild (Codex CRITICAL: prior draft dropped it).
            new_check = (
                "CHECK (outcome IN ("
                "'sent','blocked_eligibility',"
                "'blocked_cooldown','dispatch_failed',"
                "'announcement_sent','m1_5c_announcement_sent',"
                "'blocked_dedup_24h'"
                "))"
            )

            pattern = _re.compile(
                r"outcome\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*outcome\s+IN\s*\([^)]*\)\s*\)",
                _re.IGNORECASE | _re.DOTALL,
            )
            new_table_sql = pattern.sub(
                f"outcome     TEXT NOT NULL {new_check}", table_sql
            )
            if new_table_sql == table_sql:
                _log.warning(
                    "tg_alert_log_dedup_check_pattern_miss",
                    sql_excerpt=table_sql[:200],
                )
                await conn.execute("ROLLBACK")
                return

            # Quoted-identifier hotfix regex (M1.5a precedent).
            new_table_sql_renamed = _re.sub(
                r'TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?tg_alert_log["`]?\s*\(',
                "TABLE tg_alert_log_new (",
                new_table_sql,
                count=1,
                flags=_re.IGNORECASE,
            )
            if new_table_sql_renamed == new_table_sql:
                _log.warning(
                    "tg_alert_log_dedup_rename_pattern_miss",
                    sql_excerpt=new_table_sql[:200],
                )
                await conn.execute("ROLLBACK")
                return

            # No views depend on tg_alert_log (verified at m1_5c) — skip view-drop.
            await conn.execute(new_table_sql_renamed)
            await conn.execute(
                f"INSERT INTO tg_alert_log_new ({col_list}) "
                f"SELECT {col_list} FROM tg_alert_log"
            )
            await conn.execute("DROP TABLE tg_alert_log")
            await conn.execute("ALTER TABLE tg_alert_log_new RENAME TO tg_alert_log")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_alert_log_token "
                "ON tg_alert_log(token_id, alerted_at)"
            )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_tg_alert_log_dedup_outcome", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260530, now_iso, "bl_tg_alert_log_dedup_outcome"),
            )

            # Preserve the m1_5c sentinel value across this rebuild (audit).
            cur = await conn.execute(
                "SELECT 1 FROM tg_alert_log "
                "WHERE outcome='m1_5c_announcement_sent' LIMIT 1"
            )
            m1_5c_present = (await cur.fetchone()) is not None

            await conn.commit()
            _log.info(
                "tg_alert_log_dedup_outcome_migration_complete",
                m1_5c_sentinel_preserved=m1_5c_present,
            )
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_tg_alert_operator_actions_v1(self) -> None:
        """BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY: operator labels.

        Additive table keyed one-to-one by tg_alert_log.id. The table stores
        copied alert facts so later analysis does not have to infer context
        through joins if the source dispatch row changes.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "bl_tg_alert_operator_actions_v1"
        now_iso = datetime.now(timezone.utc).isoformat()

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                await self._assert_tg_alert_operator_actions_schema(conn)
                return

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )""")
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version = ?",
                (20260531,),
            )
            existing_version = await cur.fetchone()
            if existing_version is not None and existing_version[0] != migration_name:
                raise RuntimeError(
                    "tg_alert_operator_actions_v1 schema_version collision: "
                    f"version 20260531 owned by {existing_version[0]!r}"
                )

            await conn.execute("""CREATE TABLE IF NOT EXISTS tg_alert_operator_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_alert_log_id INTEGER NOT NULL UNIQUE,
                    paper_trade_id INTEGER,
                    token_id TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    alerted_at TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (
                        action IN ('acted','useful','ignored','false_positive')
                    ),
                    note TEXT,
                    source TEXT NOT NULL DEFAULT 'dashboard'
                        CHECK (source IN ('dashboard','api','backfill')),
                    marked_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_tg_alert_operator_actions_marked_at "
                "ON tg_alert_operator_actions(marked_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_tg_alert_operator_actions_action "
                "ON tg_alert_operator_actions(action, marked_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260531, now_iso, migration_name),
            )
            await self._assert_tg_alert_operator_actions_schema(conn)
            await conn.commit()
            _db_log.info(
                "tg_alert_operator_actions_v1_migration_complete",
                table="tg_alert_operator_actions",
            )
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _assert_tg_alert_operator_actions_schema(self, conn) -> None:
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='tg_alert_operator_actions'"
        )
        if await cur.fetchone() is None:
            raise RuntimeError("tg_alert_operator_actions table missing")

        cur = await conn.execute("PRAGMA table_info(tg_alert_operator_actions)")
        cols = {row[1] for row in await cur.fetchall()}
        required = {
            "id",
            "tg_alert_log_id",
            "paper_trade_id",
            "token_id",
            "signal_type",
            "alerted_at",
            "action",
            "note",
            "source",
            "marked_at",
            "updated_at",
        }
        missing = sorted(required - cols)
        if missing:
            raise RuntimeError(
                "tg_alert_operator_actions schema missing columns: " + ",".join(missing)
            )

        for idx in (
            "idx_tg_alert_operator_actions_marked_at",
            "idx_tg_alert_operator_actions_action",
        ):
            cur = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (idx,),
            )
            if await cur.fetchone() is None:
                raise RuntimeError(f"tg_alert_operator_actions index missing: {idx}")
        cur = await conn.execute(
            "SELECT 1 FROM schema_version "
            "WHERE version = 20260531 "
            "AND description = 'bl_tg_alert_operator_actions_v1'"
        )
        if await cur.fetchone() is None:
            raise RuntimeError("tg_alert_operator_actions schema_version missing")

    async def _migrate_narrative_scanner_v1(self) -> None:
        """BL-NEW-NARRATIVE-SCANNER (V1): create narrative_alerts_inbound table.

        Receives events emitted by the Hermes-based crypto narrative scanner
        on main-vps via HMAC-authed HTTPS. Append-only; one row per
        Hermes-computed event_id. See tasks/design_crypto_narrative_scanner.md
        for full design + idempotency semantics + per-column rationale.

        Schema version 20260518. Idempotent via paper_migrations sentinel.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")

            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_narrative_scanner_v1",),
            )
            if (await cur.fetchone()) is not None:
                # V2-PR-review C-OG1 fold: emit log on idempotent skip so
                # journalctl can distinguish "ran before" from "never ran".
                _log.info(
                    "narrative_scanner_v1_migration_skip_already_applied",
                    table="narrative_alerts_inbound",
                )
                await conn.commit()
                return

            await conn.execute("""CREATE TABLE IF NOT EXISTS narrative_alerts_inbound (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    tweet_id TEXT NOT NULL,
                    tweet_author TEXT NOT NULL,
                    tweet_ts TEXT NOT NULL,
                    tweet_text TEXT NOT NULL,
                    tweet_text_hash TEXT NOT NULL,
                    extracted_cashtag TEXT,
                    extracted_ca TEXT,
                    extracted_chain TEXT,
                    resolved_coin_id TEXT,
                    narrative_theme TEXT,
                    urgency_signal TEXT,
                    classifier_confidence REAL,
                    classifier_version TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT (datetime('now'))
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_narrative_inbound_received "
                "ON narrative_alerts_inbound(received_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_narrative_inbound_resolved "
                "ON narrative_alerts_inbound(resolved_coin_id) "
                "WHERE resolved_coin_id IS NOT NULL"
            )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                ("bl_narrative_scanner_v1", now_iso),
            )
            await conn.commit()
            _log.info(
                "narrative_scanner_v1_migration_complete",
                action="created",
                table="narrative_alerts_inbound",
            )
        except Exception as exc:
            # V2-PR-review C-OG2 fold: log rollback so journalctl attributes
            # the failure to this migration even if upstream re-raise is noisy.
            _log.error(
                "narrative_scanner_v1_migration_rollback",
                err=str(exc),
                err_type=type(exc).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_minara_alert_emissions_v1(self) -> None:
        """BL-NEW-MINARA-DB-PERSISTENCE: durable Minara emit telemetry."""
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()
        migration_name = "bl_minara_alert_emissions_v1"

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )""")

            await conn.execute("""CREATE TABLE IF NOT EXISTS minara_alert_emissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_trade_id INTEGER REFERENCES paper_trades(id) ON DELETE RESTRICT,
                    tg_alert_log_id INTEGER,
                    signal_type TEXT NOT NULL,
                    coin_id TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    command_text TEXT,
                    command_hash TEXT,
                    command_text_observed INTEGER NOT NULL DEFAULT 0
                        CHECK (command_text_observed IN (0,1)),
                    source TEXT NOT NULL
                        CHECK (source IN ('live','journalctl_backfill')),
                    source_event_id TEXT NOT NULL UNIQUE,
                    emitted_at TEXT NOT NULL,
                    operator_paste_acknowledged_at TEXT
                )""")
            await self._assert_minara_alert_emissions_schema(
                conn, require_indexes=False
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_minara_alert_emissions_emitted_at "
                "ON minara_alert_emissions(emitted_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_minara_alert_emissions_coin_id "
                "ON minara_alert_emissions(coin_id, emitted_at)"
            )
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_minara_alert_emissions_tg_alert_log_id "
                "ON minara_alert_emissions(tg_alert_log_id) "
                "WHERE tg_alert_log_id IS NOT NULL"
            )

            await self._assert_minara_alert_emissions_schema(conn)

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260519, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "minara_alert_emissions_v1_migration_complete",
                table="minara_alert_emissions",
            )
        except BaseException as e:
            _log.exception(
                "schema_migration_failed",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration=migration_name)
            raise

    async def _migrate_trade_adverse_excursion_v1(self) -> None:
        """Add ``trough_price`` + ``mae_pct`` to paper_trades.

        Maximum adverse excursion: the low-water mark since entry, the mirror of
        the existing ``peak_price`` / ``peak_pct`` high-water mark. Both nullable
        with NO DEFAULT -- absence means "never measured", which is NOT the same
        as "never dipped", and a DEFAULT 0 would silently assert the latter for
        every historical row.

        Anti-scope: NO backfill. A closed trade's low-water mark cannot be
        reconstructed -- `paper_trades` keeps no price path and
        `paper_trade_entry_snapshots` is entry-context only. Any MAE analysis
        must filter `mae_pct IS NOT NULL`, and must not read NULL as 0.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "bl_trade_adverse_excursion_v1"
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            expected_cols = {"trough_price": "REAL", "mae_pct": "REAL"}
            cur_pragma = await conn.execute("PRAGMA table_info(paper_trades)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing_cols:
                    _log.info(
                        "schema_migration_column_action",
                        migration=migration_name,
                        col=col,
                        action="skip_exists",
                    )
                    continue
                await conn.execute(
                    f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}"
                )
                _log.info(
                    "schema_migration_column_action",
                    migration=migration_name,
                    col=col,
                    action="added",
                )
            # Verify presence -- fail loud on drift rather than at first UPDATE.
            cur_pragma = await conn.execute("PRAGMA table_info(paper_trades)")
            post_cols = {row[1] for row in await cur_pragma.fetchall()}
            missing = sorted(set(expected_cols) - post_cols)
            if missing:
                raise RuntimeError(
                    f"{migration_name} schema missing columns: " + ", ".join(missing)
                )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260803, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "bl_trade_adverse_excursion_v1_migration_complete",
                table="paper_trades",
            )
        except BaseException as e:
            _log.exception(
                "bl_trade_adverse_excursion_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration=migration_name,
                )
            raise

    async def _migrate_pre_leg1_adverse_excursion_v1(self) -> None:
        """Add ``pre_leg1_trough_price`` + ``pre_leg1_mae_pct`` to paper_trades.

        The low-water mark restricted to the window in which the INITIAL
        stop-loss is eligible to fire -- i.e. before leg 1 arms the floor.
        ``mae_pct`` is a whole-life mark and therefore cannot answer stop-width
        questions on its own: it keeps accumulating after the SL has stopped
        being the active downside rule, so post-arm dips get misread as
        stop-outs a tighter stop would have caused.

        Both nullable with NO DEFAULT, same discipline as ``mae_pct``: NULL
        means "never measured" and MUST NOT be read as 0. Once leg 1 arms, the
        value freezes -- a frozen value is a measurement, not an absence.

        Anti-scope: NO backfill. The pre-arm price path of a closed trade is
        unrecoverable for exactly the same reason ``mae_pct`` was not
        backfilled. Any stop-width analysis must filter
        ``pre_leg1_mae_pct IS NOT NULL``.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "bl_pre_leg1_adverse_excursion_v1"
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            expected_cols = {
                "pre_leg1_trough_price": "REAL",
                "pre_leg1_mae_pct": "REAL",
            }
            cur_pragma = await conn.execute("PRAGMA table_info(paper_trades)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing_cols:
                    _log.info(
                        "schema_migration_column_action",
                        migration=migration_name,
                        col=col,
                        action="skip_exists",
                    )
                    continue
                await conn.execute(
                    f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}"
                )
                _log.info(
                    "schema_migration_column_action",
                    migration=migration_name,
                    col=col,
                    action="added",
                )
            # Verify presence -- fail loud on drift rather than at first UPDATE.
            cur_pragma = await conn.execute("PRAGMA table_info(paper_trades)")
            post_cols = {row[1] for row in await cur_pragma.fetchall()}
            missing = sorted(set(expected_cols) - post_cols)
            if missing:
                raise RuntimeError(
                    f"{migration_name} schema missing columns: " + ", ".join(missing)
                )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260808, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "bl_pre_leg1_adverse_excursion_v1_migration_complete",
                table="paper_trades",
            )
        except BaseException as e:
            _log.exception(
                "bl_pre_leg1_adverse_excursion_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration=migration_name,
                )
            raise

    async def _migrate_tg_act_shadow_v1(self) -> None:
        """TG shadow Stage A: durable decision inputs + counterfactual decisions.

        Three additive changes, one transaction:

        1. ``tg_social_signals.resolution_snapshot_json`` — nullable TEXT, the
           canonical-JSON snapshot of the resolver facts that were true when
           the signal was persisted. It is written in the SAME INSERT as the
           row itself, so the table stays INSERT-only. This column is the ONLY
           permitted recovery source for a historical decision input: catch-up
           after a disabled window and crash-restart both reproduce the
           original decision from it, and no external refetch may ever fill a
           gap (that would decide a past call on today's facts).

        2. ``tg_act_shadow`` — append-only, exactly one immutable decision per
           ``(signal_id, gate_version)``, enforced by the UNIQUE index rather
           than by writer discipline. Multiple rows per signal exist only
           across distinct gate_versions; that is the mechanism for comparing
           rule iterations, not an accident.

        3. ``tg_act_shadow_generations`` — one row per gate_version, written
           only on the first ENABLED startup with a real feature provider.
           ``activated_at`` is therefore the activation boundary, never
           install time: calls arriving between this dark deploy and operator
           activation are outside every generation, so first activation starts
           from zero eligible rows instead of a page storm.

        Indexes are created inside this step (DDL-order lesson), not deferred.

        Anti-scope: NO backfill of ``resolution_snapshot_json``. The resolver
        facts of an already-persisted signal are unrecoverable, and generation
        cutover guarantees the evaluated population post-dates the column.

        Tolerates an absent ``tg_social_signals``: that table belongs to
        :meth:`_migrate_feedback_loop_schema`, and a bootstrap in which that
        step was skipped must not have its whole ``initialize()`` aborted by
        this one. The column is added on the next startup instead (this
        migration re-checks the PRAGMA every run rather than short-circuiting
        on its ``paper_migrations`` marker).
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "tg_act_shadow_v1"
        schema_version = 20260812
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )

            # 1. Additive nullable column, PRAGMA-guarded. No early return on
            #    the paper_migrations marker: a partial state (column landed,
            #    marker insert didn't) must still converge on re-run.
            #
            #    `tg_social_signals` is created by `_migrate_feedback_loop_schema`,
            #    so on a bootstrap where that step was skipped or has not run
            #    yet the table is simply absent. Skip rather than abort the
            #    whole `initialize()`: because this migration re-checks the
            #    PRAGMA on every run instead of short-circuiting on its
            #    marker, the column lands on the next startup once the parent
            #    table exists. Converging late beats failing the bootstrap.
            cur_master = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='tg_social_signals'"
            )
            signals_table_exists = await cur_master.fetchone() is not None
            cur_pragma = await conn.execute("PRAGMA table_info(tg_social_signals)")
            signal_cols = {row[1] for row in await cur_pragma.fetchall()}
            if not signals_table_exists:
                _log.info(
                    "schema_migration_column_action",
                    migration=migration_name,
                    col="resolution_snapshot_json",
                    action="skip_no_table",
                )
            elif "resolution_snapshot_json" in signal_cols:
                _log.info(
                    "schema_migration_column_action",
                    migration=migration_name,
                    col="resolution_snapshot_json",
                    action="skip_exists",
                )
            else:
                await conn.execute(
                    "ALTER TABLE tg_social_signals "
                    "ADD COLUMN resolution_snapshot_json TEXT"
                )
                _log.info(
                    "schema_migration_column_action",
                    migration=migration_name,
                    col="resolution_snapshot_json",
                    action="added",
                )

            # 2. Decision table. `actionable` is stored as INTEGER 0/1.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_act_shadow (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id      INTEGER NOT NULL,
                    gate_version   TEXT NOT NULL,
                    actionable     INTEGER NOT NULL,
                    reason         TEXT NOT NULL,
                    features_json  TEXT NOT NULL,
                    created_at     TEXT NOT NULL,
                    UNIQUE(signal_id, gate_version),
                    FOREIGN KEY (signal_id) REFERENCES tg_social_signals(id)
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tg_act_shadow_gate_created "
                "ON tg_act_shadow(gate_version, created_at)"
            )

            # 3. Activation registry.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tg_act_shadow_generations (
                    gate_version  TEXT PRIMARY KEY,
                    activated_at  TEXT NOT NULL
                )
                """)

            # Verify presence -- fail loud on drift rather than at first write.
            # Only meaningful when the parent table exists; the skip_no_table
            # branch above has nothing to verify.
            cur_pragma = await conn.execute("PRAGMA table_info(tg_social_signals)")
            post_cols = {row[1] for row in await cur_pragma.fetchall()}
            if signals_table_exists and "resolution_snapshot_json" not in post_cols:
                raise RuntimeError(
                    f"{migration_name} schema missing column: "
                    "tg_social_signals.resolution_snapshot_json"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "tg_act_shadow_v1_migration_complete",
                table="tg_act_shadow",
            )
        except BaseException as e:
            _log.exception(
                "tg_act_shadow_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_alert_events_v1(self) -> None:
        """F3: `alert_events` — append-only control-plane event ledger.

        THE DEFECT. journald retention on prod collapses to minutes during the
        03:00 backup window, so any control-plane fact that exists only as a
        log line is unreconstructable after the fact. Worse, the four decisive
        suppression transitions (initial latch, re-latch, clear, parole re-arm)
        and the parole slot decrement emit no log event at all — they exist
        only as mutated `combo_performance` rows, which record the CURRENT
        state and nothing about how it got there. Suppression / retest / alert
        acceptance evidence therefore cannot be reconstructed.

        THE SHAPE. One append-only table, written by
        `scout.trading.alert_events.record_alert_event`. Rows are never updated
        and never pruned — deliberate: they are rare (a handful per refresh) and
        tiny, and a retention policy on an audit ledger reintroduces exactly the
        "the evidence aged out" failure this closes. `event_type` is a CHECK
        constraint rather than writer discipline, so a typo'd event lands as a
        loud IntegrityError instead of an unqueryable row.

        NO BACKFILL. The transitions that already happened left no record to
        recover from; the ledger starts at the deploy boundary and every row in
        it is a real observation.

        Indexes ship inside this step (DDL-order lesson), not deferred: the two
        query axes are per-combo history and per-event-type freshness (the §12a
        watchdog reads the newest `refresh_completed`).
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "alert_events_v1"
        schema_version = 20260814
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )

            # UPGRADE SAFETY. `CREATE TABLE IF NOT EXISTS` is a no-op against a
            # table that already exists with an OLDER CHECK vocabulary, and the
            # `ledger_installed` seed below then violates that stale constraint
            # — the migration re-raises and `initialize()` cannot boot. Verified
            # by reproduction against an intermediate-F3-shape DB.
            #
            # So the vocabulary is not merely additive-by-hope: on drift the
            # table is REBUILT. Keeping the CHECK is what makes a typo'd
            # event_type fail loudly, and this rebuild is what keeps the CHECK
            # evolvable — without it, the next event type added to this ledger
            # bricks every database that predates it.
            cur = await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='alert_events'"
            )
            row = await cur.fetchone()
            live_ddl = row[0] if row else None
            wanted = _alert_event_check_vocabulary(_ALERT_EVENTS_DDL)
            if (
                live_ddl is not None
                and _alert_event_check_vocabulary(live_ddl) != wanted
            ):
                found = _alert_event_check_vocabulary(live_ddl)
                # Rows whose event_type is no longer in the vocabulary cannot be
                # carried across — the new CHECK would reject them. Counted and
                # logged rather than silently dropped: today this is zero
                # everywhere (the vocabulary has only ever grown), and if it is
                # ever non-zero the operator needs to know what the rebuild ate.
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM alert_events WHERE event_type NOT IN "
                    "(" + ",".join("?" * len(wanted)) + ")",
                    tuple(sorted(wanted)),
                )
                (dropped,) = await cur.fetchone()
                _log.warning(
                    "alert_events_vocabulary_rebuild",
                    migration=migration_name,
                    added=sorted(wanted - found),
                    removed=sorted(found - wanted),
                    rows_dropped=dropped,
                    detail="rebuilding alert_events to carry the current "
                    "event_type CHECK vocabulary",
                )
                await conn.execute(
                    _ALERT_EVENTS_DDL.replace(
                        "CREATE TABLE alert_events", "CREATE TABLE alert_events_new"
                    )
                )
                await conn.execute(
                    f"INSERT INTO alert_events_new ({_ALERT_EVENTS_COLUMNS}) "
                    f"SELECT {_ALERT_EVENTS_COLUMNS} FROM alert_events "
                    "WHERE event_type IN (" + ",".join("?" * len(wanted)) + ")",
                    tuple(sorted(wanted)),
                )
                # DROP before RENAME: the old table's indexes go with it, and
                # the recreate below re-attaches them to the new table under the
                # same names.
                await conn.execute("DROP TABLE alert_events")
                await conn.execute(
                    "ALTER TABLE alert_events_new RENAME TO alert_events"
                )

            await conn.execute(
                _ALERT_EVENTS_DDL.replace(
                    "CREATE TABLE alert_events",
                    "CREATE TABLE IF NOT EXISTS alert_events",
                )
            )
            for index_sql in _ALERT_EVENTS_INDEXES:
                await conn.execute(index_sql)

            # Verify presence -- fail loud on drift rather than at first write.
            cur = await conn.execute("PRAGMA table_info(alert_events)")
            post_cols = {row[1] for row in await cur.fetchall()}
            required = {
                "id",
                "created_at",
                "event_type",
                "combo_key",
                "signal_type",
                "alert_source",
                "transition",
                "detected_at",
                "delivery_result",
                "retry",
                "payload_hash",
                "state_json",
                "detail",
            }
            missing = required - post_cols
            if missing:
                raise RuntimeError(
                    f"{migration_name} schema missing columns: {sorted(missing)}"
                )

            # Seed exactly ONE `ledger_installed` row, in this same
            # transaction. It is the ledger's own epoch marker and it exists to
            # keep the §12a watchdog TRUTHFUL across the deploy boundary.
            #
            # The watchdog is enabled unconditionally by the cron line
            # (`cron/gecko-alpha.crontab`: ALERT_CHANNEL_WATCHDOG_ENABLED=true,
            # hourly at :50), and an empty ledger is a breach. Without this row,
            # the first watchdog run after deploy would page "NO
            # 'refresh_completed' rows" — technically true and operationally
            # false, because the nightly refresh simply has not come around yet.
            # A page the operator learns to dismiss is worse than no page.
            #
            # It is a TRUE statement, not a silencer: `created_at` is the
            # migration time, so the watchdog ages from it and still pages
            # `no_successful_refresh_since_install` once the SLO elapses with no
            # successful refresh. The deploy grace period is exactly the SLO,
            # and a writer that never runs is still caught.
            #
            # Guarded on absence rather than INSERT OR IGNORE: there is no
            # unique key to conflict on, so a re-run would otherwise append a
            # second epoch and move the fallback age forward on every startup.
            await conn.execute(
                "INSERT INTO alert_events (created_at, event_type, detail) "
                "SELECT ?, 'ledger_installed', ? WHERE NOT EXISTS "
                "(SELECT 1 FROM alert_events WHERE event_type = 'ledger_installed')",
                (now_iso, f"{migration_name} applied; ledger epoch"),
            )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info("alert_events_v1_migration_complete", table="alert_events")
        except BaseException as e:
            _log.exception(
                "alert_events_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_alert_events_dedup_index_v1(self) -> None:
        """`idx_alert_events_dedup(event_type, payload_hash)` — the index behind
        the `parole_denied` first-occurrence probe.

        THE DEFECT IT SERVES. `parole_denied` was written per DENIAL ATTEMPT.
        A latched combo re-enters the `parole_exhausted` branch on every dispatch
        attempt, so prod appended 20,094 byte-identical rows in 17h across three
        combos. The writer now appends only the first occurrence of each distinct
        denial state, which means an existence probe on the dispatch hot path
        against a ledger that is never pruned — unindexed, that is a full scan of
        a table whose whole problem is unbounded growth.

        WHY THIS STEP DOES NOT CREATE IT. The DDL lives in exactly one place,
        `_ALERT_EVENTS_DEDUP_INDEX` inside `_ALERT_EVENTS_INDEXES`, because that
        tuple is what the vocabulary-drift rebuild re-attaches after it DROPs the
        table — an index created only here would silently vanish the next time
        the event vocabulary grows. A second `CREATE` here would make the tuple
        membership look optional and hide that. This step records the version and
        FAILS LOUD if the index is absent, so the single source is verified at
        boot rather than discovered as a slow scan in prod.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "alert_events_dedup_index_v1"
        schema_version = 20260816
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            cur = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='idx_alert_events_dedup'"
            )
            if await cur.fetchone() is None:
                raise RuntimeError(
                    f"{migration_name}: idx_alert_events_dedup absent after "
                    "alert_events_v1 — the parole_denied dedup probe would "
                    "full-scan a ledger that is never pruned"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "alert_events_dedup_index_v1_migration_complete",
                index="idx_alert_events_dedup",
            )
        except BaseException as e:
            _log.exception(
                "alert_events_dedup_index_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_alert_payloads_v1(self) -> None:
        """`alert_payloads` — the exact bodies behind `alert_events.payload_hash`,
        schema_version 20260817. See `_ALERT_PAYLOADS_DDL` for the shape and the
        BLOB-over-TEXT justification.

        THE GAP. The F3 ledger preserves `payload_hash = sha256(exact body)`.
        That proves equality against a body you already hold and reconstructs
        nothing on its own, and for the terminal-incomplete / `parole_stalled`
        lane the delivered body survived nowhere else — journald never logged it
        and drops what it did log inside ~59h. Integrity without reconstruction.

        NO BACKFILL, and none is possible: the bodies behind digests already in
        `alert_events` do not exist anywhere to recover from. Rows written before
        this step keep a resolvable-to-nothing digest, which is the honest
        pre-cutover state rather than a gap to fill with a re-rendered guess —
        a re-render is a different string and would hash differently anyway.

        Additive and independent of the `alert_events` vocabulary rebuild: this
        is a separate table, so the rebuild in `_migrate_alert_events_v1` (which
        DROPs and recreates `alert_events`) neither touches nor loses it.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "alert_payloads_v1"
        schema_version = 20260817
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            await conn.execute(_ALERT_PAYLOADS_DDL)

            # Verify presence — fail loud on drift rather than at first write.
            # A missing column here means every preimage write would be
            # swallowed by the writer's fail-soft handler, i.e. the substrate
            # would be silently absent rather than loudly broken.
            cur = await conn.execute("PRAGMA table_info(alert_payloads)")
            info = await cur.fetchall()
            post_cols = {row[1] for row in info}
            required = {"payload_hash", "payload", "byte_length", "first_seen_at"}
            missing = required - post_cols
            if missing:
                raise RuntimeError(
                    f"{migration_name} schema missing columns: {sorted(missing)}"
                )
            # The primary key IS the dedup mechanism — `INSERT OR IGNORE` on a
            # table without it would append a body per referencing event row and
            # silently defeat the whole content-addressed design.
            if not any(row[1] == "payload_hash" and row[5] for row in info):
                raise RuntimeError(
                    f"{migration_name}: alert_payloads.payload_hash is not the "
                    "primary key — the content-addressed dedup would not hold"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info("alert_payloads_v1_migration_complete", table="alert_payloads")
        except BaseException as e:
            _log.exception(
                "alert_payloads_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_signal_events_indexes_v1(self) -> None:
        """Drop idx_sig_events_type, add idx_sig_events_created_at.

        Option D from retention rulings 2026-08-16: idx_sig_events_type (466 MB)
        serves NO WHERE clause anywhere; load_recent_events does a full-scan +
        temp-sort every cycle because it filters on created_at. Swapping them
        gives both size and latency wins.

        Migration runs EXCLUSIVE to gate the index operations. Both indexes exist
        by schema definition, but the DROP happens only once; subsequent runs
        check paper_migrations and skip.

        The new index helps:
        - load_recent_events: WHERE created_at >= ? ORDER BY created_at ASC
        - tracker MIN queries: WHERE ... AND datetime(created_at) < ...
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "signal_events_indexes_v1"
        schema_version = 20260822
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )

            # Check if already applied
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration_name,)
            )
            if await cur.fetchone() is not None:
                await conn.execute("COMMIT")
                _log.info(
                    "signal_events_indexes_v1_migration_skip_already_applied",
                )
                return

            # Drop the old index (serves no WHERE clause)
            await conn.execute("DROP INDEX IF EXISTS idx_sig_events_type")

            # Create the new index (helps with created_at filters)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sig_events_created_at "
                "ON signal_events(created_at)"
            )

            # Record the migration
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "signal_events_indexes_v1_migration_complete",
                dropped="idx_sig_events_type",
                created="idx_sig_events_created_at",
            )
        except BaseException as e:
            _log.exception(
                "signal_events_indexes_v1_migration_failed",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_entry_snapshot_liquidity_provenance_v1(self) -> None:
        """Add liquidity provenance columns to paper_trade_entry_snapshots:
        ``liquidity_source_at_entry`` + ``liquidity_confidence_at_entry``.

        Kept SEPARATE from ``_migrate_actionability_entry_snapshot_v1`` (whose
        early-return path runs a strict full-schema assert): a fresh,
        idempotent ALTER migration means prod DBs that already ran v1 add the
        2 columns here without tripping the v1 assert. Mirrors the column-add
        pattern of ``_migrate_liquidity_enrichment_v1``. Both columns nullable
        with NO DEFAULT (absence-vs-empty semantics).

        Anti-scope: NO change to the v1 snapshot table assert, NO backfill of
        existing snapshot rows (the source value wasn't recorded at the time).
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "bl_entry_snapshot_liquidity_provenance_v1"
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            expected_cols = {
                "liquidity_source_at_entry": "TEXT",
                "liquidity_confidence_at_entry": "TEXT",
            }
            cur_pragma = await conn.execute(
                "PRAGMA table_info(paper_trade_entry_snapshots)"
            )
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            for col, coltype in expected_cols.items():
                if col in existing_cols:
                    _log.info(
                        "schema_migration_column_action",
                        migration=migration_name,
                        col=col,
                        action="skip_exists",
                    )
                    continue
                await conn.execute(
                    f"ALTER TABLE paper_trade_entry_snapshots "
                    f"ADD COLUMN {col} {coltype}"
                )
                _log.info(
                    "schema_migration_column_action",
                    migration=migration_name,
                    col=col,
                    action="added",
                )
            # Verify presence — fail loud on drift rather than at first INSERT.
            cur_pragma = await conn.execute(
                "PRAGMA table_info(paper_trade_entry_snapshots)"
            )
            post_cols = {row[1] for row in await cur_pragma.fetchall()}
            missing = sorted(set(expected_cols) - post_cols)
            if missing:
                raise RuntimeError(
                    f"{migration_name} schema missing columns: " + ", ".join(missing)
                )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260623, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "bl_entry_snapshot_liquidity_provenance_v1_migration_complete",
                table="paper_trade_entry_snapshots",
            )
        except BaseException as e:
            _log.exception(
                "bl_entry_snapshot_liquidity_provenance_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                _log.exception(
                    "schema_migration_rollback_failed",
                    migration=migration_name,
                )
            raise

    async def _migrate_actionability_entry_snapshot_v1(self) -> None:
        """BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION: durable at-entry
        snapshot sidecar for paper_trades. See PR #199 design doc and the
        review-fold log. Pattern mirrors _migrate_minara_alert_emissions_v1."""
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "bl_actionability_entry_snapshot_v1"

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                await self._assert_paper_trade_entry_snapshots_schema(conn)
                _log.info(
                    "bl_actionability_entry_snapshot_v1_migration_skip_already_applied"
                )
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trade_entry_snapshots (
                    paper_trade_id INTEGER PRIMARY KEY,
                    entry_snapshot_version TEXT NOT NULL,
                    entry_snapshot_complete INTEGER NOT NULL
                        CHECK (entry_snapshot_complete IN (0, 1)),
                    entry_snapshot_missing_fields TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    signal_type TEXT,
                    mcap_usd_at_entry REAL,
                    mcap_bucket_at_entry TEXT,
                    liquidity_usd_at_entry REAL,
                    token_age_days_at_entry REAL,
                    first_seen_at_at_entry TEXT,
                    detected_by_combo_at_entry TEXT,
                    source_confluence_count_at_entry INTEGER,
                    tg_channel_at_entry TEXT,
                    actionability_version_at_entry TEXT,
                    actionability_reason_at_entry TEXT,
                    actionable_at_entry INTEGER,
                    tp_pct_at_entry REAL,
                    sl_pct_at_entry REAL,
                    trail_pct_at_entry REAL,
                    trail_pct_low_peak_at_entry REAL,
                    FOREIGN KEY (paper_trade_id)
                        REFERENCES paper_trades(id) ON DELETE RESTRICT
                )
                """)
            await self._assert_paper_trade_entry_snapshots_schema(
                conn, require_indexes=False
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ptes_version "
                "ON paper_trade_entry_snapshots(entry_snapshot_version)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ptes_complete "
                "ON paper_trade_entry_snapshots(entry_snapshot_complete)"
            )
            await self._assert_paper_trade_entry_snapshots_schema(conn)
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
                (migration_name, now_iso),
            )
            # 20260521 chosen to avoid collision with bl_chain_pattern_provenance_v1
            # (already on master at 20260520). Both shipped 2026-05-20; the
            # schema_version PRIMARY KEY is per-row unique. On prod (srilu-vps),
            # the actionability sentinel was already written with 20260520 on
            # initial deploy (2026-05-20T02:02Z); the sentinel-pre-check at the
            # top of this migration returns early on subsequent initialize()s
            # so the prod schema_version row stays as 20260520=actionability —
            # a cosmetic mismatch documented in the PR. Fresh-DB tests pass.
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260521, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "bl_actionability_entry_snapshot_v1_migration_complete",
                table="paper_trade_entry_snapshots",
            )
        except BaseException as e:
            _log.exception(
                "bl_actionability_entry_snapshot_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration=migration_name)
            raise

    async def _migrate_source_calls_v1(self) -> None:
        """BL-NEW-SOURCE-CALL-OUTCOME-LEDGER: durable TG/X call ledger."""
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "bl_source_calls_v1"
        schema_version = 20260522

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("bl_source_calls_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            cur = await conn.execute("SELECT json_valid('[]'), json_type('[]')")
            json_probe = await cur.fetchone()
            if json_probe is None or json_probe[0] != 1 or json_probe[1] != "array":
                raise RuntimeError("SQLite JSON1 support required for source_calls")

            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if existing_version is not None:
                if existing_version["description"] != migration_name:
                    raise RuntimeError(
                        "schema_version collision for bl_source_calls_v1: "
                        f"version={schema_version} "
                        f"description={existing_version['description']}"
                    )
                cur = await conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='source_calls'"
                )
                if await cur.fetchone():
                    await conn.execute(
                        "INSERT OR IGNORE INTO paper_migrations "
                        "(name, cutover_ts) VALUES (?, ?)",
                        (migration_name, now_iso),
                    )
                    await conn.commit()
                    _log.info(
                        "bl_source_calls_v1_sentinel_backfilled",
                        migration=migration_name,
                    )
                    return

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS source_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL CHECK (source_type IN ('tg', 'x')),
                    source_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    token_id TEXT,
                    symbol TEXT,
                    contract_address TEXT,
                    chain TEXT,
                    call_ts TEXT NOT NULL,
                    observed_at TEXT,
                    ingest_delay_sec INTEGER,
                    call_kind TEXT NOT NULL CHECK (
                        call_kind IN ('first_mention','repeat_mention','ca_call',
                                      'cashtag_only','unknown')
                    ),
                    cluster_identity TEXT NOT NULL,
                    cluster_identity_kind TEXT NOT NULL CHECK (
                        cluster_identity_kind IN ('token_id','contract','symbol',
                                                  'source_event')
                    ),
                    duplicate_cluster_key TEXT NOT NULL,
                    duplicate_rank_in_cluster INTEGER NOT NULL DEFAULT 1,
                    resolved_state TEXT NOT NULL,
                    price_at_call REAL,
                    price_at_call_snapshot_at TEXT,
                    price_source TEXT,
                    price_age_sec INTEGER,
                    forward_30m_snapshot_at TEXT,
                    forward_30m_observed_horizon_sec INTEGER,
                    forward_1h_snapshot_at TEXT,
                    forward_1h_observed_horizon_sec INTEGER,
                    forward_6h_snapshot_at TEXT,
                    forward_6h_observed_horizon_sec INTEGER,
                    forward_24h_snapshot_at TEXT,
                    forward_24h_observed_horizon_sec INTEGER,
                    mcap_at_call REAL,
                    forward_30m_pct REAL,
                    forward_1h_pct REAL,
                    forward_6h_pct REAL,
                    forward_24h_pct REAL,
                    max_favorable_pct_24h REAL,
                    max_adverse_pct_24h REAL,
                    time_to_peak_min REAL,
                    linked_paper_trade_id INTEGER,
                    linkage_candidate_count INTEGER NOT NULL DEFAULT 0,
                    linkage_conflict_count INTEGER NOT NULL DEFAULT 0,
                    linkage_method TEXT NOT NULL DEFAULT 'none'
                        CHECK (linkage_method IN ('none','direct_tg','heuristic_x')),
                    linkage_confidence TEXT NOT NULL DEFAULT 'none'
                        CHECK (linkage_confidence IN ('none','direct','heuristic',
                                                      'conflict')),
                    outcome_status TEXT NOT NULL
                        CHECK (outcome_status IN ('pending','partial','complete',
                                                  'unresolvable')),
                    missing_fields TEXT NOT NULL
                        CHECK (json_valid(missing_fields)
                               AND json_type(missing_fields) = 'array'),
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (linked_paper_trade_id)
                        REFERENCES paper_trades(id) ON DELETE RESTRICT,
                    UNIQUE (source_type, source_event_id)
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_calls_source_ts "
                "ON source_calls(source_type, source_id, call_ts)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_calls_token_ts "
                "ON source_calls(token_id, call_ts)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_calls_cluster "
                "ON source_calls(duplicate_cluster_key)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_calls_outcome "
                "ON source_calls(outcome_status, call_ts)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info("bl_source_calls_v1_migration_complete", table="source_calls")
        except BaseException as e:
            _log.exception(
                "bl_source_calls_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_source_call_price_snapshots_v1(self) -> None:
        """C2 (#392): forward-only price snapshots for CA-keyed source calls.

        Additive + idempotent. Creates ``source_call_price_snapshots`` keyed by
        priceable identity (``identity_kind`` in {coin_id, contract}); the C2
        snapshot writer populates it and the (separate) C3 pricing hookup reads
        it. No ``source_calls`` columns are added — that table already carries
        every price / forward field.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "source_call_price_snapshots_v1"
        schema_version = 20260701

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info(
                    "source_call_price_snapshots_v1_migration_skip_already_applied"
                )
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for source_call_price_snapshots_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS source_call_price_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key TEXT NOT NULL,
                    identity_kind TEXT NOT NULL
                        CHECK (identity_kind IN ('coin_id', 'contract')),
                    chain TEXT,
                    price REAL NOT NULL,
                    snapshot_at TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (source IN ('gt', 'dex', 'cg')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scps_identity_ts "
                "ON source_call_price_snapshots("
                "identity_kind, identity_key, snapshot_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "source_call_price_snapshots_v1_migration_complete",
                table="source_call_price_snapshots",
            )
        except BaseException as e:
            _log.exception(
                "source_call_price_snapshots_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_source_call_snapshot_batches_v1(self) -> None:
        """B1-residual: durable post-commit visibility markers for snapshots.

        THE DEFECT. ``source_call_price_snapshots.created_at`` is stamped at
        INSERT, but the writer commits a whole cycle at once. A row inserted
        before an ``as_of`` and committed after it therefore satisfies both
        knowability bounds while having been genuinely unknowable at ``as_of``
        — and the first-inserted rows of a long cycle approach full-cycle
        exposure. That is future leakage: a historical feature can read a price
        the decision could not have seen.

        THE SHAPE. Not an attempt to guess SQLite's commit instant. Rows may
        exist first; an as-of reader treats them as knowable only once a
        durable marker written in a SEPARATE transaction AFTER the data commit
        exists with ``visible_at <= as_of``. Conservative LATE visibility is
        acceptable; future leakage is not — that asymmetry is the whole design.

        CRASH SAFETY leans the same way. A crash between the data commit and
        the marker commit leaves rows with a ``batch_id`` and no batch row, so
        they are INVISIBLE rather than early-visible. They stay invisible until
        an operator repair stamps the missing marker; the recovery story is
        documented in tasks/brief_snapshot_commit_visibility_migration.md.

        EPOCH. ``batch_id IS NULL`` marks rows written before this mechanism
        existed. They are grandfathered — but only up to ``epoch_cutover_ts``,
        recorded here at migration time. A NULL-batch row created AFTER that
        cutover is a writer bug, and the conservative reading of a bug is
        INVISIBLE, not always-visible; otherwise a stamping regression would
        silently restore the very leak this closes.

        Additive: one nullable column plus one new table. No backfill, no
        rewrite of existing rows.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "source_call_snapshot_batches_v1"
        schema_version = 20260813

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("source_call_snapshot_batches_v1_migration_skip_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS source_call_snapshot_batches (
                    batch_id INTEGER PRIMARY KEY,
                    visible_at TEXT NOT NULL,
                    rows_written INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scsb_visible_at "
                "ON source_call_snapshot_batches(visible_at)"
            )
            # The epoch cutover itself is durable state, not a constant: the
            # reader needs to distinguish "written before the mechanism" from
            # "written after it and unstamped", and only the DB knows when this
            # DB crossed over.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS source_call_snapshot_visibility_epoch (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    epoch_cutover_ts TEXT NOT NULL
                )
            """)
            # SAME PRODUCER AS `created_at`, deliberately. `created_at` is the
            # DDL default `datetime('now')` — space-separated, no offset — while
            # `now_iso` here is Python `.isoformat()`, 'T'-separated. Comparing
            # the two as TEXT is the INF-04 bug: ' ' (0x20) < 'T' (0x54), so a
            # Python-stamped cutover is lexicographically LATER than every
            # same-day SQLite timestamp and the grandfather branch admits rows
            # it must exclude. Stamping the cutover with datetime('now') keeps
            # both sides of that comparison in one format.
            #
            # NOT solved by wrapping the predicate in datetime(): that truncates
            # sub-second precision, which would round marker comparisons the
            # wrong way and admit rows early.
            await conn.execute(
                "INSERT OR IGNORE INTO source_call_snapshot_visibility_epoch "
                "(id, epoch_cutover_ts) VALUES (1, datetime('now'))"
            )

            cur_cols = await conn.execute(
                "PRAGMA table_info(source_call_price_snapshots)"
            )
            cols = {row[1] for row in await cur_cols.fetchall()}
            if cols and "batch_id" not in cols:
                await conn.execute(
                    "ALTER TABLE source_call_price_snapshots ADD COLUMN batch_id INTEGER"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scps_batch "
                    "ON source_call_price_snapshots(batch_id)"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            # Log the STORED value, not `now_iso`: the row is stamped by
            # SQLite `datetime('now')` and `now_iso` is Python-shaped, so
            # logging the latter would report a timestamp in a format the
            # column never holds — and this is the field an operator would
            # grep when diagnosing a grandfathering question.
            cur_epoch = await conn.execute(
                "SELECT epoch_cutover_ts FROM "
                "source_call_snapshot_visibility_epoch WHERE id = 1"
            )
            stored_epoch_row = await cur_epoch.fetchone()
            _log.info(
                "source_call_snapshot_batches_v1_migration_complete",
                table="source_call_snapshot_batches",
                epoch_cutover_ts=(stored_epoch_row[0] if stored_epoch_row else None),
            )
        except BaseException as e:
            _log.exception(
                "source_call_snapshot_batches_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_source_call_price_snapshot_runs_v1(self) -> None:
        """C4 (#392): per-cycle snapshot-writer run stats (§12a watchdog substrate).

        Additive + idempotent. Records each snapshot-writer cycle's counters so
        the coverage watchdogs can read OUTPUT rows (freshness, provider-error
        rate) rather than a heartbeat — distinguishing "writer down" from "writer
        ran but nothing priceable".
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "source_call_price_snapshot_runs_v1"
        schema_version = 20260702

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info(
                    "source_call_price_snapshot_runs_v1_migration_skip_already_applied"
                )
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for source_call_price_snapshot_runs_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS source_call_price_snapshot_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ran_at TEXT NOT NULL,
                    identities_seen INTEGER NOT NULL,
                    snapshots_written INTEGER NOT NULL,
                    provider_errors INTEGER NOT NULL,
                    pools_unresolved INTEGER NOT NULL,
                    empty_ohlcv INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scps_runs_ran_at "
                "ON source_call_price_snapshot_runs(ran_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "source_call_price_snapshot_runs_v1_migration_complete",
                table="source_call_price_snapshot_runs",
            )
        except BaseException as e:
            _log.exception(
                "source_call_price_snapshot_runs_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    # Measured 2026-08-23 via dbstat: signal_outcome_ledger 171,900,928 B table
    # + 59,994,112 B indexes over 511,386 rows = 453.42 B/row all-in, rounded UP
    # to 454. The direction is deliberate: an overestimate makes the reopen
    # threshold fire slightly EARLY, and late is the direction that lets a
    # deferral quietly stop being safe. Used as a
    # cheap proxy because dbstat itself scans the whole 7 GB database and takes
    # minutes -- far too heavy for an hourly probe whose only job is to notice a
    # threshold crossing.
    #
    # RE-MEASURE TRIGGER: the drift risk is `gate_verdicts`, a variable-length
    # JSON blob -- adding gates raises the true per-row cost with nothing here
    # noticing. (The label columns matter less: measured 257.7 B/row all-pending
    # vs 342.5 B/row all-complete, and at a 7-day finalize the table sits ~94%
    # complete.) Re-measure via dbstat if the gate set changes materially. The
    # pinning test asserts the constant against a 2026-08-23 measurement; it
    # cannot detect that the measurement went stale.
    _LEDGER_BYTES_PER_ROW = 454
    _LEDGER_REOPEN_RECLAIMABLE_BYTES = 100 * 1024 * 1024
    _LEDGER_REOPEN_PROJECTED_DAYS = 30
    _LEDGER_SIZE_CEILING_BYTES = 1024 * 1024 * 1024

    async def signal_outcome_ledger_growth_probe(
        self, *, horizon_days: int = 10
    ) -> dict:
        """Lightweight growth watch for the DEFERRED ruling-E pruning work.

        E was deferred on economics, not on correctness: as ruled
        (cohort-closure only, no age-based pruning) it reclaims ~309 rows of a
        511,386-row table, and the whole table is ~232 MB of a 7 GB database.
        Building the cohort registry, closure classifier, durable receipts and
        byte-identical proof harness for ~140 KB is not a defensible trade.

        Deferral is only safe if something watches for the economics changing,
        which is what this is. It reports the four measures the ruling names and
        sets `reopen` when either threshold is crossed. Reopening the
        INVESTIGATION needs no approval; any destructive pruning still follows
        the cohort-closure ruling.

        `horizon_days` is the label deadline (r7d horizon + permitted lateness):
        past it, a labelled row can no longer change, which is what makes it
        mechanically closed rather than merely old.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")

        cur = await self._conn.execute("SELECT COUNT(*) FROM signal_outcome_ledger")
        rows = (await cur.fetchone())[0]

        # No datetime() on the COLUMN side: it is non-sargable and forces a full
        # scan (measured 203 ms vs 0.19 ms on a 511k-row clone).
        #
        # This is a LEXICOGRAPHIC compare standing in for a chronological one,
        # which is only valid while every stored value is canonical
        # isoformat-T/UTC. An earlier version of this comment justified that on
        # "record_emission always stores datetime.now(timezone.utc).isoformat()"
        # -- which was FALSE: `record_emission_with_status` takes an
        # `emitted_at` parameter for backfills and stored it unvalidated, so a
        # space-separated or negative-offset value would sort below 'T' and be
        # silently dropped from the window (growth undercounts -> under-alarm).
        #
        # The invariant is now ENFORCED at the writer by
        # `scout.outcome_ledger._normalise_emitted_at`, not asserted here. If
        # that normalisation is ever removed, this must go back to datetime().
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM signal_outcome_ledger "
            "WHERE emitted_at >= strftime('%Y-%m-%dT%H:%M:%S','now','-1 day')"
        )
        growth_rows_per_day = (await cur.fetchone())[0]

        # Safely prunable == fully-dormant (surface, kind) cohorts, per the
        # cohort-closure ruling. NOT age-based: a cohort still emitting is not
        # closed no matter how old its oldest row is.
        cur = await self._conn.execute(f"""SELECT COALESCE(SUM(n), 0) FROM (
                    SELECT COUNT(*) AS n
                      FROM signal_outcome_ledger
                     GROUP BY surface, kind
                    HAVING datetime(MAX(emitted_at))
                             < datetime('now', '-{int(horizon_days)} days')
                       AND SUM(label_status IN ('pending','partial')) = 0)""")
        reclaimable_rows = (await cur.fetchone())[0]

        bytes_total = rows * self._LEDGER_BYTES_PER_ROW
        reclaimable_bytes = reclaimable_rows * self._LEDGER_BYTES_PER_ROW
        growth_bytes_per_day = growth_rows_per_day * self._LEDGER_BYTES_PER_ROW

        headroom = self._LEDGER_SIZE_CEILING_BYTES - bytes_total
        if growth_bytes_per_day > 0:
            days_to_ceiling = max(0.0, headroom / growth_bytes_per_day)
        else:
            days_to_ceiling = float("inf")

        reopen_reasons = []
        if reclaimable_bytes >= self._LEDGER_REOPEN_RECLAIMABLE_BYTES:
            reopen_reasons.append("reclaimable_bytes")
        # ALREADY over the ceiling. This is a separate condition from the
        # projection on purpose: the projection is gated on
        # `growth_bytes_per_day > 0`, so without this clause a table that has
        # crossed 1 GB and then stopped growing -- the kill switch flipped off,
        # or any >24h outage -- reports `reopen: False` and the hourly line
        # reads green. The watch would go quiet at exactly the moment the thing
        # it watches for had already happened.
        if bytes_total >= self._LEDGER_SIZE_CEILING_BYTES:
            reopen_reasons.append("over_ceiling")
        if days_to_ceiling <= self._LEDGER_REOPEN_PROJECTED_DAYS:
            reopen_reasons.append("projected_size")

        return {
            "status": "E_DEFERRED_BY_ECONOMICS",
            "rows": rows,
            # `_est` because these are rows x a measured proxy, not dbstat. An
            # operator grepping journald for `bytes_total=` would otherwise read
            # them as measured.
            "bytes_total_est": bytes_total,
            "bytes_per_row_proxy": self._LEDGER_BYTES_PER_ROW,
            "growth_rows_per_day": growth_rows_per_day,
            "growth_bytes_per_day_est": growth_bytes_per_day,
            "reclaimable_rows": reclaimable_rows,
            # Drops the ruling's "non-protected" qualifier: no protection
            # mechanism exists yet, so this counts closed cohorts regardless.
            # Safe direction -- it OVERSTATES what is reclaimable, so the alarm
            # fires early -- but the name promises more than it delivers.
            "reclaimable_bytes_est": reclaimable_bytes,
            "days_to_1gb": (
                None if days_to_ceiling == float("inf") else round(days_to_ceiling, 1)
            ),
            "reopen": bool(reopen_reasons),
            "reopen_reasons": reopen_reasons,
        }

    async def reconcile_signal_first_seen(self) -> dict[str, int]:
        """Re-derive the substrate from surviving events. Idempotent, self-healing.

        The substrate's per-write path is NOT atomic against the shared
        connection, and no savepoint can make it so. `scout/chains/tracker.py`
        opens an explicit `BEGIN` on `db._conn` and calls `conn.rollback()` on
        any failure in its pass -- and that failure fires several times a day
        in production. A concurrent `emit_event` whose fold is pending when
        that FOREIGN rollback lands loses the fold, then commits its own event
        insert: a committed event with no substrate row, reading as "never
        detected by chains" at every consumer.

        Ordering the fold before the insert does not help; neither does
        insert-first. The two writes are simply not atomic against a foreign
        rollback, so the durable answer is repair rather than prevention.

        This is that repair, and it is safe precisely because the fold can only
        ever LOWER the minimum: re-running it can restore a lost row or correct
        a late one, and can never push a first_seen later than the truth. It
        also repairs a truncated/restored table, which the one-shot migration
        cannot -- that early-returns forever once its marker exists.

        Returns counts for observability; `repaired` > 0 is a real signal that
        the write path dropped something, not routine noise.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT token_id FROM signal_events "
            "WHERE token_id IS NOT NULL AND token_id != '' "
            "EXCEPT SELECT token_id FROM signal_first_seen)"
        )
        missing = (await cur.fetchone())[0]
        cur = await self._conn.execute("""SELECT COUNT(*) FROM signal_first_seen s
                 JOIN (SELECT token_id, MIN(created_at) m FROM signal_events
                        WHERE token_id IS NOT NULL AND token_id != ''
                        GROUP BY token_id) e ON e.token_id = s.token_id
                WHERE s.first_seen_at > e.m""")
        late = (await cur.fetchone())[0]

        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO signal_first_seen (token_id, first_seen_at, updated_at)
               SELECT token_id, MIN(created_at), ?
                 FROM signal_events
                WHERE token_id IS NOT NULL AND token_id != ''
                GROUP BY token_id
               ON CONFLICT(token_id) DO UPDATE SET
                 first_seen_at = MIN(
                   signal_first_seen.first_seen_at, excluded.first_seen_at),
                 updated_at = excluded.updated_at""",
            (now,),
        )
        # An '' row poisons every consumer via LIKE '%'; nothing prunes this
        # table, so sweep it here rather than hoping it never lands.
        cur = await self._conn.execute(
            "DELETE FROM signal_first_seen WHERE token_id IS NULL OR token_id = ''"
        )
        poisoned = cur.rowcount or 0
        await self._conn.commit()
        return {
            "repaired": missing + late,
            "missing": missing,
            "late": late,
            "poisoned_removed": poisoned,
        }

    async def record_signal_first_seen(self, token_id: str, created_at: str) -> None:
        """Fold one observation into the derived first-seen substrate.

        MIN semantics, NOT insert-once. `emit_event` stamps `created_at` in
        Python, so two concurrent callers can interleave and produce an event
        whose timestamp is EARLIER than one already written; replay and
        backfill paths write historical rows deliberately. An insert-once cache
        would keep whichever row happened to land first and be permanently
        wrong about the minimum, with nothing to signal it.

        Idempotent and order-independent: applying the same observations in any
        order, any number of times, converges on the same value.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        if not token_id or not created_at:
            # RAISE, do not return. `signal_events.token_id` is TEXT NOT NULL
            # but '' satisfies that, so a silent no-op here commits an event
            # with no substrate row -- the exact divergence this table exists
            # to prevent, and invisible because the write "succeeded".
            #
            # An empty token_id in the substrate is separately destructive:
            # consumers match with `LOWER(?) LIKE LOWER(token_id || '%')`,
            # which for '' degenerates to LIKE '%' and matches EVERY coin. MIN
            # then makes that row win everywhere, and nothing prunes this table
            # so it would never age out.
            raise ValueError(
                f"record_signal_first_seen requires a non-empty token_id and "
                f"created_at, got token_id={token_id!r} created_at={created_at!r}"
            )
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO signal_first_seen (token_id, first_seen_at, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(token_id) DO UPDATE SET
                 first_seen_at = MIN(
                   signal_first_seen.first_seen_at, excluded.first_seen_at),
                 updated_at = excluded.updated_at""",
            (token_id, created_at, now),
        )

    async def _migrate_signal_first_seen_v1(self) -> None:
        """Derived first-seen substrate for signal_events (retention option F).

        Several consumers derive "when did we first see this token" with
        ``MIN(created_at)`` over the whole of ``signal_events``. That makes
        RETENTION the implicit historical boundary: shorten it and the derived
        minimum silently moves forward, changing lead-time attribution with no
        error anywhere. The ruling's requirement is to build the derived state
        FIRST, migrate the consumers, prove parity, and only then reopen
        retention separately -- which this migration does not touch.

        MONOTONIC BY CONSTRUCTION. The writer takes ``MIN(existing, incoming)``,
        not insert-once. An insert-once cache would be wrong the first time an
        event arrives carrying an earlier timestamp than one already recorded --
        a replay, an out-of-order insert, or a backfill -- and the repository
        cannot prove those impossible: ``emit_event`` stamps ``created_at`` in
        Python, so two callers can interleave, and the ledger/backfill paths
        write historical rows by design.

        Backfill is a single grouped scan of the existing table, so the
        substrate starts equal to what the consumers compute today. That
        equality is what the differential tests assert.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "signal_first_seen_v1"
        schema_version = 20260823

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("signal_first_seen_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for signal_first_seen_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_first_seen (
                    token_id TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            # Backfill from whatever history still exists. Runs BEFORE any
            # retention change, which is the whole point of ordering F ahead of
            # a shortening: once rows are gone the true minimum is
            # unrecoverable.
            await conn.execute(
                """INSERT INTO signal_first_seen (token_id, first_seen_at, updated_at)
                   SELECT token_id, MIN(created_at), ?
                     FROM signal_events
                    WHERE token_id IS NOT NULL AND token_id != ''
                    GROUP BY token_id
                   ON CONFLICT(token_id) DO UPDATE SET
                     first_seen_at = MIN(
                       signal_first_seen.first_seen_at, excluded.first_seen_at),
                     updated_at = excluded.updated_at""",
                (now_iso,),
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signal_first_seen_at "
                "ON signal_first_seen(first_seen_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            cur = await conn.execute("SELECT COUNT(*) FROM signal_first_seen")
            n = (await cur.fetchone())[0]
            _log.info(
                "signal_first_seen_v1_migration_complete",
                table="signal_first_seen",
                backfilled_tokens=n,
            )
        except BaseException as e:
            _log.exception(
                "signal_first_seen_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_cg_credit_ledger_v1(self) -> None:
        """Monthly-credit accounting for the CoinGecko provider budget.

        2026-08-21: the Basic plan's 100,000 monthly credits hit 100.0% with 11
        days to the reset. Nothing counted them — the system modeled
        calls/MINUTE (the rate limiter) while the hard constraint was
        calls/MONTH. Those are independent limits at the provider.

        Durable because module counters reset on restart, and a budget you can
        zero by bouncing the service is not a budget.

        Two measures per (month, bucket), because ATTEMPTS ARE NOT CREDITS:
          * ``attempts`` — every request issued. Rate/backoff observability.
          * ``credits``  — successful billable calls only. CoinGecko deducts a
            monthly credit on HTTP 200; 4xx/5xx do NOT deduct one, though they
            still count against the per-minute rate limit.

        ``month`` is the provider's billing period key (``YYYY-MM``, UTC) —
        credits replenish on the 1st. ``bucket`` partitions the allowance so
        discovery cannot consume the reserve that keeps held positions
        re-priceable.

        Additive + idempotent, mirroring _migrate_ingest_watchdog_state_v1.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "cg_credit_ledger_v1"
        schema_version = 20260821

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("cg_credit_ledger_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for cg_credit_ledger_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS cg_credit_ledger (
                    month TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    credits INTEGER NOT NULL DEFAULT 0,
                    -- When CoinGecko itself last returned HTTP 200. This is the
                    -- PROVIDER liveness heartbeat the open-boundary gate reads.
                    -- Deliberately not derived from price_cache: that table is
                    -- also written by DexScreener (outcome_ledger's dex
                    -- enrollment poller), so its freshness says nothing about
                    -- CoinGecko. Durable so a restart does not present a dead
                    -- provider as merely unobserved.
                    last_success_at TEXT,
                    -- Reconciled provider truth. Persisted because
                    -- effective_used() depends on it: without this a restart
                    -- came back with zero drift and treated unattributed spend
                    -- as capacity that still existed.
                    provider_credits_used INTEGER,
                    provider_checked_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (month, bucket)
                )
                """)
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "cg_credit_ledger_v1_migration_complete", table="cg_credit_ledger"
            )
        except BaseException as e:
            _log.exception(
                "cg_credit_ledger_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_ingest_watchdog_state_v1(self) -> None:
        """GA-19: durable ingest-starvation watchdog state.

        The per-source consecutive-miss counter previously lived only in a
        module-level dict (scout/heartbeat.py) cleared on every boot, so
        gecko-pipeline restarts (deploys, Restart=always crash-bounces)
        reset the counter and `ingest_source_starved` never fired for a
        persistently-dead source. This table makes the counter
        restart-durable; scout/heartbeat.py hydrates from it at startup
        and writes through each cycle.

        Additive + idempotent, mirroring
        :meth:`_migrate_source_call_price_snapshot_runs_v1`.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "ingest_watchdog_state_v1"
        schema_version = 20260703

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("ingest_watchdog_state_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for ingest_watchdog_state_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ingest_watchdog_state (
                    source TEXT PRIMARY KEY,
                    consecutive_misses INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "ingest_watchdog_state_v1_migration_complete",
                table="ingest_watchdog_state",
            )
        except BaseException as e:
            _log.exception(
                "ingest_watchdog_state_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_signal_outcome_ledger_v1(self) -> None:
        """P0 edge-audit 2026-07-02: self-labeling forward-return ledger.

        The 2026-07-02 edge audit (tasks/gecko-alpha-fable-review_2026_07.md
        Phase 3) found historical forward-returns uncomputable — the alerts
        table has 33 lifetime rows with no usable price, so gate
        counterfactuals are impossible. ``signal_outcome_ledger`` records
        every future emission (candidate alert, paper-trade dispatch, sampled
        gate-block) WITH its price + liquidity at emission, and an in-DB
        labeler (scout/outcome_ledger.py) fills r15m/r1h/r4h/r24h/r7d/peak7d
        from volume_history_cg history + price_cache — zero external API
        budget. This is the measurement precondition for the exit-lifecycle
        flagship and funnel reopening.

        §12a freshness surface: the table is registered in the dashboard
        system-health query path (dashboard/db.py get_system_health, keyed on
        emitted_at) and the hourly labeler pass emits a ``ledger_label_pass``
        structured log every run.

        Additive + idempotent, mirroring :meth:`_migrate_ingest_watchdog_state_v1`.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "signal_outcome_ledger_v1"
        schema_version = 20260704

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("signal_outcome_ledger_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for signal_outcome_ledger_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_outcome_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL
                        CHECK(kind IN ('alert','dispatch','gated_out_sample')),
                    token_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    price_at_emission REAL,
                    -- Age (seconds) of the anchor price at emission: 0.0 for
                    -- live caller-supplied prices (dispatch / gated_out),
                    -- now - price_cache.updated_at for cache-resolved alert
                    -- anchors, NULL when price_at_emission is NULL.
                    anchor_cache_age_seconds REAL,
                    liquidity_at_emission REAL,
                    liquidity_source TEXT,
                    gate_verdicts TEXT,
                    -- Forward-polling enrollment outcome at emission:
                    -- 'not_needed' (token has in-DB price coverage),
                    -- 'enrolled' (enrollment row written; may later be
                    -- cap-evicted — see the ledger_enrollment_evicted log),
                    -- 'skipped_cap' (reserved; unreachable under the current
                    -- evict-oldest-to-make-room semantics).
                    enrollment_status TEXT
                        CHECK(enrollment_status IN
                              ('not_needed','enrolled','skipped_cap')),
                    emitted_at TEXT NOT NULL,
                    r15m REAL,
                    r1h REAL,
                    r4h REAL,
                    r24h REAL,
                    r7d REAL,
                    peak7d REAL,
                    label_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(label_status IN
                              ('pending','partial','complete','unlabelable')),
                    labeled_at TEXT
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sol_status_emitted "
                "ON signal_outcome_ledger(label_status, emitted_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sol_token_emitted "
                "ON signal_outcome_ledger(token_id, emitted_at)"
            )
            # Enrollment-at-emission (operator design fold): tokens with no
            # in-DB price coverage (gated-out micro-caps, dex:-namespace ids)
            # would make their ledger rows structurally unlabelable — the
            # missed-winner recall lane would be unmeasurable. Emissions
            # enroll such tokens into a TTL'd, capped forward-polling set;
            # a per-cycle poller prices them through price_cache so labeling
            # stays in-DB. See scout/outcome_ledger.py poll_enrollments.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ledger_enrollments (
                    token_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    enrolled_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_enroll_expires "
                "ON ledger_enrollments(expires_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "signal_outcome_ledger_v1_migration_complete",
                table="signal_outcome_ledger",
            )
        except BaseException as e:
            _log.exception(
                "signal_outcome_ledger_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_ledger_enrollment_evictions_v1(self) -> None:
        """BL-NEW-LEDGER-EVICTION-DB-MARKER: durable cap-eviction record.

        REQUIRED 2026-07-02 (operator ruling on #406; the evict-oldest
        enrollment cap was ACCEPTED conditional on this). Cap eviction from
        ``ledger_enrollments`` previously left ONLY a ``ledger_enrollment_evicted``
        structured log, scraped weekly into an interim JSONL by
        ``scripts/ledger-eviction-export.sh`` — lossy once journald rotates
        (~3wk size-based clock). This table makes each eviction a durable,
        per-token DB row so ``unlabelable: cap-eviction`` (censored) separates
        from ``unlabelable: liquidity-death`` via DB state alone.

        One row per evicted token per eviction event. UNIQUE(token_id,
        evicted_at) is the idempotency key: the live writer and the one-time
        journal backfill (scripts/backfill_ledger_eviction_markers.py) share it,
        so a backfilled journal record of a live eviction dedups against the
        live row instead of double-counting.

        New table only — additive + idempotent, mirroring
        :meth:`_migrate_ingest_watchdog_state_v1`.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "ledger_enrollment_evictions_v1"
        schema_version = 20260710

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info(
                    "ledger_enrollment_evictions_v1_migration_skip_already_applied"
                )
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for ledger_enrollment_evictions_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ledger_enrollment_evictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    -- The evicted token (removed from ledger_enrollments).
                    token_id TEXT NOT NULL,
                    -- ISO timestamp of the eviction event; matches the
                    -- ledger_enrollment_evicted log's evicted_at field.
                    evicted_at TEXT NOT NULL,
                    -- The incoming token whose enrollment triggered the cap
                    -- eviction (evict-oldest-to-make-room).
                    evicted_for TEXT,
                    -- LEDGER_ENROLLMENT_MAX_ACTIVE at eviction time.
                    max_active INTEGER,
                    -- Batch size of the eviction event this row belongs to.
                    n_evicted INTEGER,
                    -- Provenance: 'live' (written atomically with the DELETE)
                    -- or 'journal_backfill' (one-time import from the JSONL).
                    source TEXT NOT NULL DEFAULT 'live',
                    -- Idempotency key shared by live writer + backfill.
                    UNIQUE(token_id, evicted_at)
                )
                """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_evictions_evicted_at "
                "ON ledger_enrollment_evictions(evicted_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "ledger_enrollment_evictions_v1_migration_complete",
                table="ledger_enrollment_evictions",
            )
        except BaseException as e:
            _log.exception(
                "ledger_enrollment_evictions_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_solana_executions_v1(self) -> None:
        """Durable execution state for the Solana DEX lane.

        A NEW TABLE rather than a column on live_trades, and the reason is
        correctness rather than tidiness. ``live_trades.status`` is a
        CROSS-VENUE column meaning "what happened to the trade" — Binance and
        Kraken rows share it, and the exposure view, the daily-gross rollups
        and the Kraken lane's own gates all read its vocabulary. The Solana
        lane's 13 states are a different axis: where in the pipeline this
        transaction is. Overloading one column with two orthogonal meanings is
        how a cross-venue query silently misreads a row.

        Four of those states — quote_created, transaction_built,
        simulation_passed, awaiting_authorization — also exist BEFORE anything
        financial does. Holding them on live_trades would mean a row in `open`
        status backed by no trade, which every consumer of exposure would
        count as a live position.

        Hence: lifecycle here, money in live_trades, joined by a NULLABLE
        live_trade_id. Nullable because an execution that never reaches signing
        has no money row and must still be storable — that is the case the
        recovery path most needs to read.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "solana_executions_v1"
        schema_version = 20260801

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                await self._assert_solana_executions_schema(conn)
                _db_log.info("solana_executions_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version = ?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if existing_version is not None and existing_version[0] != migration_name:
                raise RuntimeError(
                    "solana_executions_v1 schema_version description mismatch - "
                    f"version {schema_version} already owned by "
                    f"{existing_version[0]!r}"
                )
            # The CHECK enumerates exactly the 13 states. A state the machine
            # does not know cannot be written at all, so a typo in a future
            # transition fails at the write rather than becoming a row nobody
            # can classify on recovery.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS solana_executions (
                    decision_id             TEXT PRIMARY KEY,
                    state                   TEXT NOT NULL CHECK (state IN (
                        'quote_created','transaction_built','simulation_passed',
                        'awaiting_authorization','authorized','signed',
                        'submission_attempted','landed','confirmed','finalized',
                        'reconciled','failed','submission_unknown'
                    )),
                    mode                    TEXT NOT NULL,
                    live_trade_id           INTEGER,
                    message_sha256          TEXT,
                    expected_signature      TEXT,
                    last_valid_block_height INTEGER,
                    amount_lamports         INTEGER,
                    minimum_out_raw         INTEGER,
                    detail                  TEXT,
                    created_at              TEXT NOT NULL,
                    updated_at              TEXT NOT NULL,
                    FOREIGN KEY (live_trade_id)
                        REFERENCES live_trades(id) ON DELETE SET NULL
                )
                """)
            # Indexes AFTER the table (project DDL-ordering discipline). The
            # state index is what the stuck-execution watchdog and the startup
            # recovery scan both read.
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_executions_state "
                "ON solana_executions(state, updated_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_executions_signature "
                "ON solana_executions(expected_signature)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            await self._assert_solana_executions_schema(conn)
            _db_log.info(
                "solana_executions_v1_migration_complete", table="solana_executions"
            )
        except BaseException as e:
            _db_log.exception(
                "schema_migration_failed",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _db_log.error("SCHEMA_DRIFT_DETECTED", migration=migration_name)
            raise

    async def _assert_solana_executions_schema(self, conn) -> None:
        """Fail loudly if the table is not the shape the lane expects."""
        cur = await conn.execute("PRAGMA table_info(solana_executions)")
        columns = {row[1] for row in await cur.fetchall()}
        required = {
            "decision_id",
            "state",
            "mode",
            "live_trade_id",
            "message_sha256",
            "expected_signature",
            "last_valid_block_height",
            "amount_lamports",
            "minimum_out_raw",
            "detail",
            "created_at",
            "updated_at",
        }
        missing = required - columns
        if missing:
            raise RuntimeError(
                f"solana_executions is missing column(s): {sorted(missing)}"
            )

    async def _migrate_retire_dead_tables_v1(self, *, enabled: bool = False) -> None:
        """NAR-06 + INF-07: retire dead tables. schema_version 20260711.

        NAR-06 named three LunarCrush tables; ``social_signals`` is EXCLUDED
        (see below), so the DROP set is four tables.

        Drops (verified zero live readers, list in the PR body):
          * ``social_baselines`` / ``social_credit_ledger`` — retired LunarCrush
            integration, 0 rows ever, only readers/writers were the deleted
            LunarCrush code.
          * ``gainers_comparisons_bak_20260602051857`` — one-off backup artifact
            (scripts/backfill_gainers_comparisons.py mints new timestamped
            backups; it never reads this dated instance).
          * ``paper_trades_junk_backfill`` — one-off backfill artifact, zero refs.

        ``social_signals`` is deliberately NOT in the set: scout/trending/
        tracker.py reads it (the trending-comparison "social 4th tier"), a live
        consumer independent of the retired LunarCrush loop. Dropping it would
        break that path with "no such table".

        OPT-IN DESTRUCTIVE / fail-closed. Unlike every other migration here the
        DDL is IRREVERSIBLE — SQLite cannot roll a committed ``DROP TABLE`` back;
        recovery is restore-from-backup only. So the drops run ONLY when the
        operator sets ``RETIRE_DEAD_TABLES_ENABLED=true`` at a deploy (plumbed
        via ``initialize(retire_dead_tables=...)``). The flag IS the
        recorded-approval hook: no flag, no drop, and — critically — nothing is
        stamped in ``paper_migrations`` / ``schema_version`` until the drops
        actually execute, so a later flag-on deploy still performs them.

        Idempotent: once recorded, re-runs skip regardless of the flag.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "retire_dead_tables_v1"
        schema_version = 20260711

        # Module-local constants, never user input — safe to interpolate.
        dead_tables = (
            "social_baselines",
            "social_credit_ledger",
            "gainers_comparisons_bak_20260602051857",
            "paper_trades_junk_backfill",
        )

        # Idempotence: if already applied, skip regardless of the flag — the
        # drops already happened on the deploy that recorded the migration.
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("retire_dead_tables_v1_migration_skip_already_applied")
                return

        # Fail-closed gate: irreversible drops never fire (and nothing is
        # recorded) until the operator flips the flag at a deploy.
        if not enabled:
            _log.info("retire_dead_tables_v1_migration_skip_disabled")
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for retire_dead_tables_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            for table in dead_tables:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")

            # Record ONLY after the drops actually executed (fail-closed).
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "retire_dead_tables_v1_migration_complete",
                dropped=list(dead_tables),
            )
        except BaseException as e:
            _log.exception(
                "retire_dead_tables_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def _migrate_price_provenance_v1(self) -> None:
        """Phase 6 slices 2+3: price-source invariant + exit provenance.

        Adds five nullable columns to ``paper_trades``:

        - ``price_source`` — stamped at open ('cg_lane' | 'price_cache_row');
          backfilled to 'legacy' for every pre-existing row (open or closed)
          so post-migration NULL means "writer bug", never "old row".
        - ``exit_provenance`` — stamped at close ('market' |
          'stale_snapshot' | 'entry_fallback'); backfilled for closed rows
          from exit_reason (the GA-01 #404 fabricated-close reasons map to
          their provenance; every other closed row was a market fill).
          Open rows stay NULL until their close stamps it.
        - ``stale_age_seconds_at_exit`` / ``last_good_price_at`` /
          ``liquidity_at_exit`` — stale-onset mark provenance, written only
          by the evaluator's stale-onset exit. ``liquidity_at_exit`` NULL
          means "could not verify exitability" (token had no observed
          liquidity when the price feed died).

        Additive + idempotent, mirroring
        :meth:`_migrate_ingest_watchdog_state_v1`.
        """
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "price_provenance_v1"
        schema_version = 20260705

        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_migrations'"
        )
        if await cur.fetchone():
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name=?", (migration_name,)
            )
            if await cur.fetchone():
                _log.info("price_provenance_v1_migration_skip_already_applied")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS paper_migrations ("
                "name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            cur = await conn.execute(
                "SELECT description FROM schema_version WHERE version=?",
                (schema_version,),
            )
            existing_version = await cur.fetchone()
            if (
                existing_version is not None
                and existing_version["description"] != migration_name
            ):
                raise RuntimeError(
                    "schema_version collision for price_provenance_v1: "
                    f"version={schema_version} "
                    f"description={existing_version['description']}"
                )

            cur = await conn.execute("PRAGMA table_info(paper_trades)")
            cols = {row[1] for row in await cur.fetchall()}
            for col, decl in (
                ("price_source", "TEXT"),
                ("exit_provenance", "TEXT"),
                ("stale_age_seconds_at_exit", "REAL"),
                ("last_good_price_at", "TEXT"),
                ("liquidity_at_exit", "REAL"),
            ):
                if col not in cols:
                    await conn.execute(
                        f"ALTER TABLE paper_trades ADD COLUMN {col} {decl}"
                    )

            # Backfill 1: every pre-invariant row is 'legacy' — including
            # still-open rows (they were opened before the stamp existed).
            await conn.execute(
                "UPDATE paper_trades SET price_source = 'legacy' "
                "WHERE price_source IS NULL"
            )
            # Backfill 2: closed rows get provenance from exit_reason. The
            # two GA-01 fabricated-close reasons carry their provenance in
            # the reason string; everything else closed was a market fill.
            # Guarded on exit_reason existing: ancient pre-feedback-loop
            # table shapes (tests/test_trading_db_migration.py) lack it —
            # such DBs also cannot contain GA-01 fabricated rows, so the
            # 'market' fallback below is the correct label for their closes.
            if "exit_reason" in cols:
                await conn.execute(
                    "UPDATE paper_trades SET exit_provenance = 'entry_fallback' "
                    "WHERE exit_provenance IS NULL "
                    "  AND exit_reason = 'expired_stale_no_price'"
                )
                await conn.execute(
                    "UPDATE paper_trades SET exit_provenance = 'stale_snapshot' "
                    "WHERE exit_provenance IS NULL "
                    "  AND exit_reason = 'expired_stale_price'"
                )
            await conn.execute(
                "UPDATE paper_trades SET exit_provenance = 'market' "
                "WHERE exit_provenance IS NULL AND status != 'open'"
            )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration_name, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (schema_version, now_iso, migration_name),
            )
            await conn.commit()
            _log.info(
                "price_provenance_v1_migration_complete",
                table="paper_trades",
            )
        except BaseException as e:
            _log.exception(
                "price_provenance_v1_migration_rollback",
                migration=migration_name,
                err=str(e),
                err_type=type(e).__name__,
            )
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    async def load_ingest_watchdog_state(self) -> dict[str, int]:
        """GA-19: read all persisted per-source consecutive-miss counters."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cur = await self._conn.execute(
            "SELECT source, consecutive_misses FROM ingest_watchdog_state"
        )
        rows = await cur.fetchall()
        return {row["source"]: int(row["consecutive_misses"]) for row in rows}

    async def upsert_ingest_watchdog_state(
        self, source: str, consecutive_misses: int
    ) -> None:
        """GA-19: write-through one source's consecutive-miss counter.

        Deliberately ON CONFLICT DO UPDATE (never INSERT OR REPLACE) so any
        future decoupled columns on this table are not clobbered — see the
        UPSERT-clobber lesson from PR #325.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO ingest_watchdog_state "
            "(source, consecutive_misses, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "consecutive_misses=excluded.consecutive_misses, "
            "updated_at=excluded.updated_at",
            (source, consecutive_misses, now_iso),
        )
        await self._conn.commit()

    async def _assert_minara_alert_emissions_schema(
        self, conn, *, require_indexes: bool = True
    ) -> None:
        required_columns = {
            "id",
            "paper_trade_id",
            "tg_alert_log_id",
            "signal_type",
            "coin_id",
            "chain",
            "amount_usd",
            "command_text",
            "command_hash",
            "command_text_observed",
            "source",
            "source_event_id",
            "emitted_at",
            "operator_paste_acknowledged_at",
        }
        cur = await conn.execute("PRAGMA table_info(minara_alert_emissions)")
        col_rows = await cur.fetchall()
        cols = {row[1] for row in col_rows}
        missing = sorted(required_columns - cols)
        if missing:
            raise RuntimeError(
                "bl_minara_alert_emissions_v1 schema missing columns: "
                + ", ".join(missing)
            )
        by_name = {row[1]: row for row in col_rows}
        expected = {
            "id": ("INTEGER", False, None, 1),
            "paper_trade_id": ("INTEGER", False, None, 0),
            "tg_alert_log_id": ("INTEGER", False, None, 0),
            "signal_type": ("TEXT", True, None, 0),
            "coin_id": ("TEXT", True, None, 0),
            "chain": ("TEXT", True, None, 0),
            "amount_usd": ("REAL", True, None, 0),
            "command_text": ("TEXT", False, None, 0),
            "command_hash": ("TEXT", False, None, 0),
            "command_text_observed": ("INTEGER", True, "0", 0),
            "source": ("TEXT", True, None, 0),
            "source_event_id": ("TEXT", True, None, 0),
            "emitted_at": ("TEXT", True, None, 0),
            "operator_paste_acknowledged_at": ("TEXT", False, None, 0),
        }
        for name, (typ, notnull, default, pk) in expected.items():
            row = by_name[name]
            if (row[2] or "").upper() != typ:
                raise RuntimeError(
                    f"bl_minara_alert_emissions_v1 column {name} type mismatch"
                )
            if bool(row[3]) != notnull:
                raise RuntimeError(
                    f"bl_minara_alert_emissions_v1 column {name} NOT NULL mismatch"
                )
            actual_default = row[4]
            if default is None:
                if actual_default is not None:
                    raise RuntimeError(
                        f"bl_minara_alert_emissions_v1 column {name} default mismatch"
                    )
            elif str(actual_default).strip("'\"") != default:
                raise RuntimeError(
                    f"bl_minara_alert_emissions_v1 column {name} default mismatch"
                )
            if int(row[5]) != pk:
                raise RuntimeError(
                    f"bl_minara_alert_emissions_v1 column {name} pk mismatch"
                )

        cur = await conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='minara_alert_emissions'"
        )
        create_sql_row = await cur.fetchone()
        create_sql = (create_sql_row[0] if create_sql_row else "").upper()
        if "CHECK (COMMAND_TEXT_OBSERVED IN (0,1))" not in create_sql:
            raise RuntimeError(
                "bl_minara_alert_emissions_v1 command_text_observed CHECK missing"
            )
        if "CHECK (SOURCE IN ('LIVE','JOURNALCTL_BACKFILL'))" not in create_sql:
            raise RuntimeError("bl_minara_alert_emissions_v1 source CHECK missing")
        if "ON DELETE RESTRICT" not in create_sql:
            raise RuntimeError(
                "bl_minara_alert_emissions_v1 paper_trade_id FK action missing"
            )

        cur = await conn.execute("PRAGMA foreign_key_list(minara_alert_emissions)")
        fks = await cur.fetchall()
        has_paper_trade_fk = any(
            row[2] == "paper_trades"
            and row[3] == "paper_trade_id"
            and row[4] == "id"
            and row[6].upper() == "RESTRICT"
            for row in fks
        )
        if not has_paper_trade_fk:
            raise RuntimeError("bl_minara_alert_emissions_v1 paper_trade_id FK missing")

        if not require_indexes:
            return

        cur = await conn.execute("PRAGMA index_list(minara_alert_emissions)")
        indexes = {row[1]: bool(row[2]) for row in await cur.fetchall()}
        if "sqlite_autoindex_minara_alert_emissions_1" not in indexes:
            raise RuntimeError(
                "bl_minara_alert_emissions_v1 source_event_id unique index missing"
            )
        if indexes.get("idx_minara_alert_emissions_emitted_at") is not False:
            raise RuntimeError("bl_minara_alert_emissions_v1 emitted_at index missing")
        if indexes.get("idx_minara_alert_emissions_coin_id") is not False:
            raise RuntimeError("bl_minara_alert_emissions_v1 coin_id index missing")
        if indexes.get("idx_minara_alert_emissions_tg_alert_log_id") is not True:
            raise RuntimeError(
                "bl_minara_alert_emissions_v1 tg_alert_log_id unique index missing"
            )
        expected_index_columns = {
            "idx_minara_alert_emissions_emitted_at": ["emitted_at"],
            "idx_minara_alert_emissions_coin_id": ["coin_id", "emitted_at"],
            "idx_minara_alert_emissions_tg_alert_log_id": ["tg_alert_log_id"],
        }
        for index_name, expected_cols in expected_index_columns.items():
            cur = await conn.execute(f"PRAGMA index_info({index_name})")
            actual_cols = [row[2] for row in await cur.fetchall()]
            if actual_cols != expected_cols:
                raise RuntimeError(
                    f"bl_minara_alert_emissions_v1 {index_name} columns mismatch"
                )
        cur = await conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' "
            "AND name='idx_minara_alert_emissions_tg_alert_log_id'"
        )
        idx_row = await cur.fetchone()
        idx_sql = (idx_row[0] if idx_row else "").upper()
        if "WHERE TG_ALERT_LOG_ID IS NOT NULL" not in idx_sql:
            raise RuntimeError(
                "bl_minara_alert_emissions_v1 tg_alert_log_id partial index missing"
            )

    async def _assert_paper_trade_entry_snapshots_schema(
        self, conn, *, require_indexes: bool = True
    ) -> None:
        required_columns = {
            "paper_trade_id",
            "entry_snapshot_version",
            "entry_snapshot_complete",
            "entry_snapshot_missing_fields",
            "captured_at",
            "signal_type",
            "mcap_usd_at_entry",
            "mcap_bucket_at_entry",
            "liquidity_usd_at_entry",
            "token_age_days_at_entry",
            "first_seen_at_at_entry",
            "detected_by_combo_at_entry",
            "source_confluence_count_at_entry",
            "tg_channel_at_entry",
            "actionability_version_at_entry",
            "actionability_reason_at_entry",
            "actionable_at_entry",
            "tp_pct_at_entry",
            "sl_pct_at_entry",
            "trail_pct_at_entry",
            "trail_pct_low_peak_at_entry",
        }
        cur = await conn.execute("PRAGMA table_info(paper_trade_entry_snapshots)")
        col_rows = await cur.fetchall()
        cols = {row[1] for row in col_rows}
        missing = sorted(required_columns - cols)
        if missing:
            raise RuntimeError(
                "bl_actionability_entry_snapshot_v1 schema missing columns: "
                + ", ".join(missing)
            )

        by_name = {row[1]: row for row in col_rows}
        expected = {
            "paper_trade_id": ("INTEGER", False, None, 1),
            "entry_snapshot_version": ("TEXT", True, None, 0),
            "entry_snapshot_complete": ("INTEGER", True, None, 0),
            "entry_snapshot_missing_fields": ("TEXT", True, None, 0),
            "captured_at": ("TEXT", True, None, 0),
            "signal_type": ("TEXT", False, None, 0),
            "mcap_usd_at_entry": ("REAL", False, None, 0),
            "mcap_bucket_at_entry": ("TEXT", False, None, 0),
            "liquidity_usd_at_entry": ("REAL", False, None, 0),
            "token_age_days_at_entry": ("REAL", False, None, 0),
            "first_seen_at_at_entry": ("TEXT", False, None, 0),
            "detected_by_combo_at_entry": ("TEXT", False, None, 0),
            "source_confluence_count_at_entry": ("INTEGER", False, None, 0),
            "tg_channel_at_entry": ("TEXT", False, None, 0),
            "actionability_version_at_entry": ("TEXT", False, None, 0),
            "actionability_reason_at_entry": ("TEXT", False, None, 0),
            "actionable_at_entry": ("INTEGER", False, None, 0),
            "tp_pct_at_entry": ("REAL", False, None, 0),
            "sl_pct_at_entry": ("REAL", False, None, 0),
            "trail_pct_at_entry": ("REAL", False, None, 0),
            "trail_pct_low_peak_at_entry": ("REAL", False, None, 0),
        }
        for name, (typ, notnull, default, pk) in expected.items():
            row = by_name[name]
            if (row[2] or "").upper() != typ:
                raise RuntimeError(
                    f"bl_actionability_entry_snapshot_v1 column {name} type mismatch"
                )
            if bool(row[3]) != notnull:
                raise RuntimeError(
                    f"bl_actionability_entry_snapshot_v1 column {name} NOT NULL mismatch"
                )
            actual_default = row[4]
            if default is None:
                if actual_default is not None:
                    raise RuntimeError(
                        f"bl_actionability_entry_snapshot_v1 column {name} default mismatch"
                    )
            elif str(actual_default).strip("'\"") != default:
                raise RuntimeError(
                    f"bl_actionability_entry_snapshot_v1 column {name} default mismatch"
                )
            if int(row[5]) != pk:
                raise RuntimeError(
                    f"bl_actionability_entry_snapshot_v1 column {name} pk mismatch"
                )

        cur = await conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='paper_trade_entry_snapshots'"
        )
        create_sql_row = await cur.fetchone()
        create_sql = (create_sql_row[0] if create_sql_row else "").upper()
        compact_sql = "".join(create_sql.split())
        if "CHECK(ENTRY_SNAPSHOT_COMPLETEIN(0,1))" not in compact_sql:
            raise RuntimeError(
                "bl_actionability_entry_snapshot_v1 entry_snapshot_complete CHECK missing"
            )
        if "ONDELETERESTRICT" not in compact_sql:
            raise RuntimeError(
                "bl_actionability_entry_snapshot_v1 paper_trade_id FK action missing"
            )

        cur = await conn.execute("PRAGMA foreign_key_list(paper_trade_entry_snapshots)")
        fks = await cur.fetchall()
        has_paper_trade_fk = any(
            row[2] == "paper_trades"
            and row[3] == "paper_trade_id"
            and row[4] == "id"
            and row[6].upper() == "RESTRICT"
            for row in fks
        )
        if not has_paper_trade_fk:
            raise RuntimeError(
                "bl_actionability_entry_snapshot_v1 paper_trade_id FK missing"
            )

        if not require_indexes:
            return

        cur = await conn.execute("PRAGMA index_list(paper_trade_entry_snapshots)")
        indexes = {row[1]: bool(row[2]) for row in await cur.fetchall()}
        if indexes.get("idx_ptes_version") is not False:
            raise RuntimeError(
                "bl_actionability_entry_snapshot_v1 entry_snapshot_version index missing"
            )
        if indexes.get("idx_ptes_complete") is not False:
            raise RuntimeError(
                "bl_actionability_entry_snapshot_v1 entry_snapshot_complete index missing"
            )

    async def _migrate_scanned_at_index(
        self,
        *,
        table: str,
        column: str = "scanned_at",
        index_name: str,
        migration_name: str,
    ) -> None:
        """Add single-column timestamp index for hourly prune coverage.

        Used by BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING
        (cycle 1, column='scanned_at') and BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION
        (cycle 2, 5 tables with column in
        {'detected_at','snapshot_at','created_at','scanned_at'}).

        Each table gets its own paper_migrations entry (V4#3 fold) so disk
        failure on one doesn't roll back the others. V4#2 fold: PRAGMA
        busy_timeout (Settings.SQLITE_BUSY_TIMEOUT_MS, default 90s) covers
        concurrent readers waiting on the EXCLUSIVE write lock during
        index build.

        D8 plan-review fold (cycle 2): `column` kwarg parameterizes the
        index target; log events become 'index_migrated' /
        'index_migration_failed' (was hardcoded 'scanned_at_idx_*') so
        cycle-2 columns are grep-able via the `column=` field.
        """
        # D8 SHOULD-FIX #4 + V10 NICE-TO-HAVE: defensive guard — `column` is
        # code-supplied in all current callers, but the helper is reusable
        # across cycles. Reject anything that isn't a SQL-safe identifier.
        # Promoted from `assert` to `raise` so the guard survives `python -O`
        # (asserts are stripped under optimization).
        if not column.replace("_", "").isalnum():
            raise ValueError(f"unsafe column={column!r}")

        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn

        try:
            await conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration_name,)
            )
            row = await cur.fetchone()
            if row is not None:
                await conn.execute("COMMIT")
                return
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({column})"
            )
            await conn.execute(
                "INSERT INTO paper_migrations(name, cutover_ts) VALUES (?, ?)",
                (migration_name, datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute("COMMIT")
            _db_log.info(
                "index_migrated",
                table=table,
                column=column,
                migration=migration_name,
            )
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _db_log.exception(
                "index_migration_failed",
                table=table,
                column=column,
                migration=migration_name,
            )
            _db_log.error(
                "SCHEMA_DRIFT_DETECTED",
                table=table,
                column=column,
                migration=migration_name,
            )
            raise

    async def _migrate_score_history_scanned_at_index(self) -> None:
        """BL-NEW-SCORE-HISTORY-PRUNING migration entry point."""
        await self._migrate_scanned_at_index(
            table="score_history",
            index_name="idx_score_history_scanned_at",
            migration_name="score_history_scanned_at_idx_v1",
        )

    async def _migrate_volume_snapshots_scanned_at_index(self) -> None:
        """BL-NEW-VOLUME-SNAPSHOTS-PRUNING migration entry point."""
        await self._migrate_scanned_at_index(
            table="volume_snapshots",
            index_name="idx_volume_snapshots_scanned_at",
            migration_name="volume_snapshots_scanned_at_idx_v1",
        )

    # ------------------------------------------------------------------
    # BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (cycle 2) — 5 migration entry points
    # ------------------------------------------------------------------

    async def _migrate_volume_spikes_detected_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="volume_spikes",
            column="detected_at",
            index_name="idx_volume_spikes_detected_at",
            migration_name="volume_spikes_detected_at_idx_v1",
        )

    async def _migrate_cohort_digest_state_v1(self) -> None:
        """BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST (cycle 5) — adds:

        1. ``cohort_digest_state`` singleton table holding ``last_digest_date``
           + ``last_final_block_fired_at``. CHECK constraint enforces single
           row. Seeded with NULL+NULL on first migration so the stamp helpers
           can use ``INSERT OR REPLACE`` safely (V30 MUST-FIX).
        2. ``idx_paper_trades_closed_at`` partial index — covers the cohort
           digest's window query. Partial WHERE ``closed_at IS NOT NULL``
           skips open trades (~60-80% rowcount reduction). Custom migration
           because :meth:`_migrate_scanned_at_index` doesn't support partial
           indexes (V30 MUST-FIX).

        Both DDL statements run inside one BEGIN EXCLUSIVE; matches the
        ``_migrate_*`` pattern (single migration_name in paper_migrations).
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        migration_name = "cohort_digest_state_v1"

        try:
            await conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration_name,)
            )
            row = await cur.fetchone()
            if row is not None:
                await conn.execute("COMMIT")
                return

            # PR-review V31/V32/V33 fold: paper_trades.closed_at must exist
            # before the partial index. Pre-BL055 prod-snapshot test seeds
            # paper_trades WITHOUT closed_at; _create_tables is a no-op for
            # the existing table; no prior migration ADD-COLUMNs closed_at.
            # Add it idempotently here so the index step is safe across
            # upgrade paths. Real prod (srilu) already has the column from
            # _create_tables fresh-install path — ALTER is a no-op there.
            cur = await conn.execute("PRAGMA table_info(paper_trades)")
            paper_cols = {row[1] for row in await cur.fetchall()}
            if "closed_at" not in paper_cols:
                await conn.execute("ALTER TABLE paper_trades ADD COLUMN closed_at TEXT")

            await conn.execute("""CREATE TABLE IF NOT EXISTS cohort_digest_state (
                    marker INTEGER PRIMARY KEY DEFAULT 1,
                    last_digest_date TEXT,
                    last_final_block_fired_at TEXT,
                    CHECK (marker = 1)
                )""")
            await conn.execute("""INSERT OR IGNORE INTO cohort_digest_state
                   (marker, last_digest_date, last_final_block_fired_at)
                   VALUES (1, NULL, NULL)""")
            await conn.execute("""CREATE INDEX IF NOT EXISTS idx_paper_trades_closed_at
                   ON paper_trades(closed_at)
                   WHERE closed_at IS NOT NULL""")
            await conn.execute(
                "INSERT INTO paper_migrations(name, cutover_ts) VALUES (?, ?)",
                (migration_name, datetime.now(timezone.utc).isoformat()),
            )
            await conn.execute("COMMIT")
            _db_log.info("cohort_digest_state_migrated", migration=migration_name)
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _db_log.exception(
                "cohort_digest_state_migration_failed", migration=migration_name
            )
            _db_log.error("SCHEMA_DRIFT_DETECTED", migration=migration_name)
            raise

    async def _migrate_symbol_upper_indexes_v1(self) -> None:
        """BL-NEW-DASHBOARD-X-ALERTS-RESOLVER-INDEX: functional indexes on
        UPPER(symbol) for the two large symbol-resolver source tables.

        Why: x_alerts dashboard endpoint's symbol resolver runs
        ``SELECT DISTINCT coin_id FROM <table> WHERE UPPER(symbol) = ?``
        which is non-sargable against the existing ``(coin_id, time)``
        indexes — produces a SCAN of 2.5M-row volume_history_cg per
        unresolved cashtag (~360ms each), times ~30 distinct cashtags per
        request, totalling ~10s of the limit=80 response budget. With
        functional indexes on UPPER(symbol), the same query becomes an
        index lookup (~1-5ms).

        Both tables get separate paper_migrations entries so disk failure
        on one doesn't roll back the other. Single EXCLUSIVE transaction
        per index (held during the build itself — ~1-5s on 2.5M rows in
        WAL mode); ``busy_timeout`` (Settings.SQLITE_BUSY_TIMEOUT_MS,
        default 90s) covers concurrent writer/reader wait.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn

        targets = [
            (
                "volume_history_cg",
                "idx_vol_hist_cg_symbol_upper",
                "vol_hist_cg_symbol_upper_idx_v1",
            ),
            (
                "gainers_snapshots",
                "idx_gainers_snap_symbol_upper",
                "gainers_snap_symbol_upper_idx_v1",
            ),
        ]

        for table, index_name, migration_name in targets:
            try:
                await conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
                await conn.execute("BEGIN EXCLUSIVE")
                await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                        name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
                cur = await conn.execute(
                    "SELECT 1 FROM paper_migrations WHERE name = ?",
                    (migration_name,),
                )
                row = await cur.fetchone()
                if row is not None:
                    await conn.execute("COMMIT")
                    continue
                # Note: NOT a partial index — SQLite's planner has trouble
                # matching `COALESCE(coin_id, '') != ''` against a
                # `WHERE coin_id IS NOT NULL AND coin_id != ''` predicate
                # in the index definition, causing it to fall back to
                # SCAN. A full functional index on UPPER(symbol) matches
                # the query shape exactly.
                await conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table}(UPPER(symbol))"
                )
                await conn.execute(
                    "INSERT INTO paper_migrations(name, cutover_ts) VALUES (?, ?)",
                    (migration_name, datetime.now(timezone.utc).isoformat()),
                )
                await conn.execute("COMMIT")
                _db_log.info(
                    "symbol_upper_index_migrated",
                    table=table,
                    index_name=index_name,
                    migration=migration_name,
                )
            except Exception:
                try:
                    await conn.execute("ROLLBACK")
                except Exception as rb_err:
                    _db_log.exception(
                        "schema_migration_rollback_failed", err=str(rb_err)
                    )
                _db_log.exception(
                    "symbol_upper_index_migration_failed",
                    table=table,
                    migration=migration_name,
                )
                _db_log.error(
                    "SCHEMA_DRIFT_DETECTED",
                    migration=migration_name,
                )
                raise

    async def _migrate_gainers_comparisons_appeared_idx_v1(self) -> None:
        """Index Top Gainers comparison recency for Trade Inbox promotion."""
        await self._migrate_scanned_at_index(
            table="gainers_comparisons",
            column="appeared_on_gainers_at",
            index_name="idx_gainers_comp_appeared_at",
            migration_name="gainers_comp_appeared_at_idx_v1",
        )

    async def _migrate_gainer_acceleration_v1(self) -> None:
        """Gap-fill 2026-06-02: create gainer_acceleration + add the
        acceleration/momentum/slow_burn/velocity surface columns to
        gainers_comparisons. Idempotent: CREATE TABLE IF NOT EXISTS in
        _create_tables is a no-op for the pre-existing prod gainers_comparisons,
        so the new columns need PRAGMA-guarded ALTERs here. Additive only."""
        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        await conn.execute("""CREATE TABLE IF NOT EXISTS gainer_acceleration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                change_1h REAL,
                change_4h REAL,
                vol_expansion REAL,
                market_cap REAL,
                current_price REAL,
                detected_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gainer_accel "
            "ON gainer_acceleration(coin_id, detected_at)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gainer_accel_detected "
            "ON gainer_acceleration(detected_at)"
        )
        cur = await conn.execute("PRAGMA table_info(gainers_comparisons)")
        existing = {row[1] for row in await cur.fetchall()}
        new_cols = [
            ("detected_by_acceleration", "INTEGER DEFAULT 0"),
            ("acceleration_lead_minutes", "REAL"),
            ("detected_by_momentum", "INTEGER DEFAULT 0"),
            ("momentum_lead_minutes", "REAL"),
            ("detected_by_slow_burn", "INTEGER DEFAULT 0"),
            ("slow_burn_lead_minutes", "REAL"),
            ("detected_by_velocity", "INTEGER DEFAULT 0"),
            ("velocity_lead_minutes", "REAL"),
        ]
        added: list[str] = []
        for name, decl in new_cols:
            if name not in existing:
                await conn.execute(
                    f"ALTER TABLE gainers_comparisons ADD COLUMN {name} {decl}"
                )
                added.append(name)
        await conn.commit()
        _log.info("gainer_acceleration_migration_complete", columns_added=added)

    # --- cohort_digest state helpers (D5 fold) -------------------------------

    async def cohort_digest_read_state(self) -> dict:
        """Return current singleton-row contents.

        Returns dict with ``last_digest_date`` and ``last_final_block_fired_at``
        (both nullable). Returns NULL+NULL if the row is missing.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        cur = await self._conn.execute(
            "SELECT last_digest_date, last_final_block_fired_at "
            "FROM cohort_digest_state WHERE marker = 1"
        )
        row = await cur.fetchone()
        if row is None:
            return {"last_digest_date": None, "last_final_block_fired_at": None}
        return {"last_digest_date": row[0], "last_final_block_fired_at": row[1]}

    async def cohort_digest_stamp_last_digest_date(self, date_iso: str) -> None:
        """Stamp ``last_digest_date`` on the singleton row, preserving the
        other field (V30 MUST-FIX — sub-SELECT prevents NULL clobber)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "INSERT OR REPLACE INTO cohort_digest_state "
            "(marker, last_digest_date, last_final_block_fired_at) VALUES (1, ?, "
            "(SELECT last_final_block_fired_at FROM cohort_digest_state WHERE marker = 1))",
            (date_iso,),
        )
        await self._conn.commit()

    async def cohort_digest_stamp_final_block_fired(self, ts_iso: str) -> None:
        """Stamp ``last_final_block_fired_at`` on the singleton row,
        preserving the other field."""
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        await self._conn.execute(
            "INSERT OR REPLACE INTO cohort_digest_state "
            "(marker, last_digest_date, last_final_block_fired_at) VALUES (1, "
            "(SELECT last_digest_date FROM cohort_digest_state WHERE marker = 1), ?)",
            (ts_iso,),
        )
        await self._conn.commit()

    async def _migrate_chain_matches_completed_at_index(self) -> None:
        """V12 PR-review SHOULD-FIX #1 fold: chain_matches index promoted
        from V9 NICE-TO-HAVE deferral. Cost is 5 lines; hourly prune was
        structurally table-scanning otherwise."""
        await self._migrate_scanned_at_index(
            table="chain_matches",
            column="completed_at",
            index_name="idx_chain_matches_completed_at",
            migration_name="chain_matches_completed_at_idx_v1",
        )

    async def _migrate_chain_pattern_provenance_v1(self) -> None:
        """Add chain-pattern state provenance for protected built-ins.

        Migration is intentionally narrow: only the known 2026-05-17 prod
        snapshot is stamped as legacy lifecycle retirement. Other unknown
        inactive built-ins remain inactive with NULL reason so startup cannot
        infer operator intent from incomplete old schema state.
        """
        conn = self._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        migration_name = "bl_chain_pattern_provenance_v1"
        now_iso = datetime.now(timezone.utc).isoformat()
        await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                name TEXT PRIMARY KEY,
                cutover_ts TEXT NOT NULL
            )""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )""")
        cur = await conn.execute("PRAGMA table_info(chain_patterns)")
        cols = {row[1] for row in await cur.fetchall()}
        if "is_protected_builtin" not in cols:
            await conn.execute(
                "ALTER TABLE chain_patterns ADD COLUMN "
                "is_protected_builtin INTEGER NOT NULL DEFAULT 0"
            )
        if "disabled_reason" not in cols:
            await conn.execute(
                "ALTER TABLE chain_patterns ADD COLUMN disabled_reason TEXT"
            )
        if "disabled_at" not in cols:
            await conn.execute("ALTER TABLE chain_patterns ADD COLUMN disabled_at TEXT")

        builtins = ("full_conviction", "narrative_momentum", "volume_breakout")
        await conn.execute(
            f"""UPDATE chain_patterns
                SET is_protected_builtin = 1
                WHERE name IN ({','.join('?' for _ in builtins)})""",
            builtins,
        )

        snapshot = {
            "full_conviction": (52, 2),
            "narrative_momentum": (58, 2),
            "volume_breakout": (70, 3),
        }
        cur = await conn.execute(
            f"""SELECT name, is_active, total_triggers, total_hits, updated_at,
                       disabled_reason
                FROM chain_patterns
                WHERE name IN ({','.join('?' for _ in builtins)})
                ORDER BY name""",
            builtins,
        )
        rows = await cur.fetchall()
        by_name = {row[0]: row for row in rows}
        snapshot_matches = len(by_name) == 3
        for name, (triggers, hits) in snapshot.items():
            row = by_name.get(name)
            snapshot_matches = snapshot_matches and row is not None
            if row is None:
                continue
            snapshot_matches = (
                snapshot_matches
                and int(row[1]) == 0
                and int(row[2] or 0) == triggers
                and int(row[3] or 0) == hits
                and str(row[4]) == "2026-05-17 01:24:59"
                and row[5] is None
            )
        if snapshot_matches:
            await conn.execute(
                f"""UPDATE chain_patterns
                    SET disabled_reason = 'legacy_lifecycle_retired',
                        disabled_at = COALESCE(updated_at, ?)
                    WHERE name IN ({','.join('?' for _ in builtins)})
                      AND disabled_reason IS NULL""",
                (now_iso, *builtins),
            )

        await conn.execute(
            "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
            (migration_name, now_iso),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (20260520, now_iso, migration_name),
        )
        await conn.commit()
        _db_log.info("chain_pattern_provenance_migrated", migration=migration_name)

    async def _migrate_momentum_7d_detected_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="momentum_7d",
            column="detected_at",
            index_name="idx_momentum_7d_detected_at",
            migration_name="momentum_7d_detected_at_idx_v1",
        )

    async def _migrate_trending_snapshots_snapshot_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="trending_snapshots",
            column="snapshot_at",
            index_name="idx_trending_snapshots_snapshot_at",
            migration_name="trending_snapshots_snapshot_at_idx_v1",
        )

    async def _migrate_learn_logs_created_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="learn_logs",
            column="created_at",
            index_name="idx_learn_logs_created_at",
            migration_name="learn_logs_created_at_idx_v1",
        )

    async def _migrate_holder_snapshots_scanned_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="holder_snapshots",
            column="scanned_at",
            index_name="idx_holder_snapshots_scanned_at",
            migration_name="holder_snapshots_scanned_at_idx_v1",
        )

    async def record_minara_alert_emission(
        self,
        *,
        paper_trade_id: int | None,
        tg_alert_log_id: int | None,
        signal_type: str,
        coin_id: str,
        chain: str,
        amount_usd: float,
        command_text: str | None,
        emitted_at: str | None = None,
        source_event_id: str | None = None,
        source: str = "live",
        lock_timeout_sec: float | None = None,
    ) -> bool:
        """Persist one Minara command-emission audit row.

        Returns True when inserted, False when a duplicate idempotency key
        was ignored.
        """
        if self._conn is None or self._txn_lock is None:
            raise RuntimeError("Database not initialized.")
        if source_event_id is None:
            if tg_alert_log_id is None:
                raise ValueError(
                    "source_event_id is required when tg_alert_log_id is absent"
                )
            source_event_id = f"tg_alert_log:{tg_alert_log_id}"
        if emitted_at is None:
            emitted_at = datetime.now(timezone.utc).isoformat()
        command_hash = (
            hashlib.sha256(command_text.encode("utf-8")).hexdigest()
            if command_text is not None
            else None
        )
        command_text_observed = 1 if command_text is not None else 0

        lock_acquired = False
        if lock_timeout_sec is None:
            await self._txn_lock.acquire()
        else:
            await asyncio.wait_for(self._txn_lock.acquire(), timeout=lock_timeout_sec)
        lock_acquired = True
        try:
            await self._conn.execute(
                """INSERT INTO minara_alert_emissions (
                    paper_trade_id, tg_alert_log_id, signal_type, coin_id,
                    chain, amount_usd, command_text, command_hash,
                    command_text_observed, source, source_event_id,
                    emitted_at, operator_paste_acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    paper_trade_id,
                    tg_alert_log_id,
                    signal_type,
                    coin_id,
                    chain,
                    amount_usd,
                    command_text,
                    command_hash,
                    command_text_observed,
                    source,
                    source_event_id,
                    emitted_at,
                ),
            )
            await self._conn.commit()
            return True
        except sqlite3.IntegrityError as exc:
            try:
                await self._conn.rollback()
            except Exception as rb_err:
                _db_log.exception("connection_rollback_failed", err=str(rb_err))
            msg = str(exc)
            if "UNIQUE constraint failed" in msg and (
                "minara_alert_emissions.source_event_id" in msg
                or "minara_alert_emissions.tg_alert_log_id" in msg
            ):
                return False
            raise
        except BaseException:
            try:
                await self._conn.rollback()
            except Exception as rb_err:
                _db_log.exception("connection_rollback_failed", err=str(rb_err))
            raise
        finally:
            if lock_acquired:
                self._txn_lock.release()

    async def record_tg_alert_operator_action(
        self,
        *,
        tg_alert_log_id: int,
        action: str,
        note: str | None,
        source: str = "dashboard",
    ) -> dict:
        """Record the operator's current label for a delivered TG alert."""
        if self._conn is None or self._txn_lock is None:
            raise RuntimeError("Database not initialized.")
        allowed_actions = {"acted", "useful", "ignored", "false_positive"}
        if action not in allowed_actions:
            raise ValueError(f"invalid operator action: {action}")
        allowed_sources = {"dashboard", "api", "backfill"}
        if source not in allowed_sources:
            raise ValueError(f"invalid operator action source: {source}")

        clean_note = note.strip() if note else None
        if clean_note:
            clean_note = clean_note[:500]
        now_iso = datetime.now(timezone.utc).isoformat()
        txn_started = False

        await self._txn_lock.acquire()
        try:
            cur = await self._conn.execute(
                "SELECT id, paper_trade_id, signal_type, token_id, alerted_at "
                "FROM tg_alert_log WHERE id = ? AND outcome = 'sent'",
                (tg_alert_log_id,),
            )
            alert = await cur.fetchone()
            if alert is None:
                raise KeyError(f"sent tg_alert_log row not found: {tg_alert_log_id}")

            await self._conn.execute("BEGIN IMMEDIATE")
            txn_started = True
            await self._conn.execute(
                """INSERT INTO tg_alert_operator_actions (
                    tg_alert_log_id, paper_trade_id, token_id, signal_type,
                    alerted_at, action, note, source, marked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_alert_log_id) DO UPDATE SET
                    action = excluded.action,
                    note = excluded.note,
                    source = excluded.source,
                    updated_at = excluded.updated_at""",
                (
                    alert["id"],
                    alert["paper_trade_id"],
                    alert["token_id"],
                    alert["signal_type"],
                    alert["alerted_at"],
                    action,
                    clean_note,
                    source,
                    now_iso,
                    now_iso,
                ),
            )
            await self._conn.commit()
            txn_started = False
            cur = await self._conn.execute(
                """SELECT id, tg_alert_log_id, paper_trade_id, token_id,
                          signal_type, alerted_at, action, note, source,
                          marked_at, updated_at
                   FROM tg_alert_operator_actions
                   WHERE tg_alert_log_id = ?""",
                (tg_alert_log_id,),
            )
            row = await cur.fetchone()
            return dict(row)
        except BaseException:
            if txn_started:
                try:
                    await self._conn.rollback()
                except Exception as rb_err:
                    _db_log.exception("connection_rollback_failed", err=str(rb_err))
            raise
        finally:
            self._txn_lock.release()

    async def revive_signal_with_baseline(
        self,
        signal_type: str,
        *,
        reason: str,
        operator: str = "operator",
        force: bool = False,
        settings: "Settings | None" = None,
        restore_tg_alert_eligible: bool | None = None,
    ) -> None:
        """Atomic operator revival: enabled=1, stamp drawdown_baseline_at=NOW(),
        write audit row.

        Used by operator dashboards / scripts to revive a previously-suspended
        signal without having historical drawdown immediately re-trip the rule.
        The baseline anchors the auto_suspend rolling window to the revival
        instant; pre-revival drawdown is excluded.

        BL-NEW-REVIVAL-COOLOFF (2026-05-06): enforces a configurable cool-off
        between consecutive operator revivals of the same signal to prevent
        immortalization via repeated baseline-stamping. Default cool-off is
        ``Settings.SIGNAL_REVIVAL_MIN_SOAK_DAYS`` (7 days); set to 0 to
        disable. Pass ``force=True`` to bypass per-call (an audit-row marker
        + structlog WARNING ``revive_signal_force_bypass`` are emitted).

        Args:
            signal_type: signal_params row to revive
            reason: audit-row reason text
            operator: applied_by field; defaults to ``"operator"``. The cool-off
                only triggers on prior rows with ``applied_by='operator'``.
                Automated paths that should not trip the cool-off should pass
                a different operator value.
            force: bypass the cool-off; tags audit row + emits WARNING.
            settings: optional Settings instance for dependency injection
                (per CLAUDE.md "no global state"). Falls back to
                ``get_settings()`` if not passed.
            restore_tg_alert_eligible: controls the joint TG-eligibility
                restore. ``None`` (default) keeps the historical behaviour —
                restore to 1 for signals in ``DEFAULT_ALLOW_SIGNALS``, 0
                otherwise. **A paper-only or instrumentation revival must pass
                ``False``**: it authorises ``enabled=1`` and nothing else, and
                the default would silently re-enable operator alerting.
                Restoring to 1 always logs at WARNING.

        Raises:
            ValueError: if signal_type is unknown, OR if the cool-off
                window is active and ``force=False``.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn

        # BL-NEW-REVIVAL-COOLOFF cool-off check (skipped on force=True).
        # Positive filter applied_by='operator' (per plan-stage reviewer
        # MUST-FIX) to avoid future calibrate / dashboard / other applied_by
        # values inadvertently triggering the cool-off. Single SELECT
        # before branch (per PR-stage code reviewer Issue 3) — both
        # force=True and force=False paths read the same prior-revival
        # row; only the cool-off comparison branches.
        cur = await conn.execute(
            """SELECT applied_at FROM signal_params_audit
               WHERE signal_type = ?
                 AND field_name = 'enabled'
                 AND old_value = '0'
                 AND new_value = '1'
                 AND applied_by = 'operator'
               ORDER BY applied_at DESC LIMIT 1""",
            (signal_type,),
        )
        row = await cur.fetchone()
        prior_revival_at: str | None = row[0] if row is not None else None

        if not force and prior_revival_at is not None:
            if settings is None:
                from scout.config import get_settings

                settings = get_settings()
            # Direct attribute access (per PR-stage strategy reviewer
            # RECOMMEND): single source of truth for the default in
            # scout/config.py. AttributeError on a malformed Settings
            # stub is a louder failure than a silently-stale 7.
            cool_off_days = settings.SIGNAL_REVIVAL_MIN_SOAK_DAYS
            if cool_off_days > 0:
                last_at = datetime.fromisoformat(prior_revival_at)
                delta = datetime.now(timezone.utc) - last_at
                if delta < timedelta(days=cool_off_days):
                    days_elapsed = int(delta.total_seconds() // 86400)
                    days_remaining = max(0, cool_off_days - days_elapsed)
                    raise ValueError(
                        f"revive_signal_with_baseline cool-off: "
                        f"{signal_type} was last revived at {prior_revival_at} "
                        f"({days_elapsed} days ago); minimum "
                        f"{cool_off_days} days required between "
                        f"consecutive revivals "
                        f"({days_remaining} days remaining). "
                        f"Pass force=True to bypass: "
                        f"db.revive_signal_with_baseline("
                        f"'{signal_type}', reason='...', force=True)"
                    )

        # Force-bypass observability hook (per design-stage reviewer
        # RECOMMEND): emit WARNING only when the bypass actually overrode
        # something — i.e., a prior recent revival exists. force=True on
        # first-ever revival logs at INFO instead, to avoid noisy WARNINGs
        # from defensive operator-scripting habits.
        if force:
            import structlog as _structlog

            _logger = _structlog.get_logger("scout.db")
            if prior_revival_at is not None:
                _logger.warning(
                    "revive_signal_force_bypass",
                    signal_type=signal_type,
                    operator=operator,
                    reason=reason,
                    prior_revival_at=prior_revival_at,
                )
            else:
                _logger.info(
                    "revive_signal_force_no_prior",
                    signal_type=signal_type,
                    operator=operator,
                    reason=reason,
                )

        audit_reason = (
            f"{reason} [force=True bypass of revival cool-off]" if force else reason
        )

        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            cur = await conn.execute(
                "SELECT enabled FROM signal_params WHERE signal_type = ?",
                (signal_type,),
            )
            row = await cur.fetchone()
            if row is None:
                # Roll back the empty txn before raising so the connection
                # state is clean for the caller.
                await conn.execute("ROLLBACK")
                raise ValueError(f"unknown signal_type: {signal_type}")
            old_enabled = row[0]

            # R2-I1 design fold: restore tg_alert_eligible=1 jointly with
            # enabled if signal is in DEFAULT_ALLOW_SIGNALS. Auto-suspend
            # cleared both flags; revive restores both for default-allow
            # signals so operator doesn't have to manually re-enable
            # alerting after a Tier 1b cycle.
            #
            # `restore_tg_alert_eligible` overrides that default. A paper-only
            # or instrumentation revival authorises `enabled=1` and NOTHING
            # else, so it must pass False — otherwise this fold silently turns
            # operator-facing alerting back on for any default-allow signal.
            # That happened to `gainers_early` on 2026-08-03: the run was
            # authorised paper-only with tg_alert_eligible=0 and came back with
            # it at 1, unnoticed until a state reconciliation caught it.
            from scout.trading.tg_alert_dispatch import DEFAULT_ALLOW_SIGNALS

            in_default_allow = signal_type in DEFAULT_ALLOW_SIGNALS
            if restore_tg_alert_eligible is None:
                restore_eligible = 1 if in_default_allow else 0
                explicit = False
            else:
                restore_eligible = 1 if restore_tg_alert_eligible else 0
                explicit = True

            # V3-I3 PR-stage fold: log decision so operator sees why
            # non-default-allow opt-in wasn't restored after revive.
            #
            # Restoring to 1 logs at WARNING, never info: turning alerting back
            # on is an operator-visible state change and must not be inferable
            # only from an absent log line.
            _log_tg = _db_log.warning if restore_eligible == 1 else _db_log.info
            _log_tg(
                "signal_revived_tg_eligible",
                signal_type=signal_type,
                restored_to=restore_eligible,
                in_default_allow=in_default_allow,
                explicitly_requested=explicit,
            )
            await conn.execute(
                """UPDATE signal_params
                   SET enabled = 1,
                       tg_alert_eligible = ?,
                       suspended_at = NULL,
                       suspended_reason = NULL,
                       drawdown_baseline_at = ?,
                       updated_at = ?,
                       updated_by = ?
                   WHERE signal_type = ?""",
                (restore_eligible, now_iso, now_iso, operator, signal_type),
            )
            await conn.execute(
                """INSERT INTO signal_params_audit
                   (signal_type, field_name, old_value, new_value,
                    reason, applied_by, applied_at)
                   VALUES (?, 'enabled', ?, '1', ?, ?, ?)""",
                (signal_type, str(old_enabled), audit_reason, operator, now_iso),
            )
            await conn.execute(
                """INSERT INTO signal_params_audit
                   (signal_type, field_name, old_value, new_value,
                    reason, applied_by, applied_at)
                   VALUES (?, 'tg_alert_eligible', '0', ?, ?, ?, ?)""",
                (
                    signal_type,
                    str(restore_eligible),
                    f"revive joint flag: {audit_reason}",
                    operator,
                    now_iso,
                ),
            )
            # Combo-level revival: signal_params.enabled is only the FIRST
            # entry gate. The base combo (combo_key == signal_type) carries an
            # INDEPENDENT suppression in combo_performance that survives a
            # signal-level revival, silently keeping the revived signal
            # un-tradeable until parole_at (up to FEEDBACK_PAROLE_DAYS out).
            # Mirror the signal-level baseline reset here: open the parole
            # window NOW and refresh the retest allowance, keeping
            # suppressed=1 (bounded retest, not full exoneration — combo_refresh
            # re-evaluates on the fresh trades). Only the base combo is touched;
            # multi-signal combos must re-prove on their own merits.
            cur = await conn.execute(
                "SELECT 1 FROM combo_performance "
                "WHERE combo_key = ? AND window = '30d' AND suppressed = 1",
                (signal_type,),
            )
            if await cur.fetchone() is not None:
                # Resolve settings lazily — only when there is actually a
                # suppressed base combo to parole (mirrors the cool-off
                # branch above), so callers reviving a signal with no
                # suppressed combo need not provide / construct Settings.
                if settings is None:
                    from scout.config import get_settings

                    settings = get_settings()
                retest = settings.FEEDBACK_PAROLE_RETEST_TRADES
                await conn.execute(
                    "UPDATE combo_performance "
                    "SET parole_at = ?, parole_trades_remaining = ? "
                    "WHERE combo_key = ? AND window = '30d' AND suppressed = 1",
                    (now_iso, retest, signal_type),
                )
                _db_log.info(
                    "revive_signal_combo_paroled",
                    signal_type=signal_type,
                    parole_at=now_iso,
                    parole_trades_remaining=retest,
                )
            await conn.commit()
        except ValueError:
            raise
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _db_log.exception("schema_migration_rollback_failed", err=str(rb_err))
            raise

    # ------------------------------------------------------------------
    # Candidates
    # ------------------------------------------------------------------

    async def upsert_candidate(self, token: CandidateToken) -> None:
        """Insert or update candidate by contract_address.

        Uses SQLite UPSERT (``ON CONFLICT(contract_address) DO UPDATE``)
        instead of ``INSERT OR REPLACE`` so that columns NOT present in
        ``_CANDIDATE_COLUMNS`` are PRESERVED on conflict — specifically
        the 4 liquidity-enrichment columns written by the Phase 1a-ii
        cron (``liquidity_usd_enriched``, ``liquidity_enriched_source``,
        ``liquidity_enriched_at``, ``liquidity_enriched_confidence``).

        Before this change: ``INSERT OR REPLACE`` deleted the row and
        re-inserted with only ``_CANDIDATE_COLUMNS``, silently clobbering
        cron writes to NULL on every re-ingest. After this change: the
        4 enrichment columns survive across re-ingest because they are
        NOT in the ``DO UPDATE SET`` clause.

        For first-insert (no conflict), enrichment columns get NULL —
        correct (the cron has not visited yet). Existing behavior for
        all ``_CANDIDATE_COLUMNS`` is preserved verbatim.

        See ``tasks/design_liquidity_enrichment_b2_2026_05_29.md`` for
        the decoupled-columns design rationale.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        placeholders = ", ".join("?" for _ in _CANDIDATE_COLUMNS)
        cols = ", ".join(_CANDIDATE_COLUMNS)
        # DO UPDATE SET excludes contract_address (PK) and the 4 enrichment
        # columns (preserved). first_seen_at is an earliest-sighting contract:
        # keep the older timestamp so re-ingest cannot make a token look new.
        update_assignments = []
        for col in _CANDIDATE_COLUMNS:
            if col == "contract_address":
                continue
            if col == "first_seen_at":
                update_assignments.append(
                    "first_seen_at=CASE "
                    "WHEN datetime(excluded.first_seen_at) "
                    "< datetime(candidates.first_seen_at) "
                    "THEN excluded.first_seen_at "
                    "ELSE candidates.first_seen_at END"
                )
            else:
                update_assignments.append(f"{col}=excluded.{col}")
        update_clause = ", ".join(update_assignments)
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
            f"INSERT INTO candidates ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(contract_address) DO UPDATE SET {update_clause}",
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

        Uses INSERT ... ON CONFLICT DO UPDATE with COALESCE on price_change_7d
        so callers that don't supply 7d-change (notably the held-position
        refresh lane, which uses /simple/price) preserve any existing 7d value
        rather than nulling it out. No-op for /coins/markets callers (they
        always supply 7d). See tasks/plan_held_position_price_freshness.md.
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
                """INSERT INTO price_cache
                   (coin_id, current_price, price_change_24h, price_change_7d,
                    market_cap, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(coin_id) DO UPDATE SET
                     current_price = excluded.current_price,
                     price_change_24h = excluded.price_change_24h,
                     price_change_7d = COALESCE(
                       excluded.price_change_7d, price_cache.price_change_7d),
                     market_cap = excluded.market_cap,
                     updated_at = excluded.updated_at""",
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

    async def prune_score_history(self, *, keep_days: int) -> int:
        """Delete ``score_history`` rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM score_history WHERE scanned_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_volume_snapshots(
        self, *, keep_days: int, batch_size: int = 50_000
    ) -> int:
        """Delete ``volume_snapshots`` rows older than ``keep_days``.

        Batched, and the batching is not premature. Ruling B drops retention
        21 -> 14, which makes the FIRST prune after cutover delete 3,283,124
        rows -- 39% of the table -- where the steady state is ~17K/hour. A
        single DELETE of that size holds the write lock while it rewrites the
        table plus both indexes; the read-only index scan alone measures 3.9s
        on prod, so the write can plausibly exceed the 90s ``busy_timeout`` and
        cascade ``database is locked`` into the pipeline sharing this
        connection -- the same failure shape as the Solana watchdog incident.

        Batching trades atomicity for bounded lock-hold time, which is the
        right trade for a retention prune: a partially-completed prune is
        indistinguishable from one that ran an hour earlier, and the next pass
        finishes it.

        Returns the total rows deleted across batches.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        total = 0
        # Bound the loop independently of the exit condition: a driver that
        # reports rowcount as -1 would otherwise spin forever.
        for _ in range(10_000):
            cur = await self._conn.execute(
                "DELETE FROM volume_snapshots WHERE rowid IN ("
                "  SELECT rowid FROM volume_snapshots WHERE scanned_at <= ? LIMIT ?)",
                (cutoff, batch_size),
            )
            await self._conn.commit()
            deleted = cur.rowcount or 0
            if deleted <= 0:
                break
            total += deleted
            if deleted < batch_size:
                break
        return total

    async def prune_trade_decision_events(self, *, keep_days: int) -> int:
        """Delete ``trade_decision_events`` rows older than ``keep_days`` (INF-02).

        ``created_at`` is written via ``datetime.now(timezone.utc).isoformat()``
        (scout/trading/decision_events.py), so the isoformat cutoff compares
        order-correctly. Returns rowcount.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM trade_decision_events WHERE created_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_volume_history_cg(self, *, keep_days: int) -> int:
        """Delete ``volume_history_cg`` rows older than ``keep_days`` (INF-06).

        Independent of VOLUME_SPIKE_ENABLED (which gates the detector's own 7d
        prune). ``recorded_at`` is written via ``.isoformat()``
        (scout/spikes/detector.py), so the isoformat cutoff compares
        order-correctly. Returns rowcount.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM volume_history_cg WHERE recorded_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Observability — measurement-only methods (no DB mutations)
    # BL-NEW-SQLITE-WAL-PROFILE (cycle 4)
    # ------------------------------------------------------------------

    async def probe_wal_state(self) -> dict:
        """Read SQLite WAL + DB size pragmas for observability.

        BL-NEW-SQLITE-WAL-PROFILE cycle 4. Called hourly from
        scout.main._run_hourly_maintenance to detect WAL bloat. Returns
        a structured dict for log emission; values are near-real-time and
        may lag pending writes by ms-scale.

        V20 PR-review MUST-FIX #1: `PRAGMA wal_autocheckpoint` (no arg)
        has a documented checkpoint side effect — it triggers a passive
        checkpoint if the page-count threshold is currently exceeded.
        We use the table-valued `pragma_wal_autocheckpoint` form to read
        the value WITHOUT side effects. Per SQLite docs:
        https://www.sqlite.org/pragma.html#pragma_wal_autocheckpoint

        V23 M1 fold: `shm_size_bytes` is INFORMATIONAL ONLY. SQLite
        resizes the -shm sidecar in 32KB increments tracking concurrent
        reader count; it does NOT indicate WAL bloat and is NOT part of
        the TUNE trigger. The `sqlite_wal_bloat_observed` event in
        scout.main gates only on `wal_size_bytes`.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        import os

        async def _pragma(name: str) -> object:
            cur = await self._conn.execute(f"PRAGMA {name}")
            row = await cur.fetchone()
            return row[0] if row else None

        # V20 MUST-FIX #2: defensive .lower() in case driver normalization
        # differs from SQLite's documented lowercase return.
        jm_raw = await _pragma("journal_mode")
        journal_mode = str(jm_raw).lower() if jm_raw is not None else None
        page_count = int(await _pragma("page_count") or 0)
        page_size = int(await _pragma("page_size") or 4096)
        freelist_count = int(await _pragma("freelist_count") or 0)

        # V20 MUST-FIX #1: prefer the table-valued `pragma_wal_autocheckpoint`
        # form (pure read, no side effect). Fall back to `PRAGMA wal_autocheckpoint`
        # (which can trigger a passive checkpoint as a side effect, per
        # SQLite docs) on builds compiled without SQLITE_INTROSPECTION_PRAGMAS
        # (e.g., Python stdlib sqlite3 on some Windows distributions). The
        # fallback's side-effect is the same passive checkpoint that fires
        # at the 1000-page autocheckpoint threshold anyway — just clock-shifted
        # to the hourly probe moment. Acceptable per V20's fold note.
        try:
            cur = await self._conn.execute("SELECT * FROM pragma_wal_autocheckpoint")
            ac_row = await cur.fetchone()
            wal_autocheckpoint = int(ac_row[0]) if ac_row else 0
        except Exception:
            ac_val = await _pragma("wal_autocheckpoint")
            wal_autocheckpoint = int(ac_val) if ac_val is not None else 0

        # WAL + SHM sidecar sizes from filesystem (atomic stat syscalls).
        # V24 SHOULD-FIX: single try/except getsize avoids TOCTOU race —
        # SQLite autocheckpoint can truncate/remove the -wal sidecar between
        # exists() and getsize(); the two-stage check turns a benign race into
        # an OSError that bubbles to the hourly hook's try-except and emits a
        # spurious sqlite_wal_probe_failed event each hour under churn.
        wal_path = self._db_path + "-wal"
        shm_path = self._db_path + "-shm"
        try:
            wal_size_bytes = os.path.getsize(wal_path)
        except OSError:
            wal_size_bytes = 0
        try:
            shm_size_bytes = os.path.getsize(shm_path)
        except OSError:
            shm_size_bytes = 0
        wal_pages = wal_size_bytes // page_size if page_size else 0

        return {
            "wal_size_bytes": wal_size_bytes,
            "wal_pages": wal_pages,
            "shm_size_bytes": shm_size_bytes,
            "db_size_bytes": page_count * page_size,
            "page_count": page_count,
            "page_size": page_size,
            "freelist_count": freelist_count,
            "journal_mode": journal_mode,
            "wal_autocheckpoint": wal_autocheckpoint,
        }

    # ------------------------------------------------------------------
    # BL-NEW-SQLITE-DURABLE-MAINTENANCE (P0 Part B): active remediation
    # ------------------------------------------------------------------

    async def checkpoint_wal_truncate(self) -> dict:
        """Run ``PRAGMA wal_checkpoint(TRUNCATE)``; return the result tuple.

        Returns ``{busy, log_frames, checkpointed_frames}``. ``busy != 0``
        means the WAL could NOT be fully checkpointed/truncated (a reader is
        pinning frames) — callers MUST treat ``busy != 0`` as not-success
        (it is the silent-failure mode behind the 2026-06-18 WAL bloat).
        """
        if self._conn is None or self._txn_lock is None:
            raise RuntimeError("Database not initialized")
        async with self._txn_lock:
            cur = await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = await cur.fetchone()
        if not row:
            return {"busy": 1, "log_frames": 0, "checkpointed_frames": 0}
        return {
            "busy": int(row[0]),
            "log_frames": int(row[1]),
            "checkpointed_frames": int(row[2]),
        }

    async def run_incremental_vacuum(self, max_pages: int = 0) -> dict:
        """Reclaim freelist pages via ``PRAGMA incremental_vacuum``.

        Requires ``auto_vacuum=INCREMENTAL`` (else a no-op: 0 reclaimed).
        ``max_pages=0`` reclaims all freelist pages; ``>0`` caps the work.

        NOTE: ``incremental_vacuum`` returns one result row per freed page and
        is driven by *stepping* the result, so a bare ``execute()`` frees only
        ONE page. We ``fetchall()`` to drive the statement to completion; the
        ``(N)`` argument caps the page count (verified on SQLite 3.50.4).
        Returns ``{auto_vacuum, freelist_before, freelist_after, pages_reclaimed}``.
        """
        if self._conn is None or self._txn_lock is None:
            raise RuntimeError("Database not initialized")

        async def _pragma_int(name: str) -> int:
            cur = await self._conn.execute(f"PRAGMA {name}")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

        async with self._txn_lock:
            auto_vacuum = await _pragma_int("auto_vacuum")
            before = await _pragma_int("freelist_count")
            if auto_vacuum == 2 and before > 0:
                if max_pages > 0:
                    cur = await self._conn.execute(
                        f"PRAGMA incremental_vacuum({int(max_pages)})"
                    )
                else:
                    cur = await self._conn.execute("PRAGMA incremental_vacuum")
                await cur.fetchall()  # drive the pragma (1 result row per freed page)
                await self._conn.commit()
            after = await _pragma_int("freelist_count")
        return {
            "auto_vacuum": auto_vacuum,
            "freelist_before": before,
            "freelist_after": after,
            "pages_reclaimed": before - after,
        }

    # ------------------------------------------------------------------
    # BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (cycle 2): 6 prune methods
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # BL-NEW-CONVICTION-PROSPECTIVE-SCORE (V1): watchlist snapshot store
    # ------------------------------------------------------------------

    async def insert_conviction_watchlist_snapshot(
        self, rows: list[dict], snapshot_at: str
    ) -> int:
        """Insert one snapshot batch (all rows share ``snapshot_at``). Returns count.
        ``contributing_surfaces`` (list) + ``first_detection_ages`` (dict) are JSON-encoded.
        """
        if self._conn is None or self._txn_lock is None:
            raise RuntimeError("Database not initialized")
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        params = [
            (
                snapshot_at,
                r["coin_id"],
                r["symbol"],
                r["name"],
                int(r["early_count"]),
                int(r["fresh_count"]),
                r["tier"],
                json.dumps(r.get("contributing_surfaces") or []),
                r.get("market_cap"),
                r.get("mcap_age_minutes"),
                json.dumps(r.get("first_detection_ages") or {}),
                now,
            )
            for r in rows
        ]
        async with self._txn_lock:
            await self._conn.executemany(
                """INSERT INTO conviction_watchlist_snapshots
                   (snapshot_at, coin_id, symbol, name, early_count, fresh_count,
                    tier, contributing_surfaces, market_cap, mcap_age_minutes,
                    first_detection_ages, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            await self._conn.commit()
        return len(params)

    async def latest_conviction_watchlist_snapshot_at(self) -> str | None:
        """Most recent ``snapshot_at`` in the table, or None if empty."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cur = await self._conn.execute(
            "SELECT MAX(snapshot_at) FROM conviction_watchlist_snapshots"
        )
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

    async def get_conviction_watchlist_rows(self, snapshot_at: str) -> list[dict]:
        """Decoded rows for a specific snapshot batch (empty if none)."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cur = await self._conn.execute(
            """SELECT snapshot_at, coin_id, symbol, name, early_count, fresh_count,
                      tier, contributing_surfaces, market_cap, mcap_age_minutes,
                      first_detection_ages
               FROM conviction_watchlist_snapshots
               WHERE snapshot_at = ?
               ORDER BY early_count DESC, coin_id ASC""",
            (snapshot_at,),
        )
        out: list[dict] = []
        for r in await cur.fetchall():
            out.append(
                {
                    "snapshot_at": r[0],
                    "coin_id": r[1],
                    "symbol": r[2],
                    "name": r[3],
                    "early_count": r[4],
                    "fresh_count": r[5],
                    "tier": r[6],
                    "contributing_surfaces": json.loads(r[7]) if r[7] else [],
                    "market_cap": r[8],
                    "mcap_age_minutes": r[9],
                    "first_detection_ages": json.loads(r[10]) if r[10] else {},
                }
            )
        return out

    async def get_latest_conviction_watchlist(self) -> list[dict]:
        """Rows of the latest snapshot batch (by max ``snapshot_at``). Empty if none."""
        latest = await self.latest_conviction_watchlist_snapshot_at()
        if latest is None:
            return []
        return await self.get_conviction_watchlist_rows(latest)

    # ---- run heartbeat (Fold A: distinguish "ran, 0 rows" from "never ran") ----

    async def insert_conviction_watchlist_run(self, run: dict) -> None:
        """Record one builder run. Written EVERY run (incl. 0-row + fail-closed),
        so freshness keys off run_at, not the latest snapshot row."""
        if self._conn is None or self._txn_lock is None:
            raise RuntimeError("Database not initialized")
        async with self._txn_lock:
            await self._conn.execute(
                """INSERT INTO conviction_watchlist_runs
                   (run_at, status, rows_written, high_tier, sub30m_high_fresh,
                    per_surface_contrib, truncated, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run["run_at"],
                    run.get("status", "ok"),
                    int(run.get("rows_written", 0)),
                    int(run.get("high_tier", 0)),
                    int(run.get("sub30m_high_fresh", 0)),
                    json.dumps(run.get("per_surface_contrib") or {}),
                    1 if run.get("truncated") else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await self._conn.commit()

    async def latest_conviction_watchlist_run_at(self) -> str | None:
        """Most recent builder ``run_at`` (the freshness key), or None if never run."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cur = await self._conn.execute(
            "SELECT MAX(run_at) FROM conviction_watchlist_runs"
        )
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

    async def latest_conviction_watchlist_run(self) -> dict | None:
        """Most recent builder run (run_at + status + counts), or None if never run.
        The watchdog keys off this: a fresh run_at with a non-'ok' status (failed /
        skipped_exclusion_failed) is a real DOWN, not a healthy build."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cur = await self._conn.execute(
            """SELECT run_at, status, rows_written, high_tier, sub30m_high_fresh,
                      truncated
               FROM conviction_watchlist_runs
               ORDER BY run_at DESC, id DESC
               LIMIT 1"""
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "run_at": row[0],
            "status": row[1],
            "rows_written": row[2],
            "high_tier": row[3],
            "sub30m_high_fresh": row[4],
            "truncated": bool(row[5]),
        }

    async def prune_conviction_watchlist_snapshots(self, *, keep_days: int) -> int:
        """Delete watchlist snapshot rows AND run heartbeats older than ``keep_days``.
        Returns the snapshot rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM conviction_watchlist_snapshots WHERE snapshot_at <= ?",
            (cutoff,),
        )
        deleted = cur.rowcount or 0
        await self._conn.execute(
            "DELETE FROM conviction_watchlist_runs WHERE run_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return deleted

    async def prune_volume_spikes(self, *, keep_days: int) -> int:
        """Delete ``volume_spikes`` rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM volume_spikes WHERE detected_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_momentum_7d(self, *, keep_days: int) -> int:
        """Delete ``momentum_7d`` rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM momentum_7d WHERE detected_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_trending_snapshots(self, *, keep_days: int) -> int:
        """Delete ``trending_snapshots`` rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM trending_snapshots WHERE snapshot_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_learn_logs(self, *, keep_days: int) -> int:
        """Delete ``learn_logs`` rows older than ``keep_days``. Returns rowcount.

        PR-review fold: unlike the other narrative-owned tables (which all
        write Python ``.isoformat()`` strings, e.g. ``2026-05-16T20:03:22+00:00``),
        ``learn_logs.created_at`` defaults to SQLite's ``datetime('now')`` at
        schema declaration (``YYYY-MM-DD HH:MM:SS``, space separator, no tz).
        Both narrative learner writers at ``scout/narrative/learner.py:291,436``
        rely on the DEFAULT and don't pass ``created_at`` explicitly. Mixed
        formats break raw lexical comparison: space (0x20) sorts before 'T'
        (0x54), so ``"2026-05-16 23:59:59"`` is lexically LESS than
        ``"2026-05-16T20:03:22..."`` and a same-day-later row would be deleted
        early. Emit the cutoff in the SQLite format so both sides match;
        lexical order then equals chronological order and the
        ``idx_learn_logs_created_at`` index is still usable.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        cur = await self._conn.execute(
            "DELETE FROM learn_logs WHERE created_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_chain_matches(self, *, keep_days: int) -> int:
        """Delete ``chain_matches`` rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM chain_matches WHERE completed_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_signal_events(self, *, keep_days: int) -> int:
        """Delete ``signal_events`` rows older than ``keep_days``. Returns rowcount.

        Owned by the hourly maintenance pass, NOT by the chains engine. The
        prune used to live in ``scout/chains/tracker.py::_prune_stale``, which
        ``check_chains`` reaches only after its early return on an empty
        ``load_active_patterns()`` result — so at zero active patterns the
        largest table in the database (2.04 GB / 6.9M rows / ~550K rows a day)
        stopped pruning silently. Retention housekeeping must not sit beneath a
        "there is useful chain work to do" condition.

        ``<`` not ``<=`` (the siblings above use ``<=``): this carries over the
        exact boundary of the relocated implementation, so the relocation
        changes WHEN the prune runs and never how much it keeps.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM signal_events WHERE created_at < ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_holder_snapshots(self, *, keep_days: int) -> int:
        """Delete ``holder_snapshots`` rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM holder_snapshots WHERE scanned_at <= ?",
            (cutoff,),
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

    # ------------------------------------------------------------------
    # Venue-neutral execution binding (2026-08-02)
    # ------------------------------------------------------------------
    async def _migrate_venue_neutral_execution_v1(self) -> None:
        """Venue-neutral execution binding: intent hash, mandate mode, refusals.

        Three changes, all on ``live_trades`` (plus the CHECK on ``shadow_trades``,
        which shares the constraint):

        1. ``intent_hash TEXT`` — the SHA-256 that binds a submission to its TERMS.
           ``client_order_id`` already gave a submission an identity; this is what
           makes that identity mean "these exact terms" rather than "some submission".
        2. ``mandate_mode TEXT`` — which execution mandate authorized the row.
           ``ExecutionMandate`` counts supervised CEX history from this column, so a
           NULL is load-bearing: every row that predates the mandate correctly counts
           for NOTHING toward autonomous promotion. Backfilling it would hand the
           promotion bar a history that was never mandate-gated, which is the exact
           claim the count exists to refuse.
        3. Widen the ``reject_reason`` CHECK with the three refusals the new gates
           produce: ``mandate_inactive``, ``venue_capability_refused``,
           ``no_adapter_for_venue``. Without this the engine's rejection INSERT would
           raise IntegrityError and the refusal would be invisible — a fail-closed
           gate whose audit trail fails open.

        Both columns are nullable with NO DEFAULT, preserving absence-vs-value
        semantics (the project-wide convention). The CHECK widening uses the
        established rename-rebuild pattern from ``_migrate_reject_reason_extend_v2``,
        including the ``cross_venue_exposure`` / ``cross_venue_pnl`` view
        drop-and-recreate, because SQLite cannot ALTER a CHECK in place.

        Idempotent on the ``paper_migrations`` marker AND on per-object inspection,
        so a partially-applied run (crash between the rebuild and the marker) is safe
        to re-run.

        Migration ``bl_venue_neutral_execution_v1``, schema_version 20260802.
        """
        import re as _re

        import structlog

        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        migration = "bl_venue_neutral_execution_v1"
        new_reasons = (
            "mandate_inactive",
            "venue_capability_refused",
            "no_adapter_for_venue",
        )

        # *** PRAGMA foreign_keys MUST BE SET BEFORE `BEGIN`. ***
        # SQLite documents it as a no-op inside a transaction, so setting it after
        # BEGIN would silently leave enforcement ON and the defect below live.
        #
        # WHY IT HAS TO BE OFF AT ALL: `DROP TABLE live_trades` performs an implicit
        # DELETE of every row, which FIRES child foreign-key ACTIONS.
        # `solana_executions.live_trade_id` is declared
        # `REFERENCES live_trades(id) ON DELETE SET NULL` — so a rebuild with
        # enforcement on permanently NULLs every Solana execution's link to its
        # ledger row. Nothing detects it: `PRAGMA foreign_key_check` stays clean
        # because SET NULL is a legal outcome, not a violation. The runtime cost is
        # concrete — `_recover_interrupted_executions` calls `retire_row(...
        # live_trade_id)`, which returns early on None, so an interrupted execution
        # strands its `live_trades` row `open` forever, counting against the lane's
        # exposure and concurrency caps.
        #
        # This is NEW to this migration and not a latent flaw in the pattern it
        # copies: `_migrate_reject_reason_extend_v2` (20260514) predates
        # `solana_executions` (20260801), so this is the first live_trades rebuild
        # that has an FK child at all.
        #
        # This is SQLite's own documented 12-step ALTER TABLE procedure: disable
        # enforcement, rebuild, verify with foreign_key_check, commit, re-enable.
        await conn.execute("PRAGMA foreign_keys=OFF")
        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL)""")

            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration,)
            )
            if (await cur.fetchone()) is not None:
                await conn.commit()
                return

            # ---- Part 1: widen the reject_reason CHECK -------------------
            new_check = (
                "CHECK (reject_reason IS NULL OR reject_reason IN ("
                "'no_venue','insufficient_depth','slippage_exceeds_cap',"
                "'insufficient_balance','daily_cap_hit','kill_switch',"
                "'exposure_cap','override_disabled','venue_unavailable',"
                "'notional_cap_exceeded','signal_disabled','token_aggregate',"
                "'dual_signal_aggregate','all_candidates_failed',"
                "'master_kill','mode_paper',"
                "'live_signed_disabled','api_key_lacks_trade_scope',"
                "'mandate_inactive','venue_capability_refused',"
                "'no_adapter_for_venue'))"
            )
            # Hoisted above the loop because Part 3's post-condition uses it to
            # isolate the CHECK clause. Bound inside the loop body it would be
            # UNBOUND whenever every table was already widened — the common
            # fresh-install path, where each iteration `continue`s before reaching
            # the assignment. A NameError there would turn the safest case into
            # the only one that crashes.
            pattern = _re.compile(
                r"reject_reason\s+TEXT\s+CHECK\s*\([^)]*\)\s*\)",
                _re.IGNORECASE | _re.DOTALL,
            )
            for table in ("shadow_trades", "live_trades"):
                cur = await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                row = await cur.fetchone()
                if row is None:
                    continue
                table_sql = row[0] or ""
                if all(reason in table_sql for reason in new_reasons):
                    continue  # already widened

                cur = await conn.execute(f"PRAGMA table_info({table})")
                cols = await cur.fetchall()
                if not cols:
                    continue
                col_list = ", ".join(c[1] for c in cols)

                new_table_sql = pattern.sub(
                    f"reject_reason       TEXT {new_check}", table_sql
                )
                if new_table_sql == table_sql:
                    _log.warning(
                        "venue_neutral_reject_reason_pattern_miss",
                        table=table,
                        sql_excerpt=table_sql[:200],
                    )
                    continue

                # Prod's sqlite_master.sql may carry the table name QUOTED after an
                # earlier rebuild; the pattern covers quoted, unquoted and
                # IF NOT EXISTS forms (hotfix lesson from the v2 migration).
                name_pattern = (
                    r"TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                    r'["\x60]?' + table + r'["\x60]?\s*\('
                )
                new_table_sql_renamed = _re.sub(
                    name_pattern,
                    f"TABLE {table}_new (",
                    new_table_sql,
                    count=1,
                    flags=_re.IGNORECASE,
                )
                if new_table_sql_renamed == new_table_sql:
                    _log.warning(
                        "venue_neutral_reject_reason_rename_miss",
                        table=table,
                        sql_excerpt=new_table_sql[:200],
                    )
                    continue

                # *** CAPTURE EVERY INDEX, DO NOT ENUMERATE THEM. ***
                # DROP TABLE takes the table's indexes with it. An enumerated
                # recreate list is a list that goes stale the moment someone adds
                # an index in another migration — and it goes stale SILENTLY: the
                # rebuild still succeeds, the index is simply gone. Reading the
                # CREATE statements out of sqlite_master and replaying them cannot
                # drift. (`sql IS NULL` for auto-indexes backing UNIQUE/PK
                # constraints; those come back with the table definition itself.)
                cur = await conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                    (table,),
                )
                index_ddl = [r[0] for r in await cur.fetchall()]

                # AUTOINCREMENT's contract is that an id is never reused. The
                # rebuild resets `sqlite_sequence`, so ids above the surviving
                # max(id) become allocatable again — and live_trade ids are quoted
                # in evidence files, Telegram messages and Solana execution rows,
                # where a reused id names a different trade.
                cur = await conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
                )
                seq_row = await cur.fetchone()
                prior_seq = seq_row[0] if seq_row is not None else None

                if table == "live_trades":
                    await conn.execute("DROP VIEW IF EXISTS cross_venue_exposure")
                    await conn.execute("DROP VIEW IF EXISTS cross_venue_pnl")

                await conn.execute(new_table_sql_renamed)
                await conn.execute(
                    f"INSERT INTO {table}_new ({col_list}) "
                    f"SELECT {col_list} FROM {table}"
                )
                await conn.execute(f"DROP TABLE {table}")
                await conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")

                for ddl in index_ddl:
                    await conn.execute(ddl)

                if prior_seq is not None:
                    # UPDATE-then-INSERT rather than UPSERT: `sqlite_sequence` is an
                    # internal table with NO primary key and NO unique constraint,
                    # so `ON CONFLICT(name)` raises "does not match any PRIMARY KEY
                    # or UNIQUE constraint".
                    cur = await conn.execute(
                        "UPDATE sqlite_sequence SET seq=? WHERE name=?",
                        (prior_seq, table),
                    )
                    if cur.rowcount == 0:
                        await conn.execute(
                            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                            (table, prior_seq),
                        )

                if table == "live_trades":
                    # Belt to the capture-and-replay braces: v2's rebuild did NOT
                    # recreate this index, so a database migrated by v2 may reach
                    # here without it — in which case there was nothing to capture.
                    # It is the DB-layer backstop that stops a concurrent retry
                    # double-submitting, so it is recreated unconditionally.
                    await conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "idx_live_trades_client_order_id "
                        "ON live_trades(client_order_id) "
                        "WHERE client_order_id IS NOT NULL"
                    )
                    await conn.execute(
                        """CREATE VIEW IF NOT EXISTS cross_venue_exposure AS
                           SELECT
                               'binance' AS venue,
                               COALESCE(SUM(CAST(size_usd AS REAL)), 0) AS open_exposure_usd,
                               COUNT(*) AS open_count
                           FROM live_trades
                           WHERE status = 'open'
                           UNION ALL
                           SELECT
                               'minara_' || COALESCE(chain, 'unknown') AS venue,
                               COALESCE(SUM(amount_usd), 0) AS open_exposure_usd,
                               COUNT(*) AS open_count
                           FROM paper_trades
                           WHERE status = 'open'
                             AND chain != 'coingecko'
                             AND chain != ''
                           GROUP BY chain"""
                    )
                    await conn.execute("""CREATE VIEW IF NOT EXISTS cross_venue_pnl AS
                           SELECT
                               'placeholder_m1' AS venue,
                               0.0 AS realized_pnl_usd,
                               0.0 AS unrealized_pnl_usd""")

            # ---- Part 2: the two additive columns ------------------------
            cur = await conn.execute("PRAGMA table_info(live_trades)")
            existing_cols = {r[1] for r in await cur.fetchall()}
            if existing_cols:
                if "intent_hash" not in existing_cols:
                    await conn.execute(
                        "ALTER TABLE live_trades ADD COLUMN intent_hash TEXT"
                    )
                if "mandate_mode" not in existing_cols:
                    await conn.execute(
                        "ALTER TABLE live_trades ADD COLUMN mandate_mode TEXT"
                    )
                # Non-unique: two rows CAN legitimately share an intent_hash (a
                # rejection row written before submission, then a submitted row for
                # the same intent). Uniqueness lives on client_order_id, the
                # per-submission key; this index only serves lookup.
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_live_trades_intent_hash "
                    "ON live_trades(intent_hash) WHERE intent_hash IS NOT NULL"
                )
                # The mandate's supervised-history count filters on mandate_mode;
                # without this it table-scans live_trades on every autonomy check.
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_live_trades_mandate_mode "
                    "ON live_trades(mandate_mode) WHERE mandate_mode IS NOT NULL"
                )

            # ---- Part 3: POST-CONDITIONS, checked before the marker ------
            #
            # *** THE MARKER IS A CLAIM THAT THE MIGRATION WORKED. ***
            # Every `continue` above is a path where a regex did not match the
            # table's actual DDL — a quoted name shape, a NOT NULL before the
            # CHECK, a named constraint, a COLLATE clause. Those log a warning and
            # move on. Without this block the marker is still written, the
            # migration is permanently recorded as applied, and it never retries:
            # `reject_reason='mandate_inactive'` then raises IntegrityError forever
            # and every mandate refusal becomes invisible. That is precisely the
            # "fail-closed gate whose audit trail fails open" this widening exists
            # to prevent — reintroduced by the migration meant to prevent it.
            #
            # So the post-condition is asserted against the DATABASE, not inferred
            # from the fact that no exception was raised. A failure here rolls the
            # whole transaction back, leaving the migration un-marked and therefore
            # retried on the next boot with the operator holding a loud error.
            for table in ("shadow_trades", "live_trades"):
                cur = await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                row = await cur.fetchone()
                if row is None:
                    continue  # table genuinely absent — nothing was claimed for it
                # *** LOOK INSIDE THE CHECK CLAUSE, NOT AT THE WHOLE DDL. ***
                # A substring test over the full CREATE TABLE text shares its
                # failure mode with the already-widened skip at the top of the
                # loop: any occurrence of the three strings ANYWHERE in the DDL —
                # a comment, a column name, another constraint — satisfies both
                # while the CHECK itself stays narrow. A backstop that fails the
                # same way as the thing it backstops is not a backstop.
                check_clause = pattern.search(row[0] or "")
                haystack = check_clause.group(0) if check_clause else ""
                missing = [r for r in new_reasons if r not in haystack]
                if missing:
                    # Two ways to arrive here, and the message must not assert the
                    # wrong one: the rewrite ran and its pattern missed, OR the
                    # skip-check at the top of the loop matched the reason strings
                    # somewhere else in the DDL and never attempted a rewrite at
                    # all. Both leave the CHECK narrow; only the second means no
                    # rewrite was tried.
                    raise RuntimeError(
                        f"{migration}: {table}.reject_reason CHECK still refuses "
                        f"{missing}. Either the rewrite pattern did not match this "
                        "table's DDL, or the already-widened skip matched those "
                        "strings outside the CHECK clause and skipped the rebuild. "
                        "Refusing to record the migration as applied — a gate whose "
                        "refusals raise IntegrityError is worse than no gate."
                    )
            cur = await conn.execute("PRAGMA table_info(live_trades)")
            live_cols = {r[1] for r in await cur.fetchall()}
            if live_cols and not {"intent_hash", "mandate_mode"} <= live_cols:
                raise RuntimeError(
                    f"{migration}: live_trades is missing "
                    f"{ {'intent_hash', 'mandate_mode'} - live_cols }"
                )

            # Foreign keys were disabled for the rebuild; prove THIS rebuild left
            # nothing dangling before committing.
            #
            # *** SCOPED TO THE TABLES THIS MIGRATION TOUCHES. ***
            # `PRAGMA foreign_key_check` with no argument checks the ENTIRE
            # database. One pre-existing dangling row in a table this migration
            # never opens — and there are eleven FK-bearing tables it never
            # opens — would raise here, roll back, leave the marker unwritten, and
            # therefore fail EVERY boot from then on. Not hypothetical for this
            # database: `dashboard/api.py::_build_alert_outcome` documents handling
            # exactly that state in production ("FK set but the paper_trades row is
            # gone — ON DELETE SET NULL race / manual delete").
            #
            # A migration's post-condition must be about the migration. Asserting a
            # whole-database invariant here converts unrelated pre-existing data
            # into a permanent boot failure — a strictly worse outcome than the one
            # this check guards against.
            #
            # Worth being precise about what it can prove: `foreign_key_check(T)`
            # validates the FKs DECLARED ON T, and ON DELETE SET NULL is a legal
            # outcome rather than a violation — so this check could never have
            # caught the severed-links defect. `foreign_keys=OFF` is what fixes
            # that. This is belt-and-braces, and belt-and-braces must not have a
            # larger blast radius than the thing it braces.
            #
            # ONE FUTURE HAZARD, recorded because its symptom is opaque: the pragma
            # RAISES `OperationalError: foreign key mismatch` — rather than
            # reporting a violation — when a table declares an FK referencing a
            # column that is not UNIQUE or a PRIMARY KEY. Unreachable today: every
            # FK on these three points at an INTEGER PRIMARY KEY (`paper_trades.id`,
            # `kill_events.id`, `live_trades.id`). A later migration adding an FK to
            # a non-unique column on any of them turns this into a hard boot failure
            # whose message names neither the migration nor the cause. It fails loud
            # and un-marked, so it is correctness-preserving — just expensive to
            # diagnose without this note.
            violations: list = []
            for table in ("live_trades", "shadow_trades", "solana_executions"):
                cur = await conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if await cur.fetchone() is None:
                    continue
                cur = await conn.execute(f"PRAGMA foreign_key_check({table})")
                # `row_factory` is `aiosqlite.Row`, which reprs as an object
                # address — useless in the error an operator has to act on.
                violations.extend((table, tuple(r)) for r in await cur.fetchall())
            if violations:
                raise RuntimeError(
                    f"{migration}: rebuild left {len(violations)} foreign-key "
                    f"violation(s), first={violations[0]}"
                )

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)",
                (migration, now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260802, now_iso, migration),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?", (migration,)
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError(f"{migration} cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration=migration)
            raise
        finally:
            # Restore enforcement on every exit path — including the early return
            # on the already-applied marker, which `finally` covers.
            #
            # *** AND VERIFY IT, BECAUSE THE PRAGMA CAN SILENTLY NO-OP. ***
            # `PRAGMA foreign_keys` does nothing while a transaction is open. The
            # normal and error paths both leave one — commit, or ROLLBACK — but
            # ROLLBACK is itself wrapped in a try/except above (a failing rollback
            # must not mask the original error). If that except fires, the
            # connection is still in a transaction, this PRAGMA quietly does
            # nothing, and foreign keys stay OFF for every writer in the process
            # for the rest of its life. That is a strictly worse failure than the
            # severed-links defect this OFF exists to avoid, so it is checked
            # rather than assumed.
            # *** CLOSE THE TRANSACTION FIRST, IF ONE IS STILL OPEN. ***
            # The `except` above rolls back inside its own try/except, because a
            # failing rollback must not mask the original error. But swallowing it
            # leaves the transaction OPEN, and an open transaction is exactly the
            # condition under which the pragma below does nothing.
            #
            # Guarded on `in_transaction` rather than attempted unconditionally.
            # An unconditional ROLLBACK fails with "no transaction is active" on
            # every NORMAL path, which forces the choice between swallowing that
            # expected failure — banned in this module by
            # `tests/test_db_rollback_observability.py`, and rightly, since a
            # swallowed rollback hides disk/lock/WAL failures — and logging an
            # exception on every successful boot. Asking first makes a failure
            # here genuinely exceptional, so it can be logged loudly without noise.
            if conn.in_transaction:
                try:
                    await conn.execute("ROLLBACK")
                except Exception as rb_err:
                    _log.exception(
                        "schema_migration_finally_rollback_failed",
                        migration=migration,
                        err=str(rb_err),
                    )

            await conn.execute("PRAGMA foreign_keys=ON")
            _cur = await conn.execute("PRAGMA foreign_keys")
            _fk = await _cur.fetchone()
            if not (_fk and _fk[0]):
                # A restore that is not verified is the same class of unchecked
                # claim the post-condition above just replaced. Verified, and then
                # ACTED ON: a connection with enforcement off is not safe to hand
                # back, and callers cache it — `dashboard/api.py` assigns its
                # module-level handle BEFORE `initialize()` and reuses it on every
                # later request even when initialization raised, so "the process
                # dies anyway" is not a safe assumption.
                #
                # Dropping the handle makes every subsequent use raise
                # "Database not initialized" instead of writing unenforced. Not
                # raising from `finally`: that would replace the in-flight
                # exception, and the original migration failure is the more useful
                # one to surface.
                _log.error(
                    "FOREIGN_KEYS_LEFT_DISABLED",
                    migration=migration,
                    detail=(
                        "PRAGMA foreign_keys=ON did not take effect even after an "
                        "unconditional ROLLBACK, so a transaction is still open on "
                        "this connection. The connection has been dropped rather "
                        "than returned with enforcement off; restart the service."
                    ),
                )
                try:
                    await conn.close()
                except Exception as close_err:  # pragma: no cover — defensive
                    # Logged, not swallowed: a close that fails here means the
                    # connection is still open AND unenforced, which is the worst
                    # of the states this branch exists to escape. Dropping the
                    # handle below still happens either way.
                    _log.exception(
                        "schema_migration_unenforced_close_failed",
                        migration=migration,
                        err=str(close_err),
                    )
                self._conn = None

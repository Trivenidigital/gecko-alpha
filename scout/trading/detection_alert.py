"""ALR-02: detection-time alert lane.

Fires an "early candidate detected" Telegram alert on the SCORING pass —
BEFORE the paper dispatch gate (which rejects ~99.99% of scored candidates)
decides. This reframes the operator alert from "the robot acted" (what the
paper-open lane in ``tg_alert_dispatch.py`` says) to "an early candidate is
here", serving the product's central promise: beat CoinGecko Highlights by
minutes.

Design (tasks/design_detection_time_alert_lane.md). The lane is a composition
of EXISTING primitives — no new table, no schema_version, no CHECK change:

- trigger  = CG-sourced + fresh (candidates.first_seen_at within
  DETECTION_ALERT_MAX_AGE_MIN) + early vs CG trending
  (engine._compute_lead_time_vs_trending: no_reference, or ok+negative lead).
- universe = tg_alert_dispatch._check_universe (reused verbatim).
- dedup    = per-token 24h over sent detection_lane rows
  (TG_ALERT_DEDUP_WINDOW_HOURS; 0 disables).
- gate     = ALR-02 quality gate (quant_score >= DETECTION_ALERT_MIN_QUANT_SCORE),
  applied BEFORE the daily cap.
- budget   = DETECTION_ALERT_MAX_PER_DAY sent rows / UTC day, spent
  highest-score-first (freshest breaks ties).
- audit    = one tg_alert_log row per decision, signal_type='detection_lane',
  detail='detection_lane[:reason]', paper_trade_id=NULL.

``notify_early_detections`` NEVER raises — a bug here can never break the
pipeline cycle. It is spawned fire-and-forget from scout/main.py::run_cycle.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from scout import alerter
from scout.config import Settings
from scout.db import Database
from scout.trading.engine import _compute_lead_time_vs_trending
from scout.trading.receipt_archive import check_disk_pressure

# Reuse the paper-lane formatters + universe guard verbatim (card-v2 parity).
from scout.trading.tg_alert_dispatch import (
    _check_universe,
    _fmt_mcap,
    _fmt_price,
)

log = structlog.get_logger(__name__)

# Dormancy: emit the `detection_receipts_disabled` breadcrumb only ONCE per
# process (at the first skip), not every cycle — avoids dormant-mode log spam.
_receipts_disabled_logged = False

# Disk-pressure operator page cooldown: in-memory timestamp of the last dispatched
# Telegram alert. Module-level (per process); a restart resets it (documented in
# Settings.DETECTION_RECEIPT_DISK_ALERT_COOLDOWN_HOURS — acceptable for a page).
_last_disk_alert_at: datetime | None = None


def _log_receipts_disabled_once() -> None:
    """Emit the dormant-mode breadcrumb exactly once per process."""
    global _receipts_disabled_logged
    if not _receipts_disabled_logged:
        log.info("detection_receipts_disabled")
        _receipts_disabled_logged = True


def _disk_alert_due(now: datetime, cooldown_hours: float) -> bool:
    """True (and records ``now``) when a disk-pressure page is due — i.e. no page
    has fired within ``cooldown_hours``. The per-cycle structured WARNING is NOT
    gated by this; only the Telegram page is."""
    global _last_disk_alert_at
    if _last_disk_alert_at is None or (now - _last_disk_alert_at) >= timedelta(
        hours=cooldown_hours
    ):
        _last_disk_alert_at = now
        return True
    return False


# ALR-02 decision-receipt gate version (reviewer LOCK 2). A literal that pins
# the gate/threshold logic in force when a receipt was written, so a receipt is
# reproducible even after the gate changes. BUMP this on ANY future change to
# the detection-lane gate logic (_passes_quality_gate, the trigger, universe,
# dedup, or cap semantics) so receipts written under the old and new gates are
# distinguishable; it is also part of the idempotency-key recipe, so bumping it
# opens a fresh analytical unit per token rather than colliding with pre-bump
# rows. Format is free-text; "466.1" = PR #466 gate shape, revision 1.
DETECTION_GATE_VERSION = "466.1"

# The gate comparator actually applied by _passes_quality_gate: score >= bar.
# Recorded first-class on every receipt (reviewer correction 2) so the precise
# decision logic is resolvable from the row alone.
DETECTION_GATE_COMPARATOR = ">="


def _resolve_code_version() -> str | None:
    """Best-effort deployed-code identity (git SHA), read ONCE at import.

    Reviewer correction 2 wants the deployed code identity persisted alongside
    the gate version so a receipt resolves to the EXACT decision logic in force.
    Reads ``.git/HEAD`` (and the referenced ref) from the repo root — no env
    reads (project rule: no os.getenv in business logic), no subprocess. Handles
    both a normal checkout (``.git`` is a directory) and a git WORKTREE
    (``.git`` is a file ``gitdir: <path>``; refs resolve via ``commondir``).
    Returns the 40-char SHA, or None when ``.git`` is unavailable (e.g. a
    tarball deploy); in that case DETECTION_GATE_VERSION is the documented
    resolver of record (see tasks/prereg_detection_gate_enrichment_cohort.md).
    Never raises.
    """
    try:
        # scout/trading/detection_alert.py → repo root is two parents above scout/.
        root = Path(__file__).resolve().parents[2]
        git = root / ".git"
        if git.is_file():
            # Worktree: ".git" is a file pointing at the per-worktree gitdir.
            gitdir = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
            head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
            commondir_f = gitdir / "commondir"
            common = (
                (gitdir / commondir_f.read_text(encoding="utf-8").strip()).resolve()
                if commondir_f.exists()
                else gitdir
            )
        else:
            gitdir = git
            common = git
            head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return (common / ref).read_text(encoding="utf-8").strip()[:40]
        return head[:40] or None
    except Exception:
        return None


# Resolved once at import — the code identity of the running process.
DETECTION_CODE_VERSION = _resolve_code_version()

# The machine-readable terminal outcomes a receipt can carry, one per decision
# point in the evaluation loop (reviewer LOCK 1 — a receipt for EVERY evaluated
# candidate). "not_early" is the trigger-fail decision (gate-passer that CG has
# already trended past): it is a real terminal decision in the loop, so LOCK 1
# requires a receipt for it even though it is not one of the reviewer's seven
# example outcome strings. See tasks/prereg_detection_gate_enrichment_cohort.md.
DETECTION_RECEIPT_OUTCOMES = frozenset(
    {
        "sent",
        "gate_fail_quality",
        "dedup_24h",
        "universe_filter",
        "too_old",
        "rate_limit",
        "dispatch_failed",
        "not_early",
    }
)


def _receipt_idempotency_key(
    token_id: str,
    outcome: str,
    source_observation_ts: str | None,
    gate_version: str,
    evaluation_instance: str,
) -> str:
    """Deterministic idempotency key for a decision receipt.

    TWO-IDENTITY MODEL (reviewer, 2026-07-26). The key is the **EVALUATION
    IDENTITY**: it incorporates the evaluation instance (the cycle
    ``decided_at``), so each later poll of a candidate is a NEW receipt. Recipe
    (documented identically in tasks/prereg_detection_gate_enrichment_cohort.md):

        sha256_hex(
          "{token_id}|{outcome}|{source_observation_ts}|{gate_version}|{evaluation_instance}"
        )

    where a None ``source_observation_ts`` is rendered as the empty string and
    the field separator is a literal ``|``. Because the cycle ``decided_at`` is
    IN the key, cycle-N and cycle-N+1 evaluations of the same token yield
    DIFFERENT keys and DISTINCT rows — routine re-polls are never collapsed and
    never mis-classified as conflicts (the earlier full-payload comparison over
    an evaluation-instance-independent key mis-counted 715 unchanged re-polls/
    cycle as conflicts on 2026-07-26; see the prereg §7 + capacity artifact).

    ``INSERT OR IGNORE`` + the UNIQUE index now only collapse an exact repeat of
    the SAME evaluation instance (an intra-cycle crash/retry or a duplicated
    input) → ``exact_idempotent_replay``; a same-instance payload mismatch is a
    genuine ``conflicting_duplicate``. The separate ANALYTICAL INDEX IDENTITY —
    the token's FIRST valid evaluation after cohort start, which fixes its arm +
    endpoint anchor — is a query-time definition (prereg §4), not this key.
    """
    raw = (
        f"{token_id}|{outcome}|{source_observation_ts or ''}"
        f"|{gate_version}|{evaluation_instance}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _write_detection_receipt(
    db: Database,
    settings: Settings,
    counters: dict[str, int],
    *,
    token_id: str,
    outcome: str,
    reason: str | None,
    cand,
    source_observation_ts: str | None,
    decided_at: str,
    extra_inputs: dict | None = None,
) -> None:
    """Persist ONE decision receipt, fail-soft (reviewer LOCK 4).

    Every terminal decision in ``notify_early_detections`` calls this exactly
    once. It NEVER raises: a write failure is caught, logged as a structured
    ``detection_receipt_write_failed`` event, and counted, after which the lane
    proceeds exactly as before — a receipt-write failure can never change
    sending behavior. ``counters`` accumulates the per-cycle reconciliation set
    (evaluated / newly_written / exact_replays / write_failures / conflicts)
    surfaced in ``detection_receipt_summary``.

    Persists the RAW decision inputs (reviewer correction 2), not merely fired
    signals: the derived score BEFORE (raw model value) and AFTER the gate's
    ``int(x or 0)`` clipping, the comparator + threshold actually applied, the
    gate + code versions, and a ``raw_inputs`` JSON blob of the raw component
    values + per-input missingness/default indicators (plus any outcome-specific
    inputs the caller passes via ``extra_inputs``).
    """
    if counters.get("receipts_disabled"):
        # DORMANCY: skip BEFORE any persistence attempt and BEFORE counting, so a
        # dormant deployment writes zero rows and emits no per-candidate work.
        return
    counters["evaluated"] += 1
    if counters.get("disk_suspended"):
        # Disk-pressure fail-closed: accrual is SUSPENDED this cycle. We count the
        # evaluation as skipped (coverage is unhealthy) but write NO receipt and
        # NEVER prune/sample/reduce detail. The send path is unaffected upstream.
        counters["skipped_disk_pressure"] += 1
        return
    quant_raw = getattr(cand, "quant_score", None)  # int | None (raw model value)
    score_before = quant_raw if isinstance(quant_raw, int) else None
    score_after = int(quant_raw or 0)  # the operand the gate actually compares
    signals = getattr(cand, "signals_fired", None)
    signals_json = json.dumps(signals) if signals is not None else None
    threshold = settings.DETECTION_ALERT_MIN_QUANT_SCORE

    # raw_inputs — the raw values the decision consumed + missingness/default
    # indicators. sort_keys=True → a deterministic serialization so the
    # conflict-classifier can compare payloads byte-for-byte for a repeat of the
    # SAME evaluation instance (an intra-cycle retry).
    raw_inputs: dict = {
        "quant_score_raw": quant_raw,
        "quant_score_missing": quant_raw is None,
        "signals_fired": signals,
        "signals_fired_missing": signals is None,
        "score_before": score_before,
        "score_after": score_after,
        "comparator": DETECTION_GATE_COMPARATOR,
        "threshold": threshold,
        "gate_expr": f"score_after {DETECTION_GATE_COMPARATOR} {threshold}",
    }
    if extra_inputs:
        raw_inputs.update(extra_inputs)
    raw_inputs_json = json.dumps(raw_inputs, sort_keys=True)

    # Evaluation-identity key: decided_at (the cycle instant) is the evaluation
    # instance, so each cycle's evaluation of a token is a DISTINCT row.
    idempotency_key = _receipt_idempotency_key(
        token_id,
        outcome,
        source_observation_ts,
        DETECTION_GATE_VERSION,
        decided_at,
    )
    try:
        result = await db.insert_detection_decision_receipt(
            token_id=token_id,
            decided_at=decided_at,
            outcome=outcome,
            reason=reason,
            source_observation_ts=source_observation_ts,
            gate_version=DETECTION_GATE_VERSION,
            code_version=DETECTION_CODE_VERSION,
            score_before=score_before,
            score_after=score_after,
            comparator=DETECTION_GATE_COMPARATOR,
            threshold_value=threshold,
            signals_fired=signals_json,
            raw_inputs=raw_inputs_json,
            idempotency_key=idempotency_key,
        )
        # Bucket by result so the per-cycle reconciliation identity holds exactly
        # (reviewer naming, 2026-07-26):
        #   evaluated == newly_written + exact_replays + write_failures
        #              + conflicting_duplicates
        if result == "inserted":
            counters["newly_written"] += 1
        elif result == "idempotent_replay":
            counters["exact_replays"] += 1
        elif result == "conflict":
            # Same evaluation-instance key, different payload — a correctness
            # defect (the same evaluation produced two payloads). Surface it; it
            # must never pass as healthy coverage (reviewer correction 3).
            counters["conflicts"] += 1
            log.warning(
                "detection_receipt_conflict",
                token_id=token_id,
                outcome=outcome,
                idempotency_key=idempotency_key,
                gate_version=DETECTION_GATE_VERSION,
            )
    except Exception as e:
        counters["write_failures"] += 1
        log.warning(
            "detection_receipt_write_failed",
            token_id=token_id,
            outcome=outcome,
            err=str(e),
        )


def _detection_trigger(
    lead_time_min: float | None, lead_time_status: str | None
) -> bool:
    """True when the candidate is EARLY relative to CG trending.

    Sign convention (matches engine._compute_lead_time_vs_trending, and the
    codebase has a documented history of sign-flip bugs on this column):
    NEGATIVE lead_time = detected BEFORE the coin trended (early / good);
    POSITIVE = detected AFTER (late). So the lane fires when:

      - status == 'no_reference': the coin has NEVER appeared on CG trending
        (we are entirely ahead of the crossover), OR
      - status == 'ok' AND lead_time_min < 0: a trending crossover exists but
        it is LATER than the detection instant (still early).

    'ok' with lead_time >= 0 (already trending / late) and 'error' do NOT fire.
    """
    if lead_time_status == "no_reference":
        return True
    if lead_time_status == "ok" and lead_time_min is not None and lead_time_min < 0:
        return True
    return False


def _passes_quality_gate(cand, settings: Settings) -> bool:
    """True when a candidate's quant_score clears the ALR-02 quality bar.

    Applied BEFORE the scarce daily budget is spent, so the cap is filled with
    the highest-quality early candidates rather than merely the freshest. The
    ALR-02 evaluation (2026-07-11→07-14) found the ungated lane spent every
    slot on quant_score=0 candidates (0/20 ever trended) while genuine
    pre-trending catches — which DID fire scoring signals — were never sent.

    Single source of truth: quant_score >= DETECTION_ALERT_MIN_QUANT_SCORE.
    Because every scoring signal contributes positive points, quant_score == 0
    iff no signal fired, so the default bar of 1 is exactly "at least one
    scoring signal fired" (the validated coarse gate). A None score (un-scored
    candidate) reads as 0 and is blocked.
    """
    score = int(getattr(cand, "quant_score", None) or 0)
    return score >= settings.DETECTION_ALERT_MIN_QUANT_SCORE


def _fmt_detection_line(
    first_seen_min_ago: float | None,
    lead_time_min: float | None,
    lead_time_status: str | None,
) -> str:
    """Freshness + earliness line: 'first seen N min ago · <reference>'."""
    if first_seen_min_ago is None:
        seen = "first seen just now"
    else:
        seen = f"first seen {max(0, int(round(first_seen_min_ago)))} min ago"
    if lead_time_status == "ok" and lead_time_min is not None and lead_time_min < 0:
        ahead = abs(int(round(lead_time_min)))
        ref = f"{ahead} min ahead of CG trending"
    else:
        ref = "not yet on CG trending"
    return f"{seen} · {ref}"


def _build_token_deep_link(dashboard_base_url: str | None, coin_id: str) -> str | None:
    """ALR-09 dashboard deep link to the per-token page (no trade row exists).

    Returns None (line omitted) when the base URL is empty (operator
    off-switch).
    """
    if not dashboard_base_url:
        return None
    return f"{dashboard_base_url.rstrip('/')}/#/token/{coin_id}"


def format_detection_alert(
    *,
    symbol: str,
    coin_id: str,
    price: float | None,
    mcap: float | None,
    first_seen_min_ago: float | None,
    lead_time_min: float | None,
    lead_time_status: str | None,
    dashboard_base_url: str | None = None,
) -> str:
    """Plain-text Telegram body for an early-detection alert.

    Caller MUST dispatch with parse_mode=None (plain text; global CLAUDE.md
    §12b). Reuses _fmt_price / _fmt_mcap from the paper lane for card-v2
    parity.
    """
    header = f"🔎 EARLY DETECT · {symbol} · {_fmt_price(price)} · {_fmt_mcap(mcap)}"
    parts = [
        header,
        _fmt_detection_line(first_seen_min_ago, lead_time_min, lead_time_status),
        f"coingecko.com/en/coins/{coin_id}",
    ]
    deep_link = _build_token_deep_link(dashboard_base_url, coin_id)
    if deep_link is not None:
        parts.append(f"Dashboard: {deep_link}")
    return "\n".join(parts)


def _age_minutes(iso: str | None, now: datetime) -> float | None:
    """Minutes between an ISO timestamp and ``now``. None on parse failure."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 60.0


async def _fetch_first_seen_mcap(
    db: Database, token_id: str
) -> tuple[str | None, float | None]:
    """Authoritative first_seen_at + mcap from the candidates row. Fail-soft.

    The candidates upsert preserves the EARLIEST sighting (db.py:7153), so the
    persisted first_seen_at is the true detection time — unlike the in-memory
    CandidateToken, whose first_seen_at defaults to construction time.
    """
    try:
        cur = await db._conn.execute(
            "SELECT first_seen_at, market_cap_usd FROM candidates "
            "WHERE contract_address = ?",
            (token_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return (None, None)
        return (row[0], row[1])
    except Exception:
        log.exception("detection_alert_first_seen_fetch_failed", token_id=token_id)
        return (None, None)


async def _fetch_price_mcap(
    db: Database, token_id: str
) -> tuple[float | None, float | None]:
    """Current price + mcap from price_cache. Fail-soft (None → '$0'/'$?')."""
    try:
        cur = await db._conn.execute(
            "SELECT current_price, market_cap FROM price_cache WHERE coin_id = ?",
            (token_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return (None, None)
        return (row[0], row[1])
    except Exception:
        log.exception("detection_alert_price_fetch_failed", token_id=token_id)
        return (None, None)


async def _fetch_first_trending_at(db: Database, token_id: str) -> str | None:
    """First CG-trending snapshot time for a token — the SAME value the trigger
    classification reads (``MIN(snapshot_at)`` in
    engine._compute_lead_time_vs_trending).

    Preserved verbatim on a ``not_early`` receipt as ``pre_index_trending_at``
    (reviewer amendment 1) so the pre-index trending timestamp that drove the
    "already trending" classification is durable. Fail-soft: None when the token
    has no snapshot (e.g. a status='error' classification) or the read fails.
    """
    try:
        cur = await db._conn.execute(
            "SELECT MIN(snapshot_at) FROM trending_snapshots WHERE coin_id = ?",
            (token_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None
    except Exception:
        log.exception("detection_alert_trending_fetch_failed", token_id=token_id)
        return None


async def _count_sent_today(db: Database, now: datetime) -> int:
    """Number of detection-lane alerts sent since UTC midnight."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM tg_alert_log "
        "WHERE outcome = 'sent' AND detail = 'detection_lane' "
        "AND alerted_at >= ?",
        (today_start,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _check_detection_dedup(
    db: Database, settings: Settings, token_id: str, now: datetime
) -> bool:
    """True if a sent detection-lane alert exists for this token in the 24h
    window (block the re-send). Scoped to detail='detection_lane' so the lane
    is independent of the paper-open alert lane. window <= 0 disables dedup.
    """
    window = settings.TG_ALERT_DEDUP_WINDOW_HOURS
    if window <= 0:
        return False
    cutoff = (now - timedelta(hours=window)).isoformat()
    cur = await db._conn.execute(
        "SELECT 1 FROM tg_alert_log "
        "WHERE token_id = ? AND outcome = 'sent' AND detail = 'detection_lane' "
        "AND alerted_at >= ? LIMIT 1",
        (token_id, cutoff),
    )
    return (await cur.fetchone()) is not None


async def _log_detection_outcome(
    db: Database,
    *,
    token_id: str,
    outcome: str,
    detail: str,
    now: datetime | None = None,
) -> None:
    """Write one tg_alert_log audit row. signal_type='detection_lane',
    paper_trade_id=NULL (there is no trade yet — the point of the lane)."""
    if db._conn is None:
        return
    alerted_at = (now or datetime.now(timezone.utc)).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            "INSERT INTO tg_alert_log "
            "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (None, "detection_lane", token_id, alerted_at, outcome, detail),
        )
        await db._conn.commit()


async def notify_early_detections(
    db: Database,
    settings: Settings,
    session,
    *,
    candidates,
    now: datetime | None = None,
) -> None:
    """Fire early-detection alerts for a cycle's scored candidates (best-effort).

    Never raises. Spawned fire-and-forget from run_cycle. ``candidates`` is the
    cycle's list of scored CandidateToken objects; the lane reads the
    authoritative first_seen_at / price from the DB (not the in-memory model),
    but the quality gate reads the in-memory quant_score / signals_fired (the
    scores computed THIS cycle at detection). Candidates that clear the ALR-02
    quality gate are spent highest-score-first, bounded by
    DETECTION_ALERT_MAX_PER_DAY.
    """
    if not settings.DETECTION_ALERT_LANE_ENABLED:
        return
    if db._conn is None:
        log.warning("detection_alert_no_conn")
        return

    now = now or datetime.now(timezone.utc)
    # EVALUATION-INSTANCE IDENTIFIER (reviewer, 2026-07-26): created ONCE here,
    # BEFORE any persistence, and reused for every receipt in this cycle AND for
    # every retry of the same insert. It is the cycle-frozen ``decided_at`` — a
    # deterministic value, NOT a per-insert-attempt UUID/timestamp, so an exact
    # retry re-derives the SAME idempotency key (via _receipt_idempotency_key)
    # and collapses to one row rather than becoming a new evaluation.
    decided_at = now.isoformat()
    # Reviewer LOCK 4 + corrections 2 & 3 + two-identity naming: per-cycle
    # reconciliation counters. Every terminal decision (past the evaluation
    # boundary) writes exactly one receipt via _write_detection_receipt, bucketed
    # so the identity
    #   evaluated == newly_written + exact_replays + write_failures + conflicts
    # holds exactly. Under evaluation-identity every fresh cycle re-writes each
    # token as a NEW row, so `newly_written` ≈ evaluated and `exact_replays` /
    # `conflicts` are ~0 on healthy cycles (a same-instance payload mismatch is a
    # true defect). filtered_* count PRE-boundary rejected inputs SEPARATELY BY
    # REASON — they never inflate `evaluated` and never disappear silently. All
    # are surfaced each cycle in detection_receipt_summary.
    receipt_counters = {
        "evaluated": 0,
        "newly_written": 0,
        "exact_replays": 0,
        "write_failures": 0,
        "conflicts": 0,
        "filtered_missing_id": 0,
        "filtered_non_cg_source": 0,
        "filtered_malformed": 0,
        "skipped_disk_pressure": 0,
        "disk_suspended": False,
        "receipts_disabled": False,
    }
    # DORMANCY: master kill-switch. When receipts are disabled the lane still runs
    # (sends/gate/dedup/audit byte-identical) but writes NOTHING — _write_detection
    # _receipt no-ops before any persistence, the disk guard is bypassed, and the
    # per-cycle summary is suppressed. A single breadcrumb fires per process.
    receipts_enabled = settings.DETECTION_RECEIPTS_ENABLED
    if not receipts_enabled:
        receipt_counters["receipts_disabled"] = True
        _log_receipts_disabled_once()
    # Disk-pressure fail-closed guard (only when receipts are actually accruing).
    # If the box is low on space, SUSPEND receipt accrual for this cycle: mark
    # coverage unhealthy, page the operator (rate-limited), and preserve existing
    # receipts+manifests. The SEND PATH is never touched, and we NEVER silently
    # prune/sample/reduce detail.
    _disk = (
        check_disk_pressure(db._db_path, settings)
        if (receipts_enabled and settings.DETECTION_RECEIPT_DISK_GUARD_ENABLED)
        else None
    )
    if _disk is not None and not _disk.healthy:
        receipt_counters["disk_suspended"] = True
        # The structured WARNING stays per-cycle (journal signal).
        log.warning(
            "detection_receipt_disk_pressure",
            reason=_disk.reason(),
            free_gb=round(_disk.free_bytes / 1e9, 2),
            free_pct=round(_disk.free_pct, 1),
        )
        # The Telegram PAGE is rate-limited to one per cooldown window so a
        # sustained breach (~500 cycles/day) does not page every cycle.
        if _disk_alert_due(now, settings.DETECTION_RECEIPT_DISK_ALERT_COOLDOWN_HOURS):
            try:
                # §12b: plain text (parse_mode=None), bracketed dispatched/delivered.
                log.info("detection_receipt_disk_pressure_alert_dispatched")
                await alerter.send_telegram_message(
                    f"disk pressure: detection receipt accrual SUSPENDED "
                    f"({_disk.reason()})",
                    session,
                    settings,
                    parse_mode=None,
                    source="detection_receipt_disk_pressure",
                )
                log.info("detection_receipt_disk_pressure_alert_delivered")
            except Exception as _e:  # never break the lane on an alert failure
                log.warning("detection_receipt_disk_pressure_alert_failed", err=str(_e))
    try:
        remaining = settings.DETECTION_ALERT_MAX_PER_DAY - await _count_sent_today(
            db, now
        )
        if remaining <= 0:
            # Cap already spent — do NOT early-return: overflow candidates that
            # WOULD have fired are still audited as detection_lane:rate_limit so
            # the suppression is quantifiable.
            log.info(
                "detection_alert_daily_cap_reached",
                cap=settings.DETECTION_ALERT_MAX_PER_DAY,
            )

        # Collect CG-sourced, fresh candidates that clear the ALR-02 quality
        # gate; then spend the scarce daily budget HIGHEST-SCORE-FIRST (freshest
        # breaks ties). ``pool`` counts CG-sourced fresh candidates before the
        # gate so the pool→gated→sent funnel is queryable per run.
        pool = 0
        entries: list[tuple[int, float, object, float | None, str | None]] = []
        for cand in candidates:
            # ---- INPUT-FILTER ZONE (pre-evaluation boundary) ----------------
            # Rejections here are FILTERED INPUTS, NOT evaluated candidates: no
            # substantive decision logic (score read, gate comparison,
            # arm-relevant branching) has run yet, so NO receipt is emitted. Each
            # reason is counted SEPARATELY (reviewer amendment 2) so filtered
            # inputs never disappear silently and never inflate `evaluated`.
            try:
                chain = getattr(cand, "chain", None)
                token_id = getattr(cand, "contract_address", None)
            except Exception:
                # A candidate so malformed its own attributes cannot be read.
                receipt_counters["filtered_malformed"] += 1
                continue
            if chain != "coingecko":
                receipt_counters["filtered_non_cg_source"] += 1
                continue
            if not token_id:
                receipt_counters["filtered_missing_id"] += 1
                continue
            # ===== EVALUATION BOUNDARY =======================================
            # Past this line substantive decision logic executes (the first read
            # of decision input — the authoritative first_seen/observation time),
            # so EVERY terminal decision below MUST emit exactly one receipt.
            first_seen_iso, mcap_db = await _fetch_first_seen_mcap(db, token_id)
            age_min = _age_minutes(first_seen_iso, now)
            if age_min is None or age_min > settings.DETECTION_ALERT_MAX_AGE_MIN:
                # Terminal decision: too old (or unparseable observation time).
                await _write_detection_receipt(
                    db,
                    settings,
                    receipt_counters,
                    token_id=token_id,
                    outcome="too_old",
                    reason=(
                        "no_first_seen"
                        if age_min is None
                        else f"age_min={int(age_min)}"
                    ),
                    cand=cand,
                    source_observation_ts=first_seen_iso,
                    decided_at=decided_at,
                    extra_inputs={
                        "age_min": age_min,
                        "max_age_min": settings.DETECTION_ALERT_MAX_AGE_MIN,
                        "first_seen_missing": first_seen_iso is None,
                    },
                )
                continue
            pool += 1
            # ALR-02 quality gate — upstream of universe/trigger/dedup/cap.
            if not _passes_quality_gate(cand, settings):
                # Terminal decision: the previously-silent gate-FAILER cohort.
                # score_before/after + comparator + threshold on the receipt make
                # the exact gate arithmetic reproducible.
                await _write_detection_receipt(
                    db,
                    settings,
                    receipt_counters,
                    token_id=token_id,
                    outcome="gate_fail_quality",
                    reason=None,
                    cand=cand,
                    source_observation_ts=first_seen_iso,
                    decided_at=decided_at,
                    extra_inputs={"age_min": age_min},
                )
                continue
            quant_score = int(getattr(cand, "quant_score", None) or 0)
            # Gate-passers defer their terminal receipt to the second loop (each
            # entry reaches exactly one terminal decision below).
            entries.append((quant_score, age_min, cand, mcap_db, first_seen_iso))
        # Highest score first; freshest (smallest age) breaks ties. NOTE: this
        # ordering only changes WHICH candidates win slots when the gated pool
        # exceeds the cap. At the default bar (>=1) the gated pool is ~4/day —
        # below DETECTION_ALERT_MAX_PER_DAY=5 — so ordering is not load-bearing
        # until the operator loosens the gate or CG detection volume rises.
        entries.sort(key=lambda e: (-e[0], e[1]))

        sent_count = 0
        for quant_score, age_min, cand, mcap_db, first_seen_iso in entries:
            token_id = cand.contract_address

            # Universe filter (reused verbatim). Off when the flag is off.
            pattern = _check_universe(settings, token_id)
            if pattern is not None:
                await _log_detection_outcome(
                    db,
                    token_id=token_id,
                    outcome="blocked_eligibility",
                    detail=f"detection_lane:universe_filter:{pattern}",
                    now=now,
                )
                await _write_detection_receipt(
                    db,
                    settings,
                    receipt_counters,
                    token_id=token_id,
                    outcome="universe_filter",
                    reason=pattern,
                    cand=cand,
                    source_observation_ts=first_seen_iso,
                    decided_at=decided_at,
                    extra_inputs={"universe_pattern": pattern},
                )
                log.info(
                    "detection_alert_blocked_universe",
                    token_id=token_id,
                    pattern=pattern,
                )
                continue

            # Trigger: early vs CG trending.
            lead_time_min, status = await _compute_lead_time_vs_trending(
                db, token_id, now
            )
            if not _detection_trigger(lead_time_min, status):
                # Terminal decision: gate-passer CG has already trended past.
                # Previously an unlogged `continue`; the receipt makes this
                # (evaluated-but-not-early) cohort recoverable (LOCK 1). Preserve
                # the pre-index trending timestamp that drove the classification
                # (reviewer amendment 1) so not_early is auditable as flow
                # attrition — never a primary-arm hit/miss.
                pre_index_trending_at = await _fetch_first_trending_at(db, token_id)
                await _write_detection_receipt(
                    db,
                    settings,
                    receipt_counters,
                    token_id=token_id,
                    outcome="not_early",
                    reason=status,
                    cand=cand,
                    source_observation_ts=first_seen_iso,
                    decided_at=decided_at,
                    extra_inputs={
                        "lead_time_min": lead_time_min,
                        "lead_time_status": status,
                        "pre_index_trending_at": pre_index_trending_at,
                    },
                )
                continue

            # Per-token 24h dedup (scoped to the detection lane).
            if await _check_detection_dedup(db, settings, token_id, now):
                await _log_detection_outcome(
                    db,
                    token_id=token_id,
                    outcome="blocked_cooldown",
                    detail="detection_lane:dedup_24h",
                    now=now,
                )
                await _write_detection_receipt(
                    db,
                    settings,
                    receipt_counters,
                    token_id=token_id,
                    outcome="dedup_24h",
                    reason=None,
                    cand=cand,
                    source_observation_ts=first_seen_iso,
                    decided_at=decided_at,
                    extra_inputs={
                        "dedup_window_hours": settings.TG_ALERT_DEDUP_WINDOW_HOURS
                    },
                )
                continue

            # Daily budget guard (freshest-first already ordered above).
            # LOG-ONLY (tg_alert_log): an un-sent fresh+early token re-hits this
            # branch every cycle until it ages out, so a DB audit row per hit
            # flooded tg_alert_log (~9.8K rows/day observed 2026-07-11..12 vs 10
            # sent). The structlog line preserves observability; the dedup_24h
            # and sent rows remain the durable tg_alert_log trail. The decision
            # receipt (idempotency-collapsed per token+state) does NOT re-flood:
            # every cycle re-hit maps to the same key → a single row.
            if remaining <= 0:
                log.info(
                    "detection_lane_rate_limited",
                    token_id=token_id,
                )
                await _write_detection_receipt(
                    db,
                    settings,
                    receipt_counters,
                    token_id=token_id,
                    outcome="rate_limit",
                    reason=None,
                    cand=cand,
                    source_observation_ts=first_seen_iso,
                    decided_at=decided_at,
                    extra_inputs={
                        "remaining": remaining,
                        "cap": settings.DETECTION_ALERT_MAX_PER_DAY,
                    },
                )
                continue

            price, mcap_pc = await _fetch_price_mcap(db, token_id)
            mcap = mcap_db if mcap_db else mcap_pc
            body = format_detection_alert(
                symbol=getattr(cand, "ticker", "") or "",
                coin_id=token_id,
                price=price,
                mcap=mcap,
                first_seen_min_ago=age_min,
                lead_time_min=lead_time_min,
                lead_time_status=status,
                dashboard_base_url=settings.DASHBOARD_BASE_URL,
            )
            # §12b: bracket the send with dispatched/delivered logs so every
            # fire is traceable regardless of delivery outcome.
            log.info("detection_alert_dispatched", token_id=token_id)
            try:
                await alerter.send_telegram_message(
                    body,
                    session,
                    settings,
                    parse_mode=None,
                    raise_on_failure=True,
                    source="detection_alert",
                )
            except Exception as e:
                log.warning(
                    "detection_alert_dispatch_failed",
                    token_id=token_id,
                    err=str(e),
                )
                # A failed send neither burns budget nor claims dedup.
                await _log_detection_outcome(
                    db,
                    token_id=token_id,
                    outcome="dispatch_failed",
                    detail="detection_lane",
                    now=now,
                )
                await _write_detection_receipt(
                    db,
                    settings,
                    receipt_counters,
                    token_id=token_id,
                    outcome="dispatch_failed",
                    reason=str(e),
                    cand=cand,
                    source_observation_ts=first_seen_iso,
                    decided_at=decided_at,
                    extra_inputs={
                        "lead_time_min": lead_time_min,
                        "lead_time_status": status,
                    },
                )
                continue
            log.info("detection_alert_delivered", token_id=token_id)
            await _log_detection_outcome(
                db,
                token_id=token_id,
                outcome="sent",
                detail="detection_lane",
                now=now,
            )
            # Receipt AFTER the send + tg_alert_log write: a receipt-write
            # failure here cannot un-send or change budget accounting (LOCK 4).
            await _write_detection_receipt(
                db,
                settings,
                receipt_counters,
                token_id=token_id,
                outcome="sent",
                reason=None,
                cand=cand,
                source_observation_ts=first_seen_iso,
                decided_at=decided_at,
                extra_inputs={
                    "lead_time_min": lead_time_min,
                    "lead_time_status": status,
                },
            )
            remaining -= 1
            sent_count += 1

        # Queryable per-run funnel: how many CG-fresh candidates entered the
        # pool, how many the quality gate dropped, how many were eligible, and
        # how many were actually sent within the cap.
        log.info(
            "detection_alert_funnel",
            pool=pool,
            gated_out=pool - len(entries),
            eligible=len(entries),
            sent=sent_count,
            cap=settings.DETECTION_ALERT_MAX_PER_DAY,
        )
        # Reviewer LOCK 4 + corrections 2 & 3 + two-identity naming: per-cycle
        # receipt reconciliation. Identity (must hold every cycle):
        #   evaluated_n == newly_written_n + exact_replays_n
        #              + write_failures_n + conflicting_duplicates_n
        # If it does not hold, or receipts otherwise cannot be reconciled, the
        # cohort is INVALID (documented in the prereg). conflicting_duplicates_n
        # must be 0 for healthy coverage; under evaluation-identity a clean cycle
        # following a prior cycle shows newly_written_n == evaluated_n with
        # exact_replays_n == conflicting_duplicates_n == 0. filtered_*_n are
        # reported BESIDE the identity (excluded from evaluated_n) so pre-boundary
        # rejections stay visible by reason and never inflate the evaluated cohort.
        # ``coverage_healthy`` is False when disk-pressure suspended accrual
        # (skipped_disk_pressure_n > 0): the identity intentionally does NOT close
        # then, flagging the coverage gap so the cohort window is marked invalid.
        # DORMANCY: when receipts are disabled the summary is SUPPRESSED entirely
        # (the single detection_receipts_disabled breadcrumb already fired) so a
        # dormant deployment produces no per-cycle receipt telemetry.
        if not receipt_counters["receipts_disabled"]:
            log.info(
                "detection_receipt_summary",
                evaluated_n=receipt_counters["evaluated"],
                newly_written_n=receipt_counters["newly_written"],
                exact_replays_n=receipt_counters["exact_replays"],
                write_failures_n=receipt_counters["write_failures"],
                conflicting_duplicates_n=receipt_counters["conflicts"],
                skipped_disk_pressure_n=receipt_counters["skipped_disk_pressure"],
                filtered_missing_id_n=receipt_counters["filtered_missing_id"],
                filtered_non_cg_source_n=receipt_counters["filtered_non_cg_source"],
                filtered_malformed_n=receipt_counters["filtered_malformed"],
                coverage_healthy=not receipt_counters["disk_suspended"],
            )
    except Exception:
        # Belt-and-braces: the lane must never break the pipeline cycle.
        log.exception("detection_alert_notify_unexpected_error")

"""TG actionability shadow — Stage A counterfactual evaluator and writer.

For every `tg_social_signals` row that reaches `resolution_state='RESOLVED'`,
decide what a TG actionability gate WOULD have said, and persist that decision
— while the lane stays quarantined, `enabled=0`, `live_eligible=0`,
`tg_alert_eligible=0`. Nothing here touches any of those four controls, or
`scout/trading/actionability.py`, or the dispatch quarantine. The point is to
break the self-sealing loop where ACT sits behind the quarantine, so the lane
can never accumulate the evidence that would justify unblocking it.

Three properties this module exists to guarantee:

**Decisions are reproducible.** `decision_as_of` is ALWAYS the signal row's
`created_at`, never wall clock. Feature inputs come through a provider that is
required to be as-of-time honest, and the resolver facts come from the
persisted snapshot only — never a refetch. Re-running a decision tomorrow, on
a database that has since learned more about the caller, must produce
byte-identical `features_json`. Otherwise catch-up after a disabled window
would quietly decide old calls on new information.

**Decisions are immutable and singular.** Exactly one row per
`(signal_id, gate_version)`, enforced by the DB. Rows are never UPDATEd or
DELETEd. Replay collides on the UNIQUE index and is suppressed; every OTHER
integrity failure propagates loudly.

**Versions bind semantics, not commits.** `gate_version` is derived from a
fingerprint over the thresholds AND the source hashes of the decision-producing
modules. Changing extraction semantics without renaming a field or moving a
threshold still splits the cohort; an unrelated commit elsewhere in the repo
does not.

Stage A ships NO real feature provider — production wiring registers nothing.
`TG_SHADOW_ENABLED=true` without a registered provider refuses to arm loudly
rather than half-activating on a placeholder feature set.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

import structlog

from scout.config import Settings
from scout.db import Database
from scout.social.telegram import snapshot as snapshot_module

log = structlog.get_logger()

# Bumped when the DECISION RULE changes (new check, reordered checks, changed
# comparison). Declared alongside the module hash so an intentional semantic
# change is legible in review, not merely visible as a digest that moved.
SHADOW_EVALUATOR_SEMANTIC_VERSION = "tg-shadow-eval-v1"

GATE_VERSION_PREFIX = "tg-shadow-v1+"

# Number of fingerprint hex chars carried in `gate_version`. The FULL digest
# is persisted inside `features_json` — the short form is for reading logs and
# grouping rows, never the authority on which config produced a decision.
_GATE_VERSION_FINGERPRINT_CHARS = 16

# Health-table component PREFIX. The scan heartbeat is bound to the generation
# that produced it — `tg_shadow_scan:<gate_version>` — so an old generation's
# heartbeat can never satisfy the current generation's watchdog condition. A
# single shared `tg_shadow_writer` row would do exactly that: after a threshold
# bump the previous generation's fresh heartbeat would keep reporting "a scan
# completed recently" while the NEW generation's writer was doing nothing at
# all, which is the dead-writer case the watchdog exists to catch.
SHADOW_SCAN_COMPONENT_PREFIX = "tg_shadow_scan:"


def scan_heartbeat_component(gate_version: str) -> str:
    """`tg_social_health.component` for one generation's scan heartbeat."""
    return f"{SHADOW_SCAN_COMPONENT_PREFIX}{gate_version}"


# Which generation the ENABLED writer is actually operating under, published
# durably so a read-only observer never has to guess.
#
# Registry chronology cannot answer this. Re-enable semantics resume an
# EXISTING gate_version without rewriting its registry row, so after
# v1 -> v2 -> (operator returns to the v1 configuration) the newest
# `activated_at` is still v2 while the writer runs v1. An observer that picked
# the latest row would page that v2 has no fresh heartbeat -- which is true and
# irrelevant -- while v1, the generation actually producing evidence, went
# unwatched. The writer is the only component that knows; so the writer says so.
SHADOW_ACTIVE_GENERATION_COMPONENT = "tg_shadow_active_generation"
ACTIVE_GENERATION_DETAIL_PREFIX = "gate_version="


def active_generation_detail(gate_version: str) -> str:
    """`tg_social_health.detail` payload naming the active generation.

    Parsed by `scripts/check_tg_shadow_lag.py`; a test pins the two together.
    """
    return f"{ACTIVE_GENERATION_DETAIL_PREFIX}{gate_version}"


# Every reason the evaluator can emit. Enumerated so the reason distribution
# can later answer "how many rejections were weak-caller vs missing-liquidity
# vs unpriceable-identity" instead of collapsing to a single "blocked".
SHADOW_REASONS = (
    "shadow_pass",
    "shadow_block_snapshot_missing",
    "shadow_block_missing_mcap",
    "shadow_block_identity_unpriceable_class",
    "shadow_block_safety_not_passed",
    "shadow_block_mcap_band",
    "shadow_block_liquidity_unknown",
    "shadow_block_caller_insufficient_n",
    "shadow_block_caller_quality",
    "shadow_block_duplicate_call",
)

# Settings that participate in the fingerprint: DECISION AND EVIDENCE
# SEMANTICS ONLY -- every value the evaluator actually reads, and nothing else.
#
# Two categories are deliberately ABSENT:
#
#   `TG_SHADOW_ENABLED` -- an activation control. A disable/enable cycle must
#   RESUME the existing generation rather than start a new one, which it could
#   not do if the flag's own value moved the fingerprint.
#
#   `TG_SHADOW_LAG_THRESHOLD_MIN` / `TG_SHADOW_SCAN_CADENCE_MIN` -- operational
#   watchdog tuning. They change when the OPERATOR is paged, never what a
#   decision is. Binding them would mean retuning an alert cadence starts a new
#   evidence generation and resets the >=30-decision accumulation toward the
#   feature freeze -- discarding real evidence to record a monitoring
#   preference.
_FINGERPRINTED_SETTING_NAMES = (
    "TG_CALLER_MIN_ELIGIBLE_CLUSTERS",
    "TG_SHADOW_MCAP_MAX_USD",
    "TG_SHADOW_MCAP_MIN_USD",
    "TG_SHADOW_MIN_CALLER_COVERAGE",
    "TG_SHADOW_REQUIRE_LIQUIDITY",
    "TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES",
)


# ---------------------------------------------------------------------------
# Caller-feature provider interface (implemented by Stage B)
# ---------------------------------------------------------------------------


@runtime_checkable
class CallerFeatureProvider(Protocol):
    """As-of-decision-time caller features.

    `features()` returns ONE flat dict carrying two explicitly separated
    groups, distinguished by key prefix so the split is mechanical rather than
    documented:

    * ``history_*`` — the caller's track record, computed strictly EXCLUDING
      ``current_signal_id`` and its message row. Shadow evaluation runs after
      persistence, so a naive ``created_at <= as_of`` query would include the
      signal being scored and let a call improve its own caller's reputation.
    * ``event_*`` — context describing THIS call (duplicate state,
      cross-channel corroboration). May include the current signal, because
      these describe the event rather than the caller's history.

    Implementations MUST be deterministic given
    ``(channel_handle, decision_as_of, current_signal_id)``: recomputing at any
    later wall-clock time has to return the same values, or replay stops being
    replay.
    """

    caller_feature_semantic_version: str
    module_source_hash: str

    def feature_schema(self) -> list[tuple[str, str]]:
        """Ordered ``(field_name, type_name)`` pairs. Part of the fingerprint."""
        ...

    def features(
        self,
        channel_handle: str,
        decision_as_of: datetime,
        current_signal_id: int,
    ) -> dict[str, Any]: ...


_registered_provider: CallerFeatureProvider | None = None


def register_caller_feature_provider(provider: CallerFeatureProvider) -> None:
    """Register the process-wide provider. Stage B calls this from production
    wiring; Stage A production wiring calls it with nothing."""
    global _registered_provider
    _registered_provider = provider
    log.info(
        "tg_shadow_feature_provider_registered",
        caller_feature_semantic_version=provider.caller_feature_semantic_version,
    )


def get_registered_provider() -> CallerFeatureProvider | None:
    return _registered_provider


def clear_registered_provider() -> None:
    """Drop the registered provider. Used by test teardown — module-level
    state that survives a test silently changes the next one's fingerprint."""
    global _registered_provider
    _registered_provider = None


# ---------------------------------------------------------------------------
# Version binding
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def module_source_hash() -> str:
    """SHA-256 of this module's source. See `snapshot.module_source_hash`.

    Line endings normalized to `\\n` first: a CRLF checkout and an LF checkout
    of the SAME commit must produce the same gate_version, or a locally
    recomputed fingerprint can never match production's.
    """
    return snapshot_module.hash_module_source(Path(__file__))


def compute_config_fingerprint(
    settings: Settings, provider: CallerFeatureProvider
) -> str:
    """SHA-256 over the canonical JSON of everything that decides an outcome.

    Deliberately NOT the deployed repo SHA: unrelated commits must not split
    cohorts. Only the decision-producing surface is bound — the two module
    sources, the declared semantic versions, the feature schema, the snapshot
    schema version, and the thresholds. A cosmetic edit to one of those two
    modules DOES split a cohort; that is the accepted cheap side of making the
    binding mechanical rather than memory-dependent.
    """
    payload = {
        "evaluator_semantic_version": SHADOW_EVALUATOR_SEMANTIC_VERSION,
        "evaluator_module_hash": module_source_hash(),
        "caller_feature_semantic_version": provider.caller_feature_semantic_version,
        "caller_feature_module_hash": provider.module_source_hash,
        "feature_schema": [list(pair) for pair in provider.feature_schema()],
        # Criterion 3: the snapshot IS a decision input, so its schema version
        # and its producer's identity are part of what a gate_version means.
        "resolution_snapshot_schema_version": snapshot_module.SNAPSHOT_SCHEMA_VERSION,
        "snapshot_producer_semantic_version": (
            snapshot_module.SNAPSHOT_PRODUCER_SEMANTIC_VERSION
        ),
        "snapshot_module_hash": snapshot_module.module_source_hash(),
        "thresholds": _fingerprinted_thresholds(settings),
    }
    canonical = snapshot_module.canonical_json_dumps(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprinted_thresholds(settings: Settings) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in _FINGERPRINTED_SETTING_NAMES:
        value = getattr(settings, name)
        # Sorted so an operator re-ordering a list in `.env` does not read as
        # a policy change.
        values[name] = sorted(value) if isinstance(value, (list, tuple)) else value
    return values


def gate_version_for(config_fingerprint: str) -> str:
    return GATE_VERSION_PREFIX + config_fingerprint[:_GATE_VERSION_FINGERPRINT_CHARS]


def current_gate_version(
    settings: Settings, provider: CallerFeatureProvider
) -> tuple[str, str]:
    """Returns ``(gate_version, full_config_fingerprint)``."""
    fingerprint = compute_config_fingerprint(settings, provider)
    return gate_version_for(fingerprint), fingerprint


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowDecision:
    """Immutable evaluator output. `gate_version` is stamped by the caller —
    it is an activation-time property of the CONFIG, not of one signal."""

    actionable: bool
    reason: str


def identity_class_for(signal_row: Mapping[str, Any]) -> str:
    """Coarse priceability class of a signal's identity shape.

    `dex:{chain}:{addr}` collapses to `dex:{chain}` — Solana coverage must
    never mask an unsupported Robinhood path, so the classes stay separate.
    Anything else is a CoinGecko id.
    """
    token_id = str(signal_row.get("token_id") or "")
    if token_id.startswith("dex:"):
        parts = token_id.split(":")
        return f"dex:{parts[1]}" if len(parts) >= 3 and parts[1] else "dex:unknown"
    return "coingecko"


def evaluate_tg_actionability_shadow(
    signal_row: Mapping[str, Any],
    snapshot: dict[str, Any] | None,
    features: Mapping[str, Any],
    settings: Settings,
) -> ShadowDecision:
    """Deterministic, thresholded, no ML and no LLM.

    Check order is part of the contract, not an implementation detail: the
    reason distribution is the deliverable, and reordering these would silently
    re-attribute rejections between causes across a generation boundary.
    Cheap structural facts are checked before caller statistics so a signal is
    never rejected for "weak caller" when it was actually unpriceable.

    Missing feature keys resolve to the conservative value (no history, no
    coverage, not a duplicate) rather than raising — a provider that omits a
    field must produce a visible block, not an exception that the live hook
    swallows.
    """
    # 1. Snapshot is the only permitted recovery source for decision inputs.
    #    Missing or unparseable is a first-class decision, never a silent skip.
    if snapshot is None:
        return ShadowDecision(False, "shadow_block_snapshot_missing")

    # 2. mcap is the one field ACT reads today; without it there is no gate.
    mcap = signal_row.get("mcap_at_sighting")
    if mcap is None or mcap <= 0:
        return ShadowDecision(False, "shadow_block_missing_mcap")

    # 3. Identity shape. The class list is EMPTY until Stage C's PQ verdict
    #    names a stratum, so today only a missing CA can trip this.
    if not signal_row.get("contract_address") or identity_class_for(signal_row) in set(
        settings.TG_SHADOW_UNPRICEABLE_IDENTITY_CLASSES
    ):
        return ShadowDecision(False, "shadow_block_identity_unpriceable_class")

    # 4. Fail-closed on safety: "check did not complete" and "check said unsafe"
    #    are both blocks here. The shadow is not the place to re-litigate the
    #    per-channel lenient path — that is a dispatcher policy.
    if not (
        snapshot.get("safety_check_completed") is True
        and snapshot.get("safety_pass") is True
    ):
        return ShadowDecision(False, "shadow_block_safety_not_passed")

    # 5. Mcap band.
    if not (settings.TG_SHADOW_MCAP_MIN_USD <= mcap <= settings.TG_SHADOW_MCAP_MAX_USD):
        return ShadowDecision(False, "shadow_block_mcap_band")

    # 6. Liquidity is real for DexScreener-resolved tokens (the resolver
    #    forwards the value it already read to pick the deepest pair) and null
    #    for CG-resolved and cashtag rows, whose responses carry no equivalent
    #    field. So this check is OFF by default not because the data is
    #    universally missing, but because turning it on is a cohort-shaping
    #    decision: it would block every CG and cashtag row, making the gate
    #    operationally "DexScreener-resolved rows only".
    if settings.TG_SHADOW_REQUIRE_LIQUIDITY and snapshot.get("liquidity_usd") is None:
        return ShadowDecision(False, "shadow_block_liquidity_unknown")

    # 7. Min-N on ELIGIBLE DISTINCT CLUSTERS, not raw message count — a caller
    #    who posts the same token ten times has one cluster, not ten samples.
    clusters = features.get("history_eligible_distinct_clusters") or 0
    if clusters < settings.TG_CALLER_MIN_ELIGIBLE_CLUSTERS:
        return ShadowDecision(False, "shadow_block_caller_insufficient_n")

    # 8. Coverage on the PRICEABLE denominator (#482): a caller whose calls
    #    cannot be measured is not a caller with bad returns, and the two must
    #    not be averaged together.
    coverage = features.get("history_priceable_coverage_rate") or 0.0
    if coverage < settings.TG_SHADOW_MIN_CALLER_COVERAGE:
        return ShadowDecision(False, "shadow_block_caller_quality")

    # 9. Re-emphasis of a call already in this cluster.
    duplicate_rank = features.get("event_duplicate_rank_in_cluster")
    if duplicate_rank is not None and duplicate_rank > 1:
        return ShadowDecision(False, "shadow_block_duplicate_call")

    return ShadowDecision(True, "shadow_pass")


def build_features_json(
    *,
    config_fingerprint: str,
    gate_version: str,
    decision_as_of: str,
    features: Mapping[str, Any],
    snapshot: dict[str, Any] | None,
) -> str:
    """Canonical JSON of everything the decision saw.

    Contains NO wall-clock value: `decision_as_of` is the signal's own
    `created_at`. That is what makes a catch-up write byte-identical to the
    live write it is replacing.
    """
    return snapshot_module.canonical_json_dumps(
        {
            "config_fingerprint": config_fingerprint,
            "gate_version": gate_version,
            "decision_as_of": decision_as_of,
            "evaluator_semantic_version": SHADOW_EVALUATOR_SEMANTIC_VERSION,
            "resolution_snapshot_schema_version": (
                snapshot_module.SNAPSHOT_SCHEMA_VERSION
            ),
            "features": dict(features),
            "snapshot": snapshot,
        }
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _require_conn(db: Database):
    if db._conn is None or db._txn_lock is None:
        raise RuntimeError("Database not initialized.")
    return db._conn


async def write_shadow_decision(
    db: Database,
    *,
    signal_id: int,
    gate_version: str,
    decision: ShadowDecision,
    features_json: str,
) -> bool:
    """Append one decision. Returns True if a row was written.

    `ON CONFLICT(signal_id, gate_version) DO NOTHING` — NOT blanket
    `INSERT OR IGNORE`, which would also swallow NOT NULL / CHECK / FK
    failures. Only the one known-benign conflict is suppressed; everything
    else propagates.

    Written rows are counted from THIS statement's `rowcount`, never the
    connection-wide `total_changes`: on a shared connection another writer's
    changes land in that counter, and a marker stamped from it reports success
    for a write that never happened.
    """
    conn = _require_conn(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        try:
            cur = await conn.execute(
                """INSERT INTO tg_act_shadow
                   (signal_id, gate_version, actionable, reason,
                    features_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(signal_id, gate_version) DO NOTHING""",
                (
                    signal_id,
                    gate_version,
                    int(decision.actionable),
                    decision.reason,
                    features_json,
                    now_iso,
                ),
            )
            written = cur.rowcount == 1
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception as rb_err:
                log.exception("tg_shadow_write_rollback_failed", err=str(rb_err))
            raise

    if not written:
        log.info(
            "tg_shadow_duplicate_skip",
            signal_id=signal_id,
            gate_version=gate_version,
        )
    return written


async def ensure_generation(db: Database, *, gate_version: str) -> bool:
    """Create the activation row for `gate_version` if absent.

    Returns True if this call created it. Callers MUST have already checked
    `TG_SHADOW_ENABLED` and a registered provider — `activated_at` is the
    activation boundary, not install time, and a row written while the flag is
    off would make every call between dark deploy and activation retroactively
    part of the generation.
    """
    conn = _require_conn(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        try:
            created = await _insert_generation_row(conn, gate_version, now_iso)
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception as rb_err:
                log.exception("tg_shadow_generation_rollback_failed", err=str(rb_err))
            raise
    return created


async def _insert_generation_row(conn, gate_version: str, now_iso: str) -> bool:
    """Registry row insert. Caller MUST hold `db._txn_lock`. True if created."""
    cur = await conn.execute(
        "INSERT INTO tg_act_shadow_generations (gate_version, activated_at) "
        "VALUES (?, ?) ON CONFLICT(gate_version) DO NOTHING",
        (gate_version, now_iso),
    )
    return cur.rowcount == 1


async def arm_generation(db: Database, *, gate_version: str) -> bool:
    """Registry row AND active-generation marker, in ONE transaction.

    Writing them separately leaves a crash window: the registry row commits,
    the process dies, and the database now holds a legitimate generation that
    no marker names. The watchdog treats that state as an inconsistency and
    pages (fail-closed), but the better fix is for the state to be
    unreachable — so activation publishes both or neither.
    """
    conn = _require_conn(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        try:
            created = await _insert_generation_row(conn, gate_version, now_iso)
            await _upsert_health_row(
                conn,
                component=SHADOW_ACTIVE_GENERATION_COMPONENT,
                detail=active_generation_detail(gate_version),
                now_iso=now_iso,
            )
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception as rb_err:
                log.exception("tg_shadow_arm_rollback_failed", err=str(rb_err))
            raise
    return created


async def get_generation_activated_at(db: Database, gate_version: str) -> str | None:
    conn = _require_conn(db)
    cur = await conn.execute(
        "SELECT activated_at FROM tg_act_shadow_generations WHERE gate_version = ?",
        (gate_version,),
    )
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def activate_shadow_generation(db: Database, settings: Settings) -> str | None:
    """Enabled-startup entry point. Returns the armed `gate_version`, or None.

    Refusing loudly is the point: a `TG_SHADOW_ENABLED=true` with no real
    provider would otherwise arm a gate_version computed against a placeholder
    feature set, and the cohort would change semantics days later when the real
    provider lands.
    """
    if not settings.TG_SHADOW_ENABLED:
        return None
    provider = get_registered_provider()
    if provider is None:
        log.warning(
            "tg_shadow_activation_refused_no_feature_provider",
            note=(
                "TG_SHADOW_ENABLED=true but no CallerFeatureProvider is "
                "registered — no generation created, nothing processed"
            ),
        )
        return None
    gate_version, fingerprint = current_gate_version(settings, provider)
    # Registry row + marker atomically: a crash between them would leave a
    # generation no marker names, which the watchdog must then page about.
    created = await arm_generation(db, gate_version=gate_version)
    log.info(
        "tg_shadow_generation_active",
        gate_version=gate_version,
        config_fingerprint=fingerprint,
        created=created,
    )
    return gate_version


# ---------------------------------------------------------------------------
# Evaluation entry points
# ---------------------------------------------------------------------------

_SIGNAL_COLUMNS = (
    "id, token_id, symbol, contract_address, chain, mcap_at_sighting, "
    "resolution_state, source_channel_handle, created_at, resolution_snapshot_json"
)


def _parse_iso_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _decide_and_write(
    db: Database,
    settings: Settings,
    provider: CallerFeatureProvider,
    *,
    signal_row: Mapping[str, Any],
    gate_version: str,
    config_fingerprint: str,
) -> bool:
    """Evaluate one row and append its decision. Returns True if a row landed.

    `decision_as_of` is the signal's `created_at` — ALWAYS, on both the live
    and the catch-up path. Using wall clock here would let a catch-up run after
    a disabled window judge an old call against a caller history that did not
    exist when the call was made.
    """
    decision_as_of_text = str(signal_row["created_at"])
    features = provider.features(
        channel_handle=str(signal_row["source_channel_handle"]),
        decision_as_of=_parse_iso_utc(decision_as_of_text),
        current_signal_id=int(signal_row["id"]),
    )
    # Persisted column ONLY — no refetch may fill a missing historical input.
    parsed_snapshot = snapshot_module.parse_resolution_snapshot(
        signal_row["resolution_snapshot_json"]
    )
    decision = evaluate_tg_actionability_shadow(
        signal_row, parsed_snapshot, features, settings
    )
    features_json = build_features_json(
        config_fingerprint=config_fingerprint,
        gate_version=gate_version,
        decision_as_of=decision_as_of_text,
        features=features,
        snapshot=parsed_snapshot,
    )
    return await write_shadow_decision(
        db,
        signal_id=int(signal_row["id"]),
        gate_version=gate_version,
        decision=decision,
        features_json=features_json,
    )


async def _armed_context(
    db: Database, settings: Settings
) -> tuple[CallerFeatureProvider, str, str, str] | None:
    """Resolve (provider, gate_version, fingerprint, activated_at) or None.

    None means "not armed" for one of three distinct reasons, each logged
    differently: flag off (silent — the normal deployed state), no provider
    (loud refusal), no generation row (the flag is on but activation has not
    run, so there is nothing to attribute decisions to)."""
    if not settings.TG_SHADOW_ENABLED:
        return None
    provider = get_registered_provider()
    if provider is None:
        log.warning("tg_shadow_activation_refused_no_feature_provider")
        return None
    gate_version, fingerprint = current_gate_version(settings, provider)
    activated_at = await get_generation_activated_at(db, gate_version)
    if activated_at is None:
        log.info("tg_shadow_no_generation_for_gate_version", gate_version=gate_version)
        return None
    return provider, gate_version, fingerprint, activated_at


async def maybe_evaluate_signal(
    *, db: Database, settings: Settings, signal_id: int
) -> bool:
    """Live post-persist hook for one freshly-written signal.

    Reads the row back from the DB rather than taking the in-memory values:
    the catch-up path can only see the persisted row, and a live decision that
    saw anything else would not be reproducible by the replay it is supposed
    to be identical to.
    """
    armed = await _armed_context(db, settings)
    if armed is None:
        return False
    provider, gate_version, fingerprint, activated_at = armed

    conn = _require_conn(db)
    cur = await conn.execute(
        f"SELECT {_SIGNAL_COLUMNS} FROM tg_social_signals WHERE id = ?",
        (signal_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    signal_row = dict(row)
    if signal_row["resolution_state"] != "RESOLVED":
        return False
    # Generation cutover: pre-activation rows are outside every generation.
    # Both sides are `datetime.now(timezone.utc).isoformat()` strings from the
    # same producer, so the lexicographic compare is the chronological one.
    if str(signal_row["created_at"]) < activated_at:
        return False

    return await _decide_and_write(
        db,
        settings,
        provider,
        signal_row=signal_row,
        gate_version=gate_version,
        config_fingerprint=fingerprint,
    )


async def scan_and_evaluate(db: Database, settings: Settings) -> dict[str, Any]:
    """Catch-up scan: every eligible row lacking a current-generation decision.

    Eligible means RESOLVED, `created_at >= activated_at`, and no
    `tg_act_shadow` row for the CURRENT gate_version. A signal that became
    RESOLVED while the flag was off satisfies that and is picked up here — that
    is what makes disable/enable resume a generation instead of losing a window.

    On completion this emits `tg_shadow_scan_complete` AND upserts the
    `tg_social_health` heartbeat row for THIS gate_version. The health row is
    watchdog state, not evidence: it is upsert-by-design and says only "a scan
    for this generation finished at T", which is exactly what lets the watchdog
    distinguish "writer scanning but rows still unshadowed" from "writer not
    scanning at all" — without letting a retired generation's heartbeat vouch
    for the current one.
    """
    armed = await _armed_context(db, settings)
    if armed is None:
        return {"scanned": 0, "written": 0, "armed": False}
    provider, gate_version, fingerprint, activated_at = armed

    conn = _require_conn(db)
    cur = await conn.execute(
        f"""SELECT {_SIGNAL_COLUMNS}
              FROM tg_social_signals s
             WHERE s.resolution_state = 'RESOLVED'
               AND s.created_at >= ?
               AND NOT EXISTS (
                     SELECT 1 FROM tg_act_shadow a
                      WHERE a.signal_id = s.id
                        AND a.gate_version = ?
                   )
             ORDER BY s.created_at, s.id""",
        (activated_at, gate_version),
    )
    rows = [dict(r) for r in await cur.fetchall()]

    written = 0
    for signal_row in rows:
        if await _decide_and_write(
            db,
            settings,
            provider,
            signal_row=signal_row,
            gate_version=gate_version,
            config_fingerprint=fingerprint,
        ):
            written += 1

    log.info(
        "tg_shadow_scan_complete",
        scanned=len(rows),
        written=written,
        gate_version=gate_version,
    )
    await _record_scan_heartbeat(
        db, gate_version=gate_version, scanned=len(rows), written=written
    )
    return {
        "scanned": len(rows),
        "written": written,
        "armed": True,
        "gate_version": gate_version,
    }


async def _upsert_health_row(conn, *, component: str, detail: str, now_iso: str):
    """One `tg_social_health` upsert. Caller MUST hold `db._txn_lock`.

    UPSERT, not REPLACE: REPLACE deletes the row first and would clobber any
    column this statement does not name.
    """
    await conn.execute(
        """INSERT INTO tg_social_health
           (component, listener_state, last_message_at, updated_at, detail)
           VALUES (?, 'running', ?, ?, ?)
           ON CONFLICT(component) DO UPDATE SET
             listener_state = excluded.listener_state,
             last_message_at = excluded.last_message_at,
             updated_at = excluded.updated_at,
             detail = excluded.detail""",
        (component, now_iso, now_iso, detail),
    )


async def _record_scan_heartbeat(
    db: Database, *, gate_version: str, scanned: int, written: int
) -> None:
    """Scan heartbeat + active-generation refresh, in ONE transaction.

    Both rows describe the same completed scan, so a crash between them would
    leave the watchdog reading a heartbeat for a generation the marker does not
    name — exactly the disagreement this marker exists to remove.
    """
    conn = _require_conn(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    component = scan_heartbeat_component(gate_version)
    detail = f"gate_version={gate_version} scanned={scanned} written={written}"
    async with db._txn_lock:
        try:
            await _upsert_health_row(
                conn, component=component, detail=detail, now_iso=now_iso
            )
            await _upsert_health_row(
                conn,
                component=SHADOW_ACTIVE_GENERATION_COMPONENT,
                detail=active_generation_detail(gate_version),
                now_iso=now_iso,
            )
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception as rb_err:
                log.exception("tg_shadow_heartbeat_rollback_failed", err=str(rb_err))
            raise

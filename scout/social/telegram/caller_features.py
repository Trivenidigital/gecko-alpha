"""Stage B — the real `CallerFeatureProvider` for the TG actionability shadow.

Reconstructs a caller's track record AS OF a decision time from inputs whose
immutability was verified against master: the INSERT-only `tg_social_messages`
and `tg_social_signals` tables, and the forward-only
`source_call_price_snapshots` series. `source_calls` is excluded entirely —
its writer upserts essentially every payload column, so a value read from it
today is not the value that existed at the decision time being reproduced.
This module reuses the source-quality ledger's *axes* (what a feature means)
while re-deriving every one of them from those raw rows; no mutable derived
column is ever consumed as if it had existed historically.

**The determinism invariant.** Recomputing `features()` for the same
`(channel_handle, decision_as_of, current_signal_id)` at any later wall-clock
time must return byte-identical values, however much the database has learned
since. Two mechanisms make that true rather than hoped-for:

* Every input row is admitted only under a knowability bound — one clause for
  when the fact HAPPENED, one for when we RECORDED it, and on prices a third
  for when it became READABLE. A message fact needs `posted_at <= as_of` AND
  `parsed_at <= as_of`; a signal fact needs `created_at <= as_of`; a price
  observation needs `snapshot_at <= as_of` AND `created_at <= as_of` AND a
  commit-visibility marker at or before `as_of`. A call posted before the
  decision but *ingested* after it was not knowable then and is excluded now —
  and the same is true of a price stamped before the decision but recorded, or
  committed, after it. Prices earn the extra clauses for concrete reasons: the
  snapshot writer stamps `snapshot_at` ONCE at cycle start and commits the
  whole cycle ONCE at the end, so forward-only monotonicity does NOT imply
  committed-by-T; and `created_at` is stamped at INSERT, not at COMMIT, so it
  does not imply readable-by-T either.
* Every horizon-bound statistic additionally requires its measurement window
  to have been eligible to close: a prior call contributes a forward return,
  or enters a coverage denominator, only once `posted_at + window_end <=
  as_of`. "Exclude what is still pending as of today" is explicitly NOT the
  rule — that leaks future outcomes into a historical decision.

Consequently a 10-minute-old call does not depress 30m coverage: it is not yet
in that denominator at all, and it enters once its 45-minute window closes.

**Residual on the price bound — the defect the third clause exists to close.**
`source_call_price_snapshots.created_at` is stamped per row at INSERT, not at
COMMIT. A row INSERTed before `as_of` but COMMITted after it therefore passes
both clauses and is still admitted. The exposure is the per-row insert→commit
gap, and it is NOT uniformly small: the FIRST row inserted in a long writer
cycle waits for every later row before the single end-of-cycle commit, so its
exposure approaches the full cycle duration — only the LAST-inserted rows get
a genuinely tight bound. Read as "commit latency, therefore milliseconds" this
would be understated by orders of magnitude on exactly the early rows.
What the bound still buys: `snapshot_at` alone gave EVERY row in a cycle the
same cycle-start stamp, so the exposure was full-cycle for all of them; adding
`created_at` makes it decay across the cycle instead. Strictly better on every
row, dramatically better on most, worst case unchanged.
**CLOSED as of the commit-visibility marker.** The paragraph above describes the
defect and why a read-side module could not fix it alone; it is retained because
the reasoning still explains WHY the third bound exists. The substrate now writes
a durable post-commit marker per writer cycle, and this module admits a price row
only once that marker exists at or before `as_of` — so an INSERT-before /
COMMIT-after row is no longer admitted. The bound is applied through
`VISIBLE_AS_OF_SQL`, the helper exported by the substrate module, so "knowable"
has ONE implementation shared with the source-quality ledger rather than a copy
here. Rows predating the marker era are governed by a documented epoch rule and
are not stranded. See `_load_observations`.

**Unresolved rows are funnel facts, not behaviour facts.** When resolution
fails, the listener writes a `(unresolved)` placeholder into `token_id` and
`symbol`. Those rows all share one canonical identity, so left in they form a
single enormous cluster per channel-day and `duplicate_rate` silently stops
measuring "does this caller re-post the same token" and starts measuring "how
often did OUR resolver fail" — a property of our pipeline attributed to the
caller. They are therefore excluded from every cluster-shaped metric:
duplicate rate and rank, distinct and eligible clusters (so every min-N input),
cluster-representative selection, and cross-channel corroboration.

They are NOT deleted. `history_raw_calls` remains the total funnel, and
`history_unresolved_identity_calls` / `history_unresolved_identity_rate` report
the excluded volume explicitly, so resolution quality stays legible as its own
axis instead of contaminating the caller's track record. `duplicate_rate` is
computed over `history_identified_calls`.

**Self-exclusion.** Shadow evaluation runs after the signal is persisted, so a
naive as-of query would include the signal being scored. `history_*` features
therefore exclude `current_signal_id` and its message row — a call may neither
improve nor degrade its own caller's reputation. `event_*` features describe
THIS call (its duplicate rank, its cross-channel corroboration) and do see it.

**Why the constants live here and not in Settings.** The window table, the
at-call tolerance, the excursion window, the recency split, and the CG-coin-id
predicate replica are module constants rather than `.env` values, which departs
from the repo's no-hardcoded-thresholds rule. That rule targets OPERATIONAL
tunables; these are versioned evidence semantics. The evaluator never reads
them, so they are not in `shadow._FINGERPRINTED_SETTING_NAMES`, and a `.env`
edit would therefore change what a feature MEANS without moving `gate_version`
— silently re-defining a cohort mid-flight. As module constants they are bound
by this file's source hash, so changing one correctly splits the cohort. Drift
tests assert each still matches its upstream definition, so the local copies
cannot fork unnoticed either.

**Read-only by construction.** Features are computed over a separate
`mode=ro` SQLite connection, so this module cannot write, cannot migrate, and
needs no share of the pipeline's `_txn_lock`. `mode=ro` makes that structural
rather than careful. The connection is synchronous because the Stage A
provider contract is synchronous; the queries are small and channel-scoped.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

import structlog

from scout.source_quality.snapshot_writer import VISIBLE_AS_OF_SQL

log = structlog.get_logger()

# Bumped when what a feature MEANS changes. Declared alongside the module hash
# so an intentional semantic change is legible in review rather than only
# visible as a digest that moved.
CALLER_FEATURE_SEMANTIC_VERSION = "tg-caller-v1"

# Forward-return windows, as `(start, end, max_price_age_sec)`.
#
# MIRRORS `scout.source_quality.ledger.WINDOWS` and is deliberately a local
# copy rather than an import: the gate fingerprint binds THIS module's source
# hash, so a window convention reached through an unbound import could change
# what a decision means without splitting the cohort. A drift test asserts the
# two tables stay equal, so the copy cannot silently fork either.
FORWARD_WINDOWS: dict[str, tuple[timedelta, timedelta, int]] = {
    "30m": (timedelta(minutes=30), timedelta(minutes=45), 15 * 60),
    "1h": (timedelta(hours=1), timedelta(minutes=90), 30 * 60),
    "6h": (timedelta(hours=6), timedelta(hours=7), 60 * 60),
    "24h": (timedelta(hours=24), timedelta(hours=28), 60 * 60),
}

# The horizon the coverage denominators are defined on. The ledger's
# `coverage_rate` counts a cluster as covered when its `forward_30m_pct`
# exists, so mirroring the axis means mirroring that choice.
COVERAGE_HORIZON = "30m"

# Forward-only series have no snapshot at/before a call that arrived between
# writer cycles; the earliest observation within this window after the call is
# accepted as the entry anchor. Mirrors the ledger's tolerance.
PRICE_AT_CALL_TOLERANCE_SEC = 900

# Excursion window for max favorable/adverse. It closes at exactly +24h: the
# writer is forward-only, so once `as_of` reaches that instant no future row
# can land inside the window and the extrema are final. (The +28h in
# FORWARD_WINDOWS is slack for FINDING an observation near the 24h mark, which
# is a different question from when the excursion window itself closes.)
EXCURSION_WINDOW = timedelta(hours=24)
# DELIBERATE DIVERGENCE, do not "fix" to match the ledger: the ledger's extrema
# branch (ledger.py:337) has NO anchor-age gate at all. This module adds one
# because an excursion is a percentage against the entry anchor, so a stale
# anchor describes a move the caller never had. Bound by this module's hash and
# covered by `test_excursion_requires_a_fresh_anchor`.
EXCURSION_MAX_PRICE_AGE_SEC = 60 * 60

# Recency split: "lately" versus "ever", so a caller who was good a year ago
# and bad since is not averaged into looking mediocre.
TRAILING_RECENCY_WINDOW = timedelta(days=30)

# Chunk size for `identity_key IN (...)` lookups, under SQLite's variable cap.
_IN_CLAUSE_CHUNK = 400

# The placeholder `listener.py` writes into BOTH `token_id` and `symbol` when
# resolution fails. Such a row records that a message arrived and could not be
# resolved — it does not identify a token.
#
# Left un-excluded, every one of these collapses to the SAME canonical identity,
# so a channel's failures form ONE giant cluster per UTC day: duplicate_rate
# stops measuring "does this caller re-post the same token" and starts measuring
# "how often did OUR resolver fail". Prod at ruling time: 866 sentinel rows, all
# with NULL contract_address and NONE in `RESOLVED` state.
#
# Declared here rather than imported from `listener.py` for the same reason as
# `FORWARD_WINDOWS` and the CG-id replica: the gate fingerprint binds this
# module, and an upstream change to the placeholder must not silently
# re-admit 866 rows into caller behaviour under an unchanged gate_version.
# `test_sentinel_literal_still_matches_the_listener` is the drift alarm.
UNRESOLVED_IDENTITY_SENTINEL = "(unresolved)"

# Ordered `(field_name, type_name)` pairs. Part of the gate fingerprint, so
# reordering or renaming a field starts a new generation — which is correct:
# the evaluator reads these by name and the persisted `features_json` is the
# evidence record.
FEATURE_SCHEMA: tuple[tuple[str, str], ...] = (
    # --- caller history: volume and duplication -------------------------
    # `history_raw_calls` is the TOTAL funnel and still counts unresolved
    # rows; every cluster-shaped metric below is computed over IDENTIFIED
    # calls only, so resolver failure cannot read as caller behaviour.
    ("history_raw_calls", "int"),
    ("history_identified_calls", "int"),
    ("history_unresolved_identity_calls", "int"),
    ("history_unresolved_identity_rate", "float"),
    ("history_distinct_clusters", "int"),
    ("history_duplicate_rate", "float"),
    # --- caller history: coverage (30m-window denominators) --------------
    ("history_eligible_distinct_clusters", "int"),
    ("history_mature_clusters", "int"),
    ("history_mature_priceable_clusters", "int"),
    ("history_coverage_rate", "float"),
    ("history_priceable_coverage_rate", "float"),
    # --- caller history: structural priceability -------------------------
    ("history_priceable_clusters", "int"),
    ("history_unpriceable_clusters", "int"),
    ("history_unpriceable_cluster_rate", "float"),
    ("history_coin_id_identity_clusters", "int"),
    ("history_clusters_without_observations", "int"),
    # --- caller history: forward-return distribution ---------------------
    ("history_avg_forward_30m_pct", "float|null"),
    ("history_avg_forward_1h_pct", "float|null"),
    ("history_avg_forward_6h_pct", "float|null"),
    ("history_avg_forward_24h_pct", "float|null"),
    ("history_forward_30m_sample_count", "int"),
    ("history_forward_1h_sample_count", "int"),
    ("history_forward_6h_sample_count", "int"),
    ("history_forward_24h_sample_count", "int"),
    ("history_avg_max_favorable_pct_24h", "float|null"),
    ("history_avg_max_adverse_pct_24h", "float|null"),
    ("history_excursion_sample_count", "int"),
    # --- caller history: trailing-30d recency split ----------------------
    ("history_trailing_30d_eligible_distinct_clusters", "int"),
    ("history_trailing_30d_priceable_coverage_rate", "float"),
    ("history_trailing_30d_avg_forward_30m_pct", "float|null"),
    # --- current event ---------------------------------------------------
    ("event_duplicate_rank_in_cluster", "int"),
    ("event_cluster_size", "int"),
    ("event_corroborating_channels", "int"),
    ("event_identity_kind", "str"),
)


def _is_cg_coin_id(token_id: str | None) -> bool:
    """Whether a token_id is a real CoinGecko coin id, not a contract shape.

    A deliberate REPLICA of `scout.token_ids.is_cg_coin_id`, kept here for the
    same reason as `FORWARD_WINDOWS`: this classification decides which
    identity a call is priced by, so it is decision semantics, and the gate
    fingerprint binds only THIS module's source. Reached through the import,
    an upstream refinement of the heuristic would silently reclassify
    identities under an UNCHANGED gate_version — splitting a cohort's meaning
    without splitting its version, which is the one failure this program
    cannot tolerate.

    Folding `token_ids.py`'s hash into `module_source_hash()` was rejected: it
    would split evidence cohorts on ANY textual churn in that file — a comment,
    an unrelated helper — which violates the decision-semantics-only rule the
    fingerprint is built on. Binding nothing was rejected too: it leaves
    identity classification mutable under a fixed gate_version.

    Replica + drift test is the unique shape that splits cohorts ONLY on
    BEHAVIORAL divergence, with a mandatory human step in between: the drift
    test fails, someone consciously updates this replica, that edit moves this
    module's hash, and a new generation begins. The convention breach is
    therefore deliberate — `token_ids.py` declares itself the single source of
    truth, and a cross-reference comment at that declaration points here so the
    next editor of the shared helper meets the coupling at the point of edit
    rather than in a CI failure they cannot interpret.

    `test_cg_coin_id_replica_matches_the_shared_predicate` asserts this stays
    equal to the shared helper across representative shapes, so an upstream
    edit fails LOUDLY here rather than drifting. That test failing is not a
    bug: it means the shared heuristic was refined and someone must
    consciously re-ratify this replica and accept the resulting gate_version
    split.
    """
    if not token_id:
        return False
    if token_id.startswith("0x"):
        return False
    if len(token_id) > 60:
        return False
    # Mixed case is a strong signal of base58 (Solana mints); CG ids are
    # lowercase.
    if token_id != token_id.lower():
        return False
    return all(c.isalnum() or c in "-_" for c in token_id)


def hash_module_source(path: Path) -> str:
    """SHA-256 of a module's source with line endings normalized to ``\\n``.

    A CRLF working copy and an LF production checkout are the SAME commit, so
    they must hash identically — otherwise a locally recomputed
    ``gate_version`` can never match production's and replay verification is
    impossible. Separated from `module_source_hash` so the line-ending
    invariance is directly testable; `__file__` cannot be varied in a test.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@lru_cache(maxsize=1)
def module_source_hash() -> str:
    """SHA-256 of this module's source, for the gate fingerprint.

    Read from the ``.py`` file rather than derived from imported objects: the
    point is to notice an edit that changes extraction semantics without
    touching any declared version or threshold.

    Line endings are normalized to ``\\n`` first. A CRLF working copy and an LF
    production checkout are the SAME commit, but hashing raw bytes gives them
    different digests — so a locally recomputed ``gate_version`` could never
    match production's, defeating the replay verification the fingerprint
    exists to enable. Normalizing is free while no generation exists; after
    activation it would retire every accumulated cohort.
    """
    return hash_module_source(Path(__file__))


# ---------------------------------------------------------------------------
# Time and identity
# ---------------------------------------------------------------------------


def parse_utc(value: Any) -> datetime | None:
    """Parse a persisted timestamp to aware UTC, or None if unusable.

    Local rather than imported for the same reason as `FORWARD_WINDOWS`, and
    mirroring `shadow._parse_iso_utc`: a timestamp parser that silently changed
    its treatment of naive values would move every knowability bound in this
    module without moving the fingerprint.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_identity(
    *, chain: str | None, contract_address: str | None, token_id: str | None
) -> str:
    """Channel-independent identity for clustering and corroboration.

    `{chain}:{contract}` normalized when a contract exists, else the
    `token_id`. The ledger's `duplicate_cluster_key` embeds the channel by
    construction, so the same token called by two channels gets two different
    keys there — correct for within-channel duplicate rate, structurally
    unusable for corroboration. This key deliberately carries no channel; the
    caller adds one where within-channel semantics are wanted.
    """
    contract = (contract_address or "").strip()
    if contract:
        return f"{(chain or '').strip().lower()}:{contract.lower()}"
    return (token_id or "").strip()


def snapshot_identity(
    *, chain: str | None, contract_address: str | None, token_id: str | None
) -> tuple[str, str] | None:
    """`(identity_kind, identity_key)` addressing `source_call_price_snapshots`.

    The KEY format mirrors `ledger._priceable_identity` exactly — the snapshot
    writer stores keys produced by that function, so any divergence here would
    look up nothing and read as a caller with no track record, which is a wrong
    decision shaped exactly like a right one. Note the chain segment is NOT
    lowercased, matching the writer.

    The KIND is deliberately NOT the ledger's. `_priceable_identity` returns
    `'token_id'` as a ROUTING value meaning "price this from the gainers/losers
    board snapshots"; `source_call_price_snapshots` has its own namespace,
    constrained by CHECK to `('coin_id', 'contract')`. Stage B reads only the
    latter table — the board snapshots are not part of §B's verified
    forward-only input set — so a CG coin id maps to `'coin_id'` here. A test
    pins both halves: key equality with the ledger, and kind membership in the
    table's actual CHECK domain.

    SEAM + CROSS-PR CONTRACT (parallel substrate PR — CG `coin_id` extension
    of `source_call_price_snapshots`). The contract, stated explicitly because
    getting it wrong fails SILENTLY:

    * The substrate PR writes CG-identity observations with
      `identity_kind = 'coin_id'`. That is the STORAGE kind, and the only CG
      kind the table's CHECK permits.
    * `'token_id'` is the ledger's IN-MEMORY routing kind. It never appears in
      this table's storage. A read filtering `identity_kind='token_id'` returns
      ZERO ROWS — no error, no constraint violation, indistinguishable from
      "the substrate has not written anything yet".
    * Therefore every substrate read for a CG identity here filters
      `'coin_id'`, and contract identities keep filtering `'contract'` as
      today.

    The literal is written out rather than imported because the substrate PR is
    not merged; once it is, both sides should dedup onto one shared constant.

    Until then the `coin_id` branch addresses rows that do not exist yet, so
    CG-slug-identity calls resolve to zero observations. They are still counted
    as structurally priceable (the identity SHAPE is priceable) and surface in
    `history_clusters_without_observations`, which is where the substrate gap
    is legible rather than hidden inside a coverage rate. When that PR lands,
    coverage rises with no change to this module and therefore no change to the
    gate_version.
    """
    coin_id = (token_id or "").strip()
    if coin_id and _is_cg_coin_id(coin_id):
        return "coin_id", coin_id
    contract = (contract_address or "").strip()
    if contract:
        return "contract", f"{(chain or '').strip()}|{contract.lower()}"
    return None


@dataclass(frozen=True)
class _Call:
    """One knowable TG call, reconstructed from INSERT-only rows."""

    signal_id: int
    message_pk: int
    channel_handle: str
    identity: str
    # Lowercased contract address when the row had one, else None. Carried
    # explicitly rather than re-derived from `identity`, which would have to
    # guess whether a colon came from the `{chain}:{contract}` form or from a
    # `dex:{chain}:{addr}` pseudo-id used as the token_id fallback.
    contract_lower: str | None
    snapshot_kind: str | None
    snapshot_key: str | None
    posted_at: datetime
    # False when the row carries no token identity at all — the resolver's
    # `(unresolved)` placeholder. Such calls are funnel facts, not behaviour
    # facts, and are excluded from every cluster-shaped metric.
    has_genuine_identity: bool

    @property
    def cluster_key(self) -> tuple[str, str]:
        """Within-channel cluster: identity plus a frozen UTC-day bucket.

        The channel is implicit — calls are loaded one channel at a time.
        """
        return (self.identity, self.posted_at.strftime("%Y-%m-%d"))


@dataclass(frozen=True)
class _Observation:
    price: float
    snapshot_at: datetime
    row_id: int


# ---------------------------------------------------------------------------
# Outcome reconstruction
# ---------------------------------------------------------------------------


def _price_at_call(
    observations: Sequence[_Observation], call_ts: datetime
) -> _Observation | None:
    """Entry anchor: latest observation at/before the call, else the earliest
    within the just-after tolerance (forward-only series often have no
    at-or-before row)."""
    at_or_before = [o for o in observations if o.snapshot_at <= call_ts]
    if at_or_before:
        return at_or_before[-1]
    deadline = call_ts + timedelta(seconds=PRICE_AT_CALL_TOLERANCE_SEC)
    just_after = [o for o in observations if call_ts <= o.snapshot_at <= deadline]
    return just_after[0] if just_after else None


def _pct_change(anchor: float, later: float) -> float:
    return ((later - anchor) / anchor) * 100.0


def compute_call_outcome(
    *,
    call_ts: datetime,
    as_of: datetime,
    observations: Sequence[_Observation],
) -> dict[str, float | None]:
    """Forward returns and 24h excursion for one call, as of `as_of`.

    `observations` MUST already be filtered to `snapshot_at <= as_of` and
    sorted; this function adds the maturity half of the bound. A horizon whose
    window has not closed by `as_of` yields None — not a zero, and not a
    best-effort partial, either of which would let a later recomputation return
    a different number for the same decision.
    """
    values: dict[str, float | None] = {h: None for h in FORWARD_WINDOWS}
    values["max_favorable_pct_24h"] = None
    values["max_adverse_pct_24h"] = None

    anchor = _price_at_call(observations, call_ts)
    # A zero or negative anchor price cannot express a percentage change. The
    # column is REAL NOT NULL, so a bad feed writes 0.0 rather than NULL, and
    # dividing by it would raise inside a decision path whose failure mode is
    # an immutable row that never gets written.
    if anchor is None or anchor.price <= 0:
        return values
    anchor_age_sec = int(abs((anchor.snapshot_at - call_ts).total_seconds()))

    for horizon, (start_delta, end_delta, max_age_sec) in FORWARD_WINDOWS.items():
        # Maturity: the window must have been eligible to CLOSE by as_of.
        if call_ts + end_delta > as_of:
            continue
        if anchor_age_sec > max_age_sec:
            continue
        window_start = call_ts + start_delta
        window_end = call_ts + end_delta
        candidates = [
            o for o in observations if window_start <= o.snapshot_at <= window_end
        ]
        if not candidates:
            continue
        values[horizon] = _pct_change(anchor.price, candidates[0].price)

    if call_ts + EXCURSION_WINDOW <= as_of and anchor_age_sec <= (
        EXCURSION_MAX_PRICE_AGE_SEC
    ):
        inside = [
            o
            for o in observations
            if call_ts <= o.snapshot_at <= call_ts + EXCURSION_WINDOW
        ]
        if inside:
            pcts = [_pct_change(anchor.price, o.price) for o in inside]
            values["max_favorable_pct_24h"] = max(pcts)
            values["max_adverse_pct_24h"] = min(pcts)
    return values


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class TgCallerFeatureProvider:
    """The real `CallerFeatureProvider` (Stage A protocol) for production.

    Holds no per-decision state: every call opens its own read-only connection
    and closes it, so a provider instance is safe to register once at startup
    and reuse for the life of the process.
    """

    caller_feature_semantic_version: str = CALLER_FEATURE_SEMANTIC_VERSION

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self.module_source_hash: str = module_source_hash()

    def feature_schema(self) -> list[tuple[str, str]]:
        return [tuple(pair) for pair in FEATURE_SCHEMA]  # type: ignore[misc]

    # -- connection ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """`mode=ro`: this provider cannot write, and cannot create the
        database it is asked to read if the path is wrong.

        `isolation_level = None` hands transaction control to us so `features()`
        can wrap its reads in one explicit `BEGIN DEFERRED`. Under the default,
        sqlite3 opens no transaction for SELECTs and each statement takes its
        own WAL snapshot — so a commit landing between two of our statements
        would be half-seen, producing a `features_json` describing a database
        state that never existed at any instant, which no replay can reproduce.
        """
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        return conn

    # -- public contract -------------------------------------------------

    def features(
        self,
        channel_handle: str,
        decision_as_of: datetime,
        current_signal_id: int,
    ) -> dict[str, Any]:
        as_of = decision_as_of.astimezone(timezone.utc)
        conn = self._connect()
        try:
            # ONE point-in-time read for every statement below. Without it the
            # channel-history SELECT, the observations SELECT, and the
            # corroboration SELECT each take their own WAL snapshot, and a
            # commit landing between them yields internally inconsistent
            # features that describe no single database state.
            conn.execute("BEGIN DEFERRED")
            try:
                return self._features(conn, channel_handle, as_of, current_signal_id)
            finally:
                # Read-only transaction: COMMIT and ROLLBACK are equivalent
                # here, and COMMIT must not be allowed to mask a failure from
                # the block above.
                conn.execute("COMMIT")
        finally:
            conn.close()

    # -- computation -----------------------------------------------------

    def _features(
        self,
        conn: sqlite3.Connection,
        channel_handle: str,
        as_of: datetime,
        current_signal_id: int,
    ) -> dict[str, Any]:
        knowable = self._load_channel_calls(conn, channel_handle, as_of)
        current = next((c for c in knowable if c.signal_id == current_signal_id), None)
        if current is None:
            # The signal is not knowable at its own decision time only if it is
            # absent or its message row is missing. Both are real states worth
            # naming; neither may raise, because the alternative to a degraded
            # event context is no decision row at all.
            log.warning(
                "tg_caller_features_current_signal_not_knowable",
                channel_handle=channel_handle,
                current_signal_id=current_signal_id,
                as_of=as_of.isoformat(),
            )

        # `history` drops the current signal AND its message row: a message may
        # not build its own caller's track record, and a single message can
        # produce several signals.
        #
        # KNOWN-REDUNDANT (recorded rather than pretended-covered): the
        # `signal_id` clause cannot be mutation-pinned, because a signal always
        # shares its own `message_pk`, so the message clause already excludes
        # it in every reachable state. It is kept because it is the design's
        # literal rule and because the message clause is conditional on
        # `current is not None`; the mutation battery reports it as an
        # uncaught-by-construction survivor, not as covered.
        history = [
            c
            for c in knowable
            if c.signal_id != current_signal_id
            and (current is None or c.message_pk != current.message_pk)
        ]

        # Clustering sees IDENTIFIED calls only. `history` stays whole so the
        # funnel and the unresolved diagnostic still count what was excluded —
        # the rows are hidden from behaviour metrics, never deleted.
        identified = [c for c in history if c.has_genuine_identity]

        observations = self._load_observations(conn, identified, as_of=as_of)
        clusters = self._build_clusters(identified)
        outcomes = {
            cluster.signal_id: compute_call_outcome(
                call_ts=cluster.posted_at,
                as_of=as_of,
                observations=observations.get(
                    (cluster.snapshot_kind, cluster.snapshot_key), ()
                ),
            )
            for cluster in clusters.values()
        }

        result: dict[str, Any] = {}
        result.update(
            self._history_features(
                history, identified, clusters, outcomes, observations, as_of
            )
        )
        result.update(
            self._trailing_features(clusters, outcomes, as_of),
        )
        result.update(self._event_features(conn, knowable, current, as_of))
        return result

    # -- loading ---------------------------------------------------------

    def _load_channel_calls(
        self, conn: sqlite3.Connection, channel_handle: str, as_of: datetime
    ) -> list[_Call]:
        """Every call by this channel that was KNOWABLE at `as_of`.

        The double bound is applied in Python rather than SQL: persisted
        timestamps are compared as parsed instants, so a row written with a
        `Z` suffix or without an offset cannot slip past a lexicographic
        string compare that assumes one canonical spelling.
        """
        cur = conn.execute(
            """SELECT s.id AS signal_id, s.message_pk, s.token_id,
                      s.contract_address, s.chain, s.source_channel_handle,
                      s.created_at, m.posted_at, m.parsed_at
                 FROM tg_social_signals s
                 JOIN tg_social_messages m ON m.id = s.message_pk
                WHERE s.source_channel_handle = ?""",
            (channel_handle,),
        )
        calls: list[_Call] = []
        for row in cur.fetchall():
            call = _knowable_call(row, as_of)
            if call is not None:
                calls.append(call)
        # Deterministic order: every downstream rank, sum, and mean depends on
        # it, and float addition is not associative.
        calls.sort(key=lambda c: (c.posted_at, c.signal_id))
        return calls

    def _load_observations(
        self,
        conn: sqlite3.Connection,
        calls: Sequence[_Call],
        *,
        as_of: datetime,
    ) -> dict[tuple[str | None, str | None], list[_Observation]]:
        """Price observations per snapshot identity, under a TRIPLE bound.

        A row is admitted only when `snapshot_at <= as_of` AND
        `created_at <= as_of` AND its batch is COMMIT-VISIBLE at `as_of`.

        An earlier version of this docstring claimed `snapshot_at` alone was
        sufficient "because the writer is verified forward-only". That claim
        was FALSIFIED in review: forward-only monotonicity constrains the ORDER
        of stamps, not when a row becomes VISIBLE. The writer stamps
        `snapshot_at` once at cycle start and commits the entire cycle once at
        the end, so a row can carry a pre-`as_of` stamp and appear only after
        `as_of` — making a live evaluation and its replay disagree, which is
        the one invariant this module exists to hold. `created_at` is stamped
        per row at INSERT and bounds recording far more tightly.

        See the module docstring's Residual paragraph: INSERT-stamped is not
        COMMIT-stamped, so this is a much tighter bound rather than a closed
        one.

        SEAM ADOPTED — commit-visibility marker (was: "do NOT depend on it yet").
        The residual described above is now CLOSED. This read goes through
        `VISIBLE_AS_OF_SQL`, the visibility helper exported by the substrate
        module, so "knowable" has ONE implementation shared with the ledger:
        a row counts only once the post-commit marker for its batch exists at
        or before `as_of`, with a documented epoch rule so rows predating the
        marker era are not stranded.

        The bound is therefore TRIPLE, and each clause does distinct work:
          snapshot_at <= as_of   what had been OBSERVED by then
          created_at  <= as_of   what had been RECORDED by then. Retained, not
                                 replaced: the marker branch implies it (a
                                 marker is published after the insert), but the
                                 EPOCH branch does not — a pre-epoch row can
                                 carry a `created_at` later than an early
                                 `as_of`, and without this clause it would be
                                 admitted.
          VISIBLE_AS_OF_SQL      what had been COMMITTED by then — the bound
                                 INSERT-stamping could never express.

        WHY THE FIRST TWO STAY IN PYTHON rather than moving into the SQL beside
        the helper: `created_at` is written by SQLite `datetime('now')`
        (space-separated) while `as_of` here is a Python `.isoformat()`
        ('T'-separated), and SQLite compares them as TEXT where ' ' (0x20) sorts
        before 'T' (0x54). A SQL-side `created_at <= :as_of` would therefore read
        every same-day row as EARLIER than it is and admit rows it must exclude —
        the same INF-04 mismatch that made the substrate's epoch bound leak in
        review. Compared here as parsed datetimes, both shapes are handled
        correctly, and `parse_utc` returning None also screens unparseable
        stamps. The helper's own comparisons are producer-matched by
        construction (marker vs as_of both isoformat; created_at vs epoch both
        SQLite).

        `as_of` is KEYWORD-ONLY on both sides of the boundary — the Python
        signature and the SQL binding. B-1 was a positional argument that
        silently bound `identity_kind`, leaving a fully-correct gate inert in
        production while every test passed. A keyword-only parameter makes the
        Python half of that class a TypeError at the call site rather than a
        silent mis-bind, which is why the two extra call-site keywords are
        worth their noise.
        """
        # Bound for the helper's marker comparison. isoformat() deliberately:
        # `visible_at` is written by the writer with the same producer, so the
        # two sides of that TEXT comparison match by construction.
        as_of_iso = as_of.isoformat()

        by_kind: dict[str, set[str]] = {}
        for call in calls:
            if call.snapshot_kind and call.snapshot_key:
                by_kind.setdefault(call.snapshot_kind, set()).add(call.snapshot_key)

        found: dict[tuple[str | None, str | None], list[_Observation]] = {}
        for kind, keys in by_kind.items():
            ordered = sorted(keys)
            for start in range(0, len(ordered), _IN_CLAUSE_CHUNK):
                chunk = ordered[start : start + _IN_CLAUSE_CHUNK]
                # NAMED params throughout. `VISIBLE_AS_OF_SQL` binds `:as_of`,
                # and sqlite3 forbids mixing qmark and named styles — but the
                # deeper reason is B-1: that defect was a positional argument
                # silently binding the wrong parameter, and it left the gate
                # inert in production while every test passed. Named binding
                # makes the same class impossible to reintroduce here.
                key_names = [f"k{i}" for i in range(len(chunk))]
                placeholders = ",".join(f":{n}" for n in key_names)
                params: dict[str, object] = {"kind": kind, "as_of": as_of_iso}
                params.update(dict(zip(key_names, chunk)))
                cur = conn.execute(
                    f"""SELECT s.id, s.identity_key, s.price, s.snapshot_at,
                               s.created_at
                          FROM source_call_price_snapshots s
                         WHERE s.identity_kind = :kind
                           AND s.identity_key IN ({placeholders})
                           AND s.price IS NOT NULL
                           AND {VISIBLE_AS_OF_SQL}""",
                    params,
                )
                for row in cur.fetchall():
                    snapshot_at = parse_utc(row["snapshot_at"])
                    created_at = parse_utc(row["created_at"])
                    if snapshot_at is None or snapshot_at > as_of:
                        continue
                    # Two of the three bounds; the third (commit visibility)
                    # is applied in the SQL above via VISIBLE_AS_OF_SQL.
                    # BOUND on price facts, for the same reason message
                    # facts have one. `snapshot_at` is stamped ONCE at cycle
                    # start and the whole cycle commits ONCE at the end, so a
                    # row can carry a timestamp before `as_of` and only become
                    # VISIBLE after it — a live evaluation and its replay then
                    # disagree. Forward-only monotonicity does not imply
                    # committed-by-T. `created_at` is stamped per row at INSERT,
                    # so it bounds recording much more tightly than cycle start.
                    if created_at is None or created_at > as_of:
                        continue
                    found.setdefault((kind, row["identity_key"]), []).append(
                        _Observation(
                            price=float(row["price"]),
                            snapshot_at=snapshot_at,
                            row_id=int(row["id"]),
                        )
                    )
        for series in found.values():
            # `id` breaks ties so "the first observation in the window" is a
            # single well-defined row even when two share a timestamp.
            #
            # KNOWN-UNPINNED (recorded rather than pretended-covered): a test
            # cannot force SQLite to deliver tied rows in an order that differs
            # from rowid, so removing this tiebreak leaves the suite green. It
            # is kept anyway because without it the winner would depend on row
            # DELIVERY order, which is a query-plan property — and the pending
            # substrate PR changes this table's selectivity by adding a second
            # identity_kind. A determinism guarantee must not rest on that.
            series.sort(key=lambda o: (o.snapshot_at, o.row_id))
        return found

    # -- feature groups --------------------------------------------------

    @staticmethod
    def _build_clusters(history: Sequence[_Call]) -> dict[tuple[str, str], _Call]:
        """Cluster key -> the rank-1 call representing it.

        Mirrors the ledger, which computes cluster statistics over rows whose
        `duplicate_rank_in_cluster == 1`: a caller who posts the same token ten
        times has one sample, not ten.
        """
        clusters: dict[tuple[str, str], _Call] = {}
        for call in history:  # already sorted by (posted_at, signal_id)
            clusters.setdefault(call.cluster_key, call)
        return clusters

    def _history_features(
        self,
        history: Sequence[_Call],
        identified: Sequence[_Call],
        clusters: dict[tuple[str, str], _Call],
        outcomes: dict[int, dict[str, float | None]],
        observations: dict[tuple[str | None, str | None], list[_Observation]],
        as_of: datetime,
    ) -> dict[str, Any]:
        raw_calls = len(history)
        identified_calls = len(identified)
        unresolved_calls = raw_calls - identified_calls
        distinct = len(clusters)
        priceable = {
            key: call
            for key, call in clusters.items()
            if call.snapshot_kind is not None
        }

        # Coverage denominators are horizon-bound: a cluster enters them only
        # once its 30m window could have closed. Without this a burst of fresh
        # calls would read as deteriorating coverage when the snapshot writer
        # has simply had no opportunity to measure them yet.
        mature = {
            key: call
            for key, call in clusters.items()
            if _horizon_matured(call.posted_at, COVERAGE_HORIZON, as_of)
        }
        mature_priceable = {k: v for k, v in mature.items() if k in priceable}
        eligible = {
            key
            for key, call in mature.items()
            if outcomes[call.signal_id][COVERAGE_HORIZON] is not None
        }

        ordered_clusters = [clusters[k] for k in sorted(clusters)]
        sample_counts: dict[str, int] = {}
        averages: dict[str, float | None] = {}
        for horizon in FORWARD_WINDOWS:
            values = [
                outcomes[call.signal_id][horizon]
                for call in ordered_clusters
                if outcomes[call.signal_id][horizon] is not None
            ]
            sample_counts[horizon] = len(values)
            averages[horizon] = _mean(values)

        favorable = [
            outcomes[call.signal_id]["max_favorable_pct_24h"]
            for call in ordered_clusters
            if outcomes[call.signal_id]["max_favorable_pct_24h"] is not None
        ]
        adverse = [
            outcomes[call.signal_id]["max_adverse_pct_24h"]
            for call in ordered_clusters
            if outcomes[call.signal_id]["max_adverse_pct_24h"] is not None
        ]

        # The substrate gap made legible, and kept distinct from thin coverage:
        # these clusters have a priceable IDENTITY and no price observation at
        # all, rather than observations too sparse or too stale to price a
        # window. Today the count is dominated by CG-slug identities, which the
        # snapshot writer does not yet emit rows for.
        without_observations = sum(
            1
            for call in ordered_clusters
            if call.snapshot_kind is not None
            and not observations.get((call.snapshot_kind, call.snapshot_key))
        )

        return {
            "history_raw_calls": raw_calls,
            "history_identified_calls": identified_calls,
            "history_unresolved_identity_calls": unresolved_calls,
            "history_unresolved_identity_rate": (
                0.0 if raw_calls == 0 else unresolved_calls / raw_calls
            ),
            "history_distinct_clusters": distinct,
            # Denominator is IDENTIFIED calls, not the raw funnel: with the raw
            # count here, a channel whose resolver failures outnumber its real
            # calls would show a duplicate_rate approaching 1.0 while never
            # having repeated a single token.
            "history_duplicate_rate": (
                0.0 if identified_calls == 0 else 1.0 - (distinct / identified_calls)
            ),
            "history_eligible_distinct_clusters": len(eligible),
            "history_mature_clusters": len(mature),
            "history_mature_priceable_clusters": len(mature_priceable),
            "history_coverage_rate": (
                0.0 if not mature else len(eligible) / len(mature)
            ),
            "history_priceable_coverage_rate": (
                0.0 if not mature_priceable else len(eligible) / len(mature_priceable)
            ),
            "history_priceable_clusters": len(priceable),
            "history_unpriceable_clusters": distinct - len(priceable),
            "history_unpriceable_cluster_rate": (
                0.0 if not distinct else (distinct - len(priceable)) / distinct
            ),
            "history_coin_id_identity_clusters": sum(
                1 for call in clusters.values() if call.snapshot_kind == "coin_id"
            ),
            "history_clusters_without_observations": without_observations,
            "history_avg_forward_30m_pct": averages["30m"],
            "history_avg_forward_1h_pct": averages["1h"],
            "history_avg_forward_6h_pct": averages["6h"],
            "history_avg_forward_24h_pct": averages["24h"],
            "history_forward_30m_sample_count": sample_counts["30m"],
            "history_forward_1h_sample_count": sample_counts["1h"],
            "history_forward_6h_sample_count": sample_counts["6h"],
            "history_forward_24h_sample_count": sample_counts["24h"],
            "history_avg_max_favorable_pct_24h": _mean(favorable),
            "history_avg_max_adverse_pct_24h": _mean(adverse),
            "history_excursion_sample_count": len(favorable),
        }

    def _trailing_features(
        self,
        clusters: dict[tuple[str, str], _Call],
        outcomes: dict[int, dict[str, float | None]],
        as_of: datetime,
    ) -> dict[str, Any]:
        """Trailing-30d counterparts of the three decision-relevant lifetime
        axes, so a stale track record cannot masquerade as a current one."""
        cutoff = as_of - TRAILING_RECENCY_WINDOW
        recent = {
            key: call
            for key, call in clusters.items()
            if call.posted_at >= cutoff
            and _horizon_matured(call.posted_at, COVERAGE_HORIZON, as_of)
        }
        recent_priceable = {
            k: v for k, v in recent.items() if v.snapshot_kind is not None
        }
        eligible = [
            recent[key]
            for key in sorted(recent)
            if outcomes[recent[key].signal_id][COVERAGE_HORIZON] is not None
        ]
        return {
            "history_trailing_30d_eligible_distinct_clusters": len(eligible),
            "history_trailing_30d_priceable_coverage_rate": (
                0.0 if not recent_priceable else len(eligible) / len(recent_priceable)
            ),
            "history_trailing_30d_avg_forward_30m_pct": _mean(
                outcomes[call.signal_id][COVERAGE_HORIZON] for call in eligible
            ),
        }

    def _event_features(
        self,
        conn: sqlite3.Connection,
        knowable: Sequence[_Call],
        current: _Call | None,
        as_of: datetime,
    ) -> dict[str, Any]:
        """Context describing THIS call. Sees the current signal by design —
        these are properties of the event, not of the caller's history."""
        if current is None:
            return {
                "event_duplicate_rank_in_cluster": 1,
                "event_cluster_size": 0,
                "event_corroborating_channels": 0,
                "event_identity_kind": "unknown",
            }
        if not current.has_genuine_identity:
            # A call whose token could not be resolved has no cluster to rank
            # within and nothing another channel could corroborate: there is no
            # identity to agree about. Reported as the SAME shape as an absent
            # signal except for the identity kind, which stays truthful.
            #
            # Unreachable in production today — the shadow only evaluates
            # RESOLVED rows and no sentinel row is RESOLVED — but the provider
            # answers whatever id it is handed, and silently ranking such a call
            # against the sentinel pseudo-cluster is exactly the collapse this
            # ruling removes from history.
            return {
                "event_duplicate_rank_in_cluster": 1,
                "event_cluster_size": 0,
                "event_corroborating_channels": 0,
                "event_identity_kind": "unresolved_identity",
            }
        siblings = [
            c
            for c in knowable
            if c.has_genuine_identity and c.cluster_key == current.cluster_key
        ]
        rank = 1 + sum(
            1
            for c in siblings
            if (c.posted_at, c.signal_id) < (current.posted_at, current.signal_id)
        )
        return {
            "event_duplicate_rank_in_cluster": rank,
            "event_cluster_size": len(siblings),
            "event_corroborating_channels": self._corroborating_channels(
                conn, current, as_of
            ),
            "event_identity_kind": current.snapshot_kind or "unpriceable",
        }

    def _corroborating_channels(
        self, conn: sqlite3.Connection, current: _Call, as_of: datetime
    ) -> int:
        """Distinct channels calling this identity in the same UTC-day bucket.

        SEAM (cross-source identity): a channel that called the same token by
        CG slug while another used a contract address produces two different
        canonical identities and does not corroborate. Joining those requires
        the `/coins/{id}.platforms` hop, which is a resolver concern and not
        reachable from the INSERT-only tables this module is restricted to.
        """
        day = current.posted_at.strftime("%Y-%m-%d")
        if current.contract_lower is not None:
            cur = conn.execute(
                """SELECT s.token_id, s.contract_address, s.chain,
                          s.source_channel_handle, s.created_at,
                          m.posted_at, m.parsed_at, s.id AS signal_id,
                          s.message_pk
                     FROM tg_social_signals s
                     JOIN tg_social_messages m ON m.id = s.message_pk
                    WHERE s.contract_address IS NOT NULL
                      AND lower(s.contract_address) = ?""",
                (current.contract_lower,),
            )
        else:
            cur = conn.execute(
                """SELECT s.token_id, s.contract_address, s.chain,
                          s.source_channel_handle, s.created_at,
                          m.posted_at, m.parsed_at, s.id AS signal_id,
                          s.message_pk
                     FROM tg_social_signals s
                     JOIN tg_social_messages m ON m.id = s.message_pk
                    WHERE s.token_id = ?""",
                (current.identity,),
            )
        channels: set[str] = set()
        for row in cur.fetchall():
            call = _knowable_call(row, as_of)
            if call is None:
                continue
            # No sentinel filter here, deliberately: it would be UNREACHABLE.
            # This function only runs for a current call that HAS a genuine
            # identity, and both query shapes exclude sentinels structurally —
            # the contract branch cannot match rows whose contract_address is
            # NULL, and the token_id branch matches a genuine identity that the
            # `(unresolved)` placeholder never equals. A guard here would be
            # dead code implying a protection it does not provide;
            # `test_sentinels_cannot_match_the_corroboration_query` pins the
            # query property that actually does the work.
            if call.identity == current.identity and call.cluster_key[1] == day:
                channels.add(call.channel_handle)
        return len(channels)


def _knowable_call(row: sqlite3.Row, as_of: datetime) -> _Call | None:
    """Apply the double knowability bound to one joined row."""
    posted_at = parse_utc(row["posted_at"])
    parsed_at = parse_utc(row["parsed_at"])
    created_at = parse_utc(row["created_at"])
    if posted_at is None or parsed_at is None or created_at is None:
        return None
    if posted_at > as_of or parsed_at > as_of or created_at > as_of:
        return None
    identity = canonical_identity(
        chain=row["chain"],
        contract_address=row["contract_address"],
        token_id=row["token_id"],
    )
    if not identity:
        return None
    snap = snapshot_identity(
        chain=row["chain"],
        contract_address=row["contract_address"],
        token_id=row["token_id"],
    )
    contract = (row["contract_address"] or "").strip()
    return _Call(
        signal_id=int(row["signal_id"]),
        message_pk=int(row["message_pk"]),
        channel_handle=str(row["source_channel_handle"]),
        identity=identity,
        contract_lower=contract.lower() or None,
        snapshot_kind=None if snap is None else snap[0],
        snapshot_key=None if snap is None else snap[1],
        posted_at=posted_at,
        has_genuine_identity=_has_genuine_identity(
            contract_address=row["contract_address"], token_id=row["token_id"]
        ),
    )


def _has_genuine_identity(
    *, contract_address: str | None, token_id: str | None
) -> bool:
    """Whether a row identifies a TOKEN, as opposed to recording a failure.

    A contract address is always genuine. Without one, the `token_id` must not
    be the resolver's `(unresolved)` placeholder — that value says "we could
    not tell what this message was about", and treating it as an identity makes
    every failure of ours cluster together into what then reads as one caller
    re-posting the same token all day.
    """
    if (contract_address or "").strip():
        return True
    token = (token_id or "").strip()
    return bool(token) and token != UNRESOLVED_IDENTITY_SENTINEL


def _horizon_matured(call_ts: datetime, horizon: str, as_of: datetime) -> bool:
    """True once the horizon's measurement window was eligible to close."""
    return call_ts + FORWARD_WINDOWS[horizon][1] <= as_of

**New primitives introduced:**

- `paper_trade_entry_snapshots` SQLite table (sidecar to `paper_trades`, FK keyed on `paper_trade_id`).
- `Database._migrate_actionability_entry_snapshot_v1()` migration helper, sentinel `paper_migrations.name='bl_actionability_entry_snapshot_v1'`.
- `scout.trading.entry_snapshot` module with `build_entry_snapshot(...)` and `stamp_entry_snapshot(db, trade_id, ...)` functions.
- `entry_snapshot_version` semantic literal `"v1"`.
- `entry_snapshot_complete` integer (0/1) coverage flag.
- `entry_snapshot_missing_fields` JSON-encoded TEXT field listing names of any optional fields that were unavailable at trade-open.

# Design: BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION

## Guardrail

This is a **measurement substrate**. It is NOT an Entry Quality Score. It does
NOT change classifier rules, trading behavior, suppression, capital allocation,
or live/cron config. The only paper-trade-open behavior change is an additional
INSERT into a sidecar table — and that INSERT is wrapped so that any failure
degrades to a structured warning, never to a failed trade-open.

This PR is design-only. Implementation lands in a separate PR after the design
clears a two-vector reviewer pass (structural/schema + trading/analytics
leakage).

## Review fold log (2026-05-20)

Two reviewer vectors completed against the v1 draft of this design:

- **Vector A — structural/schema (storage, FK precedent, migration pattern, hot-path safety):** Verdict "design ready to implement" with 4 Important + 3 Minor items, no Criticals.
- **Vector B — trading/analytics leakage (no future leakage, no historical mixing, score-creep):** Verdict "fold first" — 2 Critical leakage holes (C1 + C2) plus 3 Important + 2 Minor items.

All Critical + Important findings folded into this revision before any implementation work begins. Each fold is annotated inline ("Vector B C1", "Vector A I3", etc.) so reviewers can grep the diff.

| Finding | Vector | Fold |
|---|---|---|
| C1: `tg_channel_at_entry` query had no `created_at <= opened_at` bound; could pull post-open social rows | B Critical | Query gains `AND created_at <= ?` bound; `opened_at` passed from `paper.py:259` through to the writer; new test 5b asserts pre/post-open rows are disjoint |
| C2: `trail_pct` was re-queried from mutable `signal_params` at stamp time; racy with operator/auto recalibration | B Critical | `trail_pct` + `trail_pct_low_peak` passed as inputs from gate-decision site; no `signal_params` read at stamp time; new test 7b asserts the stamp uses gate-decision value through a mid-flight mutation |
| I1: `INSERT OR IGNORE` masked duplicate-PK error signal | B Important | Switched to plain `INSERT`; test 11 rewritten to assert IntegrityError raises into outer try/except; docstring + design narrative updated |
| I2: `complete=0` pools all partial states; per-column `IS NOT NULL` re-introduces silent-NULL ambiguity | B Important | New "Downstream cohort-query rule" subsection mandates filtering via `entry_snapshot_missing_fields` JSON, not per-column IS NOT NULL |
| I3: `v1` version literal not enforced in tests | B Important | New test 12 locks `ENTRY_SNAPSHOT_VERSION == "v1"` and asserts the stamped value |
| I1: migration helper needs sibling pattern (BEGIN EXCLUSIVE + sentinel pre-check + ROLLBACK + log triplet) | A Important | Full migration helper sketched with the triplet; new test 13 |
| I2: `schema_version` row missing | A Important | `schema_version` write added to migration helper; covered by test 13 |
| I3: `CHECK (entry_snapshot_complete IN (0,1))` constraint missing | A Important | Added to schema; new test 14 |
| I4: `initialize()` registration ordering not pinned | A Important | Migration section pins ordering with a comment-site convention |
| M1: `actionable_at_entry` redundancy with `paper_trades.actionable` | B Minor | Documented as intentional in the field-source table |
| M2: `captured_at` vs `paper_trades.opened_at` semantic | B Minor | Documented in `stamp_entry_snapshot` docstring |
| M1: INSERT OR IGNORE docstring | A Minor | Subsumed by B I1 fold (no longer applicable) |
| M2: `entry_snapshot_missing_fields` should be NOT NULL | A Minor | Added NOT NULL to schema |
| M3: `LEFT JOIN ... USING (paper_trade_id)` invalid (paper_trades column is `id`) | A Minor | Switched to explicit `ON s.paper_trade_id = pt.id` |

No design changes were rejected. All findings folded as written; only minor wording adjustments to fit the surrounding narrative.

## Drift Check (§7a)

### Pre-existing actionability stamping (PR #181)

`paper_trades` already carries `actionable INTEGER`, `actionability_reason TEXT`,
`actionability_version TEXT` columns (`scout/db.py:820-822`). `scout/trading/paper.py:243-271`
INSERTs these three at trade-open. The new entry-snapshot work does NOT replace
or duplicate this stamping — it sits next to it, reading the same values into
the snapshot row.

### Pre-existing snapshot/audit pattern in tree

Existing sidecar patterns relevant for shape inference:

- `signal_params_audit (signal_type, field_name, old_value, new_value, reason,
  applied_by, applied_at)` at `scout/db.py:1739-1753` — append-only audit of
  parameter changes. Different population semantic (operator events) but same
  "metadata-only sidecar" shape.
- `minara_alert_emissions (paper_trade_id REFERENCES paper_trades(id), ...)` at
  `scout/db.py:3490-3500` — direct precedent for a `paper_trade_id`-keyed
  sidecar table with `ON DELETE RESTRICT`. The proposed `paper_trade_entry_snapshots`
  follows this exact FK pattern.
- `tg_social_signals.paper_trade_id INTEGER REFERENCES paper_trades(id) ON DELETE
  RESTRICT` at `scout/db.py:1221` — another precedent for paper_trade_id FK with
  the same restrict semantic.

### Pre-existing field sources at trade-open

Variables in scope at `paper.py:271` (right after the INSERT commits) and the
exact source of each must-have field:

| Field | Source | Notes |
|---|---|---|
| `signal_type` | local var `signal_type` | always present |
| `mcap_usd_at_entry` | `signal_data.get("mcap" \|\| "market_cap" \|\| "market_cap_usd")` | optional; most signal_types include it |
| `mcap_bucket_at_entry` | derived from mcap (same bands as `scout/trading/actionability.py:34-52`) | derived |
| `liquidity_usd_at_entry` | `signal_data.get("liquidity_usd")` | optional; gainers/losers do not currently carry it |
| `token_age_days_at_entry` | derived from `(now - first_seen_at_at_entry)` | derived |
| `first_seen_at_at_entry` | one extra query: `SELECT first_seen_at FROM candidates WHERE LOWER(contract_address)=LOWER(?) AND chain=? LIMIT 1` | optional; misses for cg-coin-id-only tokens |
| `detected_by_combo_at_entry` | local var `signal_combo` (already computed at `paper.py:~150`) | string like `"narrative+pipeline"` |
| `source_confluence_count_at_entry` | derived from `signal_combo` (split + dedup) — matches `scout/trading/actionability.py:_source_confluence_count` | derived |
| `tg_channel_at_entry` | one extra query for `signal_type='tg_social'`: `SELECT source_channel_handle FROM tg_social_signals WHERE token_id=? AND created_at <= ? ORDER BY created_at DESC LIMIT 1` (bound: `now` at `paper.py:259`) | optional; only meaningful for tg_social; temporal bound forbids post-open contamination |
| `actionability_version` | local var `actionability_version` | always present post-PR-#181 |
| `actionability_reason` | local var `actionability_reason` | always present |
| `actionable_at_entry` | local var `actionable_value` (0/1) | mirrors `paper_trades.actionable`; redundancy is intentional so the sidecar is self-contained for cohort joins without needing to JOIN `paper_trades` |
| `tp_pct_at_entry` | local var `tp_pct` | always present |
| `sl_pct_at_entry` | local var `sl_pct` | always present |
| `trail_pct_at_entry` | **passed as input** from the gate-decision site (the value the gate actually used) — NOT re-queried at stamp time | optional; eliminates race with operator/auto-recalibration of `signal_params` between gate and stamp |
| `trail_pct_low_peak_at_entry` | same source as `trail_pct_at_entry` — passed as input | optional |

The only fields requiring extra-query are `first_seen_at_at_entry` (candidates)
and `tg_channel_at_entry` (tg_social_signals, with `created_at <= trade_open_ts`
bound). `trail_pct_at_entry` and `trail_pct_low_peak_at_entry` are passed as
inputs from the gate-decision site (per Vector B C2 fold: reading mutable
`signal_params` at stamp time would race with operator recalibration). All
extra-query reads are optional and against tables that exist on master today.

### Conflict check

`grep -rE "paper_trade_entry_snapshots|stamp_entry_snapshot|entry_snapshot_version"
scout/ dashboard/ tests/` against `origin/master` returns zero hits. Clean
namespace; no prior partial implementation to align with.

## Hermes-first analysis (§7b)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trade entry-time feature snapshotting | None (skill hub returned 0 matches across 691 skills) | Build in-project; gecko-alpha-local SQLite table + Python writer |
| Paper-trade metadata persistence | None | Same |
| Training-data substrate for trading classifiers | None | Same |

`awesome-hermes-agent` ecosystem check: 404 (consistent with prior sessions). No
Hermes-side primitive applies; the work is a gecko-alpha-internal DB + writer
hot-path concern.

## Schema choice: sidecar vs wide columns

The operator's design requirement: explicitly compare and recommend.

### Option A — Sidecar table `paper_trade_entry_snapshots` (RECOMMENDED)

```
CREATE TABLE paper_trade_entry_snapshots (
    paper_trade_id  INTEGER PRIMARY KEY,
    entry_snapshot_version    TEXT NOT NULL,
    entry_snapshot_complete   INTEGER NOT NULL CHECK (entry_snapshot_complete IN (0, 1)),
    entry_snapshot_missing_fields TEXT NOT NULL,  -- JSON array; '[]' when complete (never NULL)
    captured_at               TEXT NOT NULL,
    signal_type               TEXT,
    mcap_usd_at_entry         REAL,
    mcap_bucket_at_entry      TEXT,
    liquidity_usd_at_entry    REAL,
    token_age_days_at_entry   REAL,
    first_seen_at_at_entry    TEXT,
    detected_by_combo_at_entry TEXT,
    source_confluence_count_at_entry INTEGER,
    tg_channel_at_entry       TEXT,
    actionability_version_at_entry TEXT,
    actionability_reason_at_entry  TEXT,
    actionable_at_entry       INTEGER,
    tp_pct_at_entry           REAL,
    sl_pct_at_entry           REAL,
    trail_pct_at_entry        REAL,
    trail_pct_low_peak_at_entry REAL,
    FOREIGN KEY (paper_trade_id) REFERENCES paper_trades(id) ON DELETE RESTRICT
);
CREATE INDEX idx_ptes_version ON paper_trade_entry_snapshots(entry_snapshot_version);
CREATE INDEX idx_ptes_complete ON paper_trade_entry_snapshots(entry_snapshot_complete);
```

Pros:
- `paper_trades` schema unchanged (no ALTER TABLE on the hot table, no
  test/migration churn for existing readers).
- Pre-cutover rows have NO sidecar row; `LEFT JOIN` cleanly yields NULL
  snapshot fields. Complete / partial / pre-cutover states are first-class
  observables, not "all-NULL columns mean which thing?" ambiguity.
- Snapshot schema can evolve independently of `paper_trades`. Adding a
  field is `ALTER TABLE paper_trade_entry_snapshots ADD COLUMN` — never
  touches the trading hot table.
- Matches precedent: `minara_alert_emissions` and `tg_social_signals` are
  both sidecars on `paper_trade_id`.
- Backfill (if ever scoped) writes rows with `entry_snapshot_version='v1-backfill'`
  — distinguishable from `v1`-stamped-at-open without query-time guesswork.

Cons:
- One extra JOIN on dashboard / backtest reads. Cheap: `LEFT JOIN
  paper_trade_entry_snapshots USING (paper_trade_id)`. Indexed on PK.
- Hot-path adds a second INSERT (already mitigated by try/except + structured
  log on failure; trade-open never blocks).

### Option B — Wide columns on `paper_trades`

Add ~17 columns to `paper_trades` via ALTER TABLE.

Pros:
- One table to read; no JOIN.
- Existing serialization paths in `scout/db.py` already SELECT * from
  `paper_trades`; new fields automatically appear.

Cons:
- `paper_trades` already has 30+ columns; adding 17 more makes the row
  long and ALTER TABLE risky on the production table (which is the hottest
  write target in the system).
- Pre-cutover rows have NULLs in the new columns AND post-cutover-missing
  rows ALSO have NULLs. Disambiguating "field was missing" from "field
  predates the cutover" requires consulting `entry_snapshot_version`
  presence + per-field semantics. Coverage contract becomes harder to
  state cleanly.
- Schema evolution risk: future fields ride on the trading hot table.

### Recommendation: Option A (sidecar)

The decisive factor is the coverage contract. Pre-cutover rows MUST be
distinguishable from post-cutover-missing rows. With Option A this is structural
(absence of the sidecar row = pre-cutover; presence with `complete=0` =
post-cutover partial). With Option B it requires per-field semantics + tooling
discipline. Sidecar wins.

## Writer hot-path

### Insertion point

In `scout/trading/paper.py`, immediately after `await conn.commit()` at line 271
(today's master), insert:

```python
trade_id = cursor.lastrowid
await conn.commit()

# BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION: metadata-only stamp of
# point-in-time entry facts. Wrapped in try/except so a snapshot-write
# failure NEVER fails the paper-trade open — the trade is already
# committed at this point.
try:
    from scout.trading.entry_snapshot import stamp_entry_snapshot

    await stamp_entry_snapshot(
        db,
        trade_id=trade_id,
        opened_at=now,                # trade-open timestamp; bounds tg_channel query (Vector B C1)
        signal_type=signal_type,
        signal_data=signal_data,
        signal_combo=signal_combo,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        trail_pct_at_entry=trail_pct,            # passed in; gate-decision value (Vector B C2)
        trail_pct_low_peak_at_entry=trail_pct_low_peak,  # passed in (Vector B C2)
        actionable_value=actionable_value,
        actionability_reason=actionability_reason,
        actionability_version=actionability_version,
        contract_address=token_id,  # used for first_seen_at lookup when chain != coingecko
        chain=chain,
        settings=settings,
    )
except Exception:
    log.exception(
        "entry_snapshot_stamp_failed",
        trade_id=trade_id,
        token_id=token_id,
        signal_type=signal_type,
    )

log.info(
    "paper_trade_opened",
    ...
)
```

The `try/except` is outermost so ANY exception (DB lock, schema mismatch on
older DBs, unexpected None) degrades to a structured-log warning and continues.
The paper-trade is already in the DB at this point — the trade-open contract
is preserved unconditionally.

### `scout/trading/entry_snapshot.py` shape

Pure module, no Database subclass extension. Two top-level functions:

```python
ENTRY_SNAPSHOT_VERSION = "v1"

REQUIRED_FIELDS = (
    "entry_snapshot_version",
    "entry_snapshot_complete",
    "captured_at",
    "signal_type",
    "tp_pct_at_entry",
    "sl_pct_at_entry",
    "actionability_version_at_entry",
    "actionability_reason_at_entry",
    "actionable_at_entry",
)

OPTIONAL_FIELDS = (
    "mcap_usd_at_entry",
    "mcap_bucket_at_entry",
    "liquidity_usd_at_entry",
    "token_age_days_at_entry",
    "first_seen_at_at_entry",
    "detected_by_combo_at_entry",
    "source_confluence_count_at_entry",
    "tg_channel_at_entry",
    "trail_pct_at_entry",
    "trail_pct_low_peak_at_entry",
)

async def build_entry_snapshot(db, *, opened_at: datetime, **inputs) -> dict:
    """Compute the snapshot payload. Returns a dict with keys matching the
    sidecar columns. Optional fields are set to None when their source is
    unavailable; missing_fields list captures the names of any None
    optional fields so coverage is auditable.

    `opened_at` is the authoritative trade-open timestamp; queries against
    mutating tables (e.g., tg_social_signals) MUST bound `created_at <= opened_at`
    so post-open rows cannot leak into the snapshot (Vector B C1)."""
    ...

async def stamp_entry_snapshot(db, *, trade_id: int, opened_at: datetime, **inputs) -> None:
    """INSERT a row into paper_trade_entry_snapshots.

    Uses plain INSERT (not INSERT OR IGNORE): the only legitimate caller is
    the post-commit hot-path in paper.py, and `paper_trades.id` is AUTOINCREMENT,
    so a duplicate-PK condition is structurally unreachable in the normal
    path. If it ever fires (test fixture replay, future backfill PR forgetting
    `v1-backfill`, retry loop), letting the PK collision raise into the
    outer try/except preserves the error signal (Vector B I1).

    `captured_at` reflects writer-time (slightly after `opened_at`); analytics
    should use `opened_at` from paper_trades.opened_at when bucketing by trade
    time (Vector B M2)."""
    snapshot = await build_entry_snapshot(db, opened_at=opened_at, **inputs)
    snapshot["paper_trade_id"] = trade_id
    snapshot["entry_snapshot_version"] = ENTRY_SNAPSHOT_VERSION  # always "v1" from live writer
    snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
    missing = [k for k in OPTIONAL_FIELDS if snapshot.get(k) is None]
    snapshot["entry_snapshot_missing_fields"] = json.dumps(missing)
    snapshot["entry_snapshot_complete"] = 1 if not missing else 0
    await db._conn.execute(
        "INSERT INTO paper_trade_entry_snapshots (...) VALUES (...)",
        tuple(snapshot[col] for col in COLUMN_ORDER),
    )
    await db._conn.commit()
```

### No future-state leakage

The writer reads ONLY fields that reflect state at trade-open time:

- **Variables already in scope** (`signal_type`, `tp_pct`, `sl_pct`,
  `trail_pct`, `trail_pct_low_peak`, etc.) — passed in directly from the
  gate-decision site. `trail_pct` and `trail_pct_low_peak` are passed
  as inputs precisely because `signal_params` is mutable (operator/auto
  recalibration); re-reading at stamp time would race (Vector B C2 fix).
- **`signal_data`** (already serialized before INSERT).
- **`candidates`** — append-only metadata about token discovery; rows are
  stable post-insert. Safe to read.
- **`tg_social_signals`** — rows grow monotonically during a trade's
  lifetime. The query MUST bound `created_at <= opened_at` to forbid any
  post-open social signal from being picked as the "at-entry" channel
  (Vector B C1 fix). The trade-open timestamp `now` from `paper.py:259`
  is passed as the `opened_at` parameter.

No reads against `paper_trades` for the trade we just inserted (would be a
self-reference; not needed). No reads against price_cache (would leak
post-open prices) — `price_freshness_seconds_at_entry` is explicitly deferred.
No reads against any post-open table (gainers_snapshots/momentum/etc) — those
are signal sources, not entry-time receipt records.

No reads against `signal_params` directly — values come in as gate-decision
inputs so the recorded `trail_pct_at_entry` reflects what the gate actually
used, not whatever value `signal_params` carries at stamp-time microseconds
later (Vector B C2).

### No historical/reconstructed mixing

`entry_snapshot_version` is the version literal at the writer site. Backfill
work — if ever scoped — would use `v1-backfill` (or `v2`, etc.), distinguishable
in any query. The dashboard and any analytics path filters by version.

## Coverage contract

Three observable states for any `paper_trades.id`:

| State | Indicator | Meaning |
|---|---|---|
| **Pre-cutover** | LEFT JOIN yields NULL row | Trade opened before this PR landed; no snapshot exists. Analytics must explicitly include this state OR explicitly exclude — never silently mix. |
| **Complete** | `entry_snapshot_complete = 1` AND `entry_snapshot_missing_fields = '[]'` | All optional fields were resolvable at trade-open. Safe to use for feature-cohort analysis. |
| **Partial** | `entry_snapshot_complete = 0` | At least one optional field unavailable. Analytics should consult `entry_snapshot_missing_fields` and either filter or include with explicit caveat. |

Required fields (the ones in `REQUIRED_FIELDS` above) MUST be non-NULL on every
snapshot row. If any required field is unresolvable, the writer raises and
falls through to the outer try/except, logging
`entry_snapshot_stamp_failed`. No row is written in that case — the trade
remains pre-cutover-shaped (no sidecar row).

This means: presence of a sidecar row implies all required fields are present.
Coverage is a single column (`complete`) on optional fields, not a complex
per-row schema check.

### Downstream cohort-query rule (Vector B I2)

`entry_snapshot_complete = 0` pools ALL partial states — a query that wants
"has mcap regardless of other missing fields" must NOT mix `IS NOT NULL` on
per-column fields with the `complete` flag, because that re-introduces the
silent-NULL ambiguity this design exists to prevent.

The rule: downstream cohort queries MUST filter sub-cohorts by reading
`entry_snapshot_missing_fields` JSON, not by per-column `IS NOT NULL`. Example:

```sql
-- CORRECT: filter by what was missing
SELECT * FROM paper_trade_entry_snapshots
WHERE entry_snapshot_version = 'v1'
  AND entry_snapshot_missing_fields NOT LIKE '%liquidity_usd_at_entry%';

-- INCORRECT: silently includes pre-cutover/partial via NULL match
SELECT * FROM paper_trades pt
LEFT JOIN paper_trade_entry_snapshots s ON s.paper_trade_id = pt.id
WHERE s.liquidity_usd_at_entry IS NOT NULL;
```

## Dashboard and backtest read semantics

Two distinct surfaces.

### Dashboard (`/api/trading/positions`)

`dashboard/db.py:get_open_positions` adds:

```sql
LEFT JOIN paper_trade_entry_snapshots s ON s.paper_trade_id = pt.id
```

`USING (paper_trade_id)` cannot be used: `paper_trades` exposes the column as
`id`, not `paper_trade_id`. The explicit `ON` clause is correct (Vector A M3).

Returned per-row, new fields:
- `entry_snapshot_version` (string or null)
- `entry_snapshot_complete` (0/1 or null)
- `entry_snapshot_missing_fields` (parsed JSON array or null)
- per-field `*_at_entry` values

`null` on `entry_snapshot_version` means pre-cutover. `complete=0` means
post-cutover-partial. The trade detail drawer (PR #195) already has a
"Source / confluence" group — extend it to surface the entry snapshot's
mcap / liquidity / token-age / detected-by / confluence fields when present,
and a clear "pre-cutover (no snapshot)" subtitle when absent.

NOT in this design's scope: a separate `/api/trading/entry_snapshots` endpoint
or a separate dashboard panel. The minimum-viable read is "show in the drawer
that already exists."

### Backtest / analytics

The `scripts/audit_*` family and the actionability runbook query
`paper_trades` directly. Future cohort-EV analysis (per the brainstorm) will
need to slice by entry-snapshot features. Recommended pattern for those
scripts (NOT to be implemented in this PR):

```sql
SELECT pt.*, s.*
FROM paper_trades pt
LEFT JOIN paper_trade_entry_snapshots s ON s.paper_trade_id = pt.id
WHERE s.entry_snapshot_complete = 1  -- exclude pre-cutover + partial
  AND s.entry_snapshot_version = 'v1';
```

Documented in this design; consumer scripts pick it up when they want feature
cohorts.

## Migration / backfill stance

### Migration

Standard `Database._migrate_*` pattern in `scout/db.py`, matching the sibling
helpers (`_migrate_minara_alert_emissions_v1` at `scout/db.py:3469`,
`_migrate_narrative_scanner_v1` at `scout/db.py:3379`). Vector A I1-I4 folded:

**Sentinel:** `paper_migrations.name = 'bl_actionability_entry_snapshot_v1'`.

**Schema version row:** alongside the sentinel insert, write
`schema_version (key='bl_actionability_entry_snapshot_v1', version=20260520)`
matching the sibling-migration convention (Vector A I2). The integer is the
ISO date the migration first ships; bumps require a new sentinel.

**Transactional shape (Vector A I1):**

```python
async def _migrate_actionability_entry_snapshot_v1(self) -> None:
    # 1. Sentinel pre-check
    cur = await self._conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name = 'bl_actionability_entry_snapshot_v1'"
    )
    if await cur.fetchone():
        log.info("bl_actionability_entry_snapshot_v1_migration_skip_already_applied")
        return

    try:
        await self._conn.execute("BEGIN EXCLUSIVE")
        await self._conn.execute("CREATE TABLE IF NOT EXISTS paper_trade_entry_snapshots (...)")
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ptes_version ...")
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ptes_complete ...")
        await self._conn.execute(
            "INSERT INTO paper_migrations (name, applied_at) VALUES (?, ?)",
            ("bl_actionability_entry_snapshot_v1", datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.execute(
            "INSERT OR REPLACE INTO schema_version (key, version) VALUES (?, ?)",
            ("bl_actionability_entry_snapshot_v1", 20260520),
        )
        await self._conn.commit()
        log.info("bl_actionability_entry_snapshot_v1_migration_complete")
    except Exception:
        await self._conn.execute("ROLLBACK")
        log.exception("bl_actionability_entry_snapshot_v1_migration_rollback")
        raise
```

The log triplet (`_skip_already_applied` / `_complete` / `_rollback`) matches
the sibling-migration convention so operator runbooks can grep uniformly.

**initialize() ordering (Vector A I4):** register
`_migrate_actionability_entry_snapshot_v1` AFTER any migration that touches
`paper_trades` itself, so the FK target table is guaranteed to exist /
already-migrated at sidecar-create time. Concretely: after the existing
paper_trades migrations and after `_migrate_minara_alert_emissions_v1`
(another paper_trade_id FK precedent). Document this pin with a comment at
the registration site.

The migration runs on `Database.initialize()` like all sibling migrations.
No coordination with the pipeline daemon required — the writer hot-path
only reads the table when stamping a new trade, and the sidecar's absence
on older DBs is caught by the outer try/except (degrades to
`entry_snapshot_stamp_failed` log).

### Backfill

**Out of scope for this design.** The dirty-data risk of reconstructing entry
context from current state is exactly the problem the foundation is designed
to avoid. If a future PR wants to backfill, it must:

1. Use a distinct `entry_snapshot_version = 'v1-backfill'`.
2. Document per-field reconstruction sources and their leakage risks.
3. Treat backfilled rows as a DIFFERENT cohort from `v1`-stamped rows in any
   downstream analysis.

This design ships no backfill code. New paper trades only.

## Tests (TDD)

For the implementation PR (not this design PR). Pre-registered acceptance
criteria here so the reviewer can match the impl against them.

1. **Schema migration is idempotent.** Running `Database.initialize()` twice
   does not duplicate the table; second run logs
   `bl_actionability_entry_snapshot_v1_migration_skip_already_applied`.
2. **Fully-complete snapshot.** Open a paper trade with full `signal_data`
   (mcap, liquidity, etc.) and a contract_address that resolves in
   `candidates`. Assert a row exists in `paper_trade_entry_snapshots` with
   `entry_snapshot_complete=1`, `entry_snapshot_missing_fields='[]'`, and
   all optional fields populated.
3. **Partial snapshot — missing liquidity.** Open with `signal_data` that
   omits `liquidity_usd`. Assert `entry_snapshot_complete=0` and
   `liquidity_usd_at_entry` in `entry_snapshot_missing_fields`. Other
   optional fields still populated.
4. **Partial snapshot — missing first_seen.** Open with a contract_address
   that does NOT appear in `candidates`. Assert
   `first_seen_at_at_entry` and `token_age_days_at_entry` in
   `entry_snapshot_missing_fields`.
5. **tg_social channel population.** Insert a `tg_social_signals` row with
   the same `token_id` shortly before opening a `signal_type='tg_social'`
   paper trade. Assert `tg_channel_at_entry` equals the channel handle.
5b. **tg_social channel — no post-open leakage (Vector B C1).** Insert a
    `tg_social_signals` row with `created_at = opened_at - 60s` and a
    DIFFERENT row with `created_at = opened_at + 60s`. Open a tg_social
    paper trade at `opened_at`. Assert `tg_channel_at_entry` equals the
    PRE-open row's channel handle and NEVER the post-open row's. (This is
    the structural guarantee that `tg_channel_at_entry` cannot be polluted
    by later social activity on the same token.)
6. **Actionability fields copied correctly.** Open a trade for which
   actionability classifier returns `(actionable=1,
   reason='v1_pass_core_signal_mcap_10_50m', version='v1')`. Assert the
   sidecar row's three `actionability_*_at_entry` fields match exactly.
7. **Exit params copied correctly.** Open a trade with `tp_pct=20.0`,
   `sl_pct=10.0`, `trail_pct=30.0`, `trail_pct_low_peak=20.0`. Assert
   `tp_pct_at_entry=20.0`, `sl_pct_at_entry=10.0`, `trail_pct_at_entry=30.0`,
   `trail_pct_low_peak_at_entry=20.0` — i.e., the values passed to
   `stamp_entry_snapshot` from the gate-decision site, NOT whatever
   `signal_params` carries at stamp time.
7b. **trail_pct passed-as-input, not re-queried (Vector B C2).** With
    `SIGNAL_PARAMS_ENABLED=True` and a `signal_params` row holding
    `trail_pct=99.0`, open a trade where the gate decided
    `trail_pct=30.0`. Between gate-decision and stamp, mutate the
    `signal_params` row to `trail_pct=88.0`. Assert
    `trail_pct_at_entry=30.0` (the gate-decision value), NOT 99 or 88.
8. **Pre-cutover rows distinguishable.** Insert a paper_trade directly via
   SQL with no sidecar row. Query `SELECT entry_snapshot_version FROM
   paper_trades LEFT JOIN paper_trade_entry_snapshots ...` returns NULL.
   No accidental v1 stamping.
9. **No classifier or trading decision change.** Before/after diff of
   `paper_trades` columns (excluding `id`) for a deterministic trade-open
   fixture matches byte-for-byte. The only DB-level change is the new
   sidecar row.
10. **Snapshot-write failure does NOT fail trade-open.** Simulate DB error
    (e.g., monkey-patch the snapshot INSERT to raise). Assert the
    paper_trade row still exists, no sidecar row is created, and the
    `entry_snapshot_stamp_failed` log is emitted.
11. **Duplicate stamp raises into outer try/except (Vector B I1).** Calling
    `stamp_entry_snapshot` twice on the same `trade_id` (synthetic test
    via direct call, not via the paper-trade hot-path which is
    structurally unreachable for duplicate trade_ids) raises
    `sqlite3.IntegrityError` (PRIMARY KEY violation). The outer try/except
    in `paper.py` catches and logs `entry_snapshot_stamp_failed`; the
    existing sidecar row is untouched. (We intentionally do NOT use
    INSERT OR IGNORE — silencing the duplicate-PK signal would mask
    incorrect callers.)
12. **Version literal enforced (Vector B I3).** Open a trade. Read the
    sidecar row. Assert `entry_snapshot_version == "v1"` exactly. Also
    assert that the writer module-level constant `ENTRY_SNAPSHOT_VERSION`
    equals `"v1"` (locks the literal so any future backfill PR must
    explicitly add a new constant rather than reusing `v1` on
    reconstructed data).
13. **Migration helper idempotency + sentinel + schema_version (Vector A I1/I2).**
    Run `_migrate_actionability_entry_snapshot_v1` twice. Assert: first
    run logs `_migration_complete` + inserts a row in `paper_migrations`
    with `name='bl_actionability_entry_snapshot_v1'` + inserts/replaces
    `schema_version` row with `version=20260520`. Second run logs
    `_migration_skip_already_applied` and does NOT re-write either row.
14. **CHECK constraint on complete (Vector A I3).** Attempt to INSERT a
    sidecar row with `entry_snapshot_complete=2`. Assert sqlite3
    constraint error. (Trivial test guarding against future writer bugs
    that compute the flag wrong.)

## Failure modes pre-empted

| Mode | How addressed |
|---|---|
| Sidecar write fails (DB lock, table missing, schema drift on old DB) | Outer `try/except` in writer; logs `entry_snapshot_stamp_failed`; paper_trade is already committed |
| Required field unresolvable | Writer raises before INSERT; outer try/except catches; no partial row written; pre-cutover-shape preserved |
| Optional field unresolvable | `None` flows through `build_entry_snapshot`; `entry_snapshot_missing_fields` enumerates them; `entry_snapshot_complete=0` |
| Future leakage | Writer reads only at-open variables + read-only queries against pre-existing-state tables |
| Historical/reconstructed mixing | `entry_snapshot_version` literal pins the cohort; any backfill must use a distinct version |
| Schema drift | New column adds go through `Database._migrate_*` pattern with a new sentinel; consumers tolerate NULL on new columns |
| `paper_trades.id` reused (it isn't — AUTOINCREMENT) | PK on `paper_trade_id` + `ON DELETE RESTRICT` prevents orphan / collision |
| Composite score creep | Schema has zero "score" columns; design explicitly forbids one; reviewer gate checks for any added |
| `paper_trades` ALTER TABLE risk on prod | Sidecar avoids; no ALTER on the hot table |

## Implementation plan

Conditional on this design clearing the two-vector reviewer pass.

1. Add migration helper `_migrate_actionability_entry_snapshot_v1` in
   `scout/db.py`, wired in `initialize()`.
2. Create `scout/trading/entry_snapshot.py` with `build_entry_snapshot` and
   `stamp_entry_snapshot`.
3. Add the try/except hook in `scout/trading/paper.py` after the existing
   commit at line 271.
4. Add `tests/test_entry_snapshot.py` covering all 14 acceptance criteria
   (1–11 + 5b + 7b + 12 + 13 + 14, per the Tests section).
5. Extend `dashboard/db.py:get_open_positions` to LEFT JOIN
   `paper_trade_entry_snapshots`.
6. Extend `dashboard/frontend/components/TradeDetailDrawer.jsx` "Source /
   confluence" group with `*_at_entry` rendering + pre-cutover label.
7. Dist rebuild + commit per the `feedback_vite_dist_index_html_commit_discipline.md`
   rule.

If the implementation grows beyond ~600 LOC or any acceptance criterion fails,
stop and report; the operator can elect to split or scope down.

## Out-of-scope (explicit non-goals)

- Entry Quality Score (rejected explicitly).
- Operator feedback marks (separate backlog item, separate PR).
- X handle stamping at entry (depends on PR #184; deferred field).
- Price freshness at entry (depends on price_cache writer instrumentation;
  deferred field).
- Backfill of historical paper_trades.
- Any classifier / suppression / capital-allocation change.
- Cohort-EV reporting (downstream consumer; this PR ships the substrate, not
  the consumer).
- Schema migration of `paper_trades` (sidecar avoids it).

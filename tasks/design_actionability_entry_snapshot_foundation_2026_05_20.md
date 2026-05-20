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
| `tg_channel_at_entry` | one extra query for `signal_type='tg_social'`: `SELECT source_channel_handle FROM tg_social_signals WHERE token_id=? ORDER BY created_at DESC LIMIT 1` | optional; only meaningful for tg_social |
| `actionability_version` | local var `actionability_version` | always present post-PR-#181 |
| `actionability_reason` | local var `actionability_reason` | always present |
| `actionable_at_entry` | local var `actionable_value` (0/1) | mirrors `paper_trades.actionable` |
| `tp_pct_at_entry` | local var `tp_pct` | always present |
| `sl_pct_at_entry` | local var `sl_pct` | always present |
| `trail_pct_at_entry` | one extra query: `SELECT trail_pct FROM signal_params WHERE signal_type=?` when `SIGNAL_PARAMS_ENABLED=True`; else `settings.MOONSHOT_TRAIL_DRAWDOWN_PCT` / `settings.PAPER_TRAIL_DRAWDOWN_PCT` | optional; param-source recorded in coverage |
| `trail_pct_low_peak_at_entry` | same source as `trail_pct_at_entry` | optional |

The only fields requiring extra-query are `first_seen_at_at_entry` (candidates),
`tg_channel_at_entry` (tg_social_signals), and `trail_pct_*_at_entry`
(signal_params). All three are optional and read-only against tables that exist
on master today.

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
    entry_snapshot_complete   INTEGER NOT NULL,
    entry_snapshot_missing_fields TEXT,           -- JSON array; empty array when complete
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
        signal_type=signal_type,
        signal_data=signal_data,
        signal_combo=signal_combo,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
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

async def build_entry_snapshot(db, **inputs) -> dict:
    """Compute the snapshot payload. Returns a dict with keys matching the
    sidecar columns. Optional fields are set to None when their source is
    unavailable; missing_fields list captures the names of any None
    optional fields so coverage is auditable."""
    ...

async def stamp_entry_snapshot(db, *, trade_id: int, **inputs) -> None:
    """INSERT a row into paper_trade_entry_snapshots. Idempotent against
    re-stamping (INSERT OR IGNORE on PRIMARY KEY paper_trade_id)."""
    snapshot = await build_entry_snapshot(db, **inputs)
    snapshot["paper_trade_id"] = trade_id
    snapshot["entry_snapshot_version"] = ENTRY_SNAPSHOT_VERSION
    snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
    missing = [k for k in OPTIONAL_FIELDS if snapshot.get(k) is None]
    snapshot["entry_snapshot_missing_fields"] = json.dumps(missing)
    snapshot["entry_snapshot_complete"] = 1 if not missing else 0
    # INSERT OR IGNORE so a repeat-stamp on the same trade_id is a no-op
    await db._conn.execute(
        "INSERT OR IGNORE INTO paper_trade_entry_snapshots (...) VALUES (...)",
        tuple(snapshot[col] for col in COLUMN_ORDER),
    )
    await db._conn.commit()
```

### No future-state leakage

The writer reads ONLY fields that exist at trade-open time:
- Variables already in scope (`signal_type`, `tp_pct`, `sl_pct`, etc.)
- `signal_data` (already serialized before INSERT)
- Read-only queries against `candidates`, `tg_social_signals`, `signal_params`
  — all of which describe pre-existing state, not future state.

No reads against `paper_trades` for the trade we just inserted (would be a
self-reference; not needed). No reads against price_cache (would leak
post-open prices) — `price_freshness_seconds_at_entry` is explicitly deferred.
No reads against any post-open table (gainers_snapshots/momentum/etc) — those
are signal sources, not entry-time receipt records.

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

## Dashboard and backtest read semantics

Two distinct surfaces.

### Dashboard (`/api/trading/positions`)

`dashboard/db.py:get_open_positions` adds:

```sql
LEFT JOIN paper_trade_entry_snapshots s USING (paper_trade_id)
```

(adapter required since the existing query is on `paper_trades`; the join key
is `paper_trades.id = paper_trade_entry_snapshots.paper_trade_id`)

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

Standard `Database._migrate_*` pattern in `scout/db.py`. Sentinel:
`paper_migrations.name = 'bl_actionability_entry_snapshot_v1'`. Idempotent:
`CREATE TABLE IF NOT EXISTS`. The migration runs on `Database.initialize()`
like all sibling migrations. No coordination with the pipeline daemon
required — the writer hot-path only reads the table when stamping a new
trade, and the sidecar's absence on older DBs is caught by the outer
try/except (degrades to `entry_snapshot_stamp_failed` log).

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
6. **Actionability fields copied correctly.** Open a trade for which
   actionability classifier returns `(actionable=1,
   reason='v1_pass_core_signal_mcap_10_50m', version='v1')`. Assert the
   sidecar row's three `actionability_*_at_entry` fields match exactly.
7. **Exit params copied correctly.** Open a trade with `tp_pct=20.0`,
   `sl_pct=10.0`. Assert `tp_pct_at_entry=20.0`, `sl_pct_at_entry=10.0`.
   When `SIGNAL_PARAMS_ENABLED=True` and the signal_params table has a
   matching row, `trail_pct_at_entry` matches that row's `trail_pct`.
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
11. **INSERT OR IGNORE idempotency.** Calling `stamp_entry_snapshot` twice
    on the same `trade_id` (synthetic test) does not duplicate or modify
    the existing row.

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
4. Add `tests/test_entry_snapshot.py` covering all 11 acceptance criteria.
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

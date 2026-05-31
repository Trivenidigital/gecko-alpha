**New primitives introduced:** [SubsystemStatus closed 4-value status enum (`ok` | `degraded` | `down` | `unknown`); per-subsystem freshness-SLO threshold mapping `HEALTH_FRESHNESS_SLO_MINUTES` — a new module-level `dict` constant in `dashboard/db.py` that ships EMPTY (`{}`) and is operator-fillable later (no per-table SLO exists in tree today, and we deliberately do NOT invent per-table thresholds); pure derivation function `derive_subsystem_status(...)` in a new `dashboard/health_status.py` module; a distinguishable read-error sentinel (`count == -1`) returned by `_table_stats` on a genuine table-read error]

# DESIGN — BL-NEW-API-SYSTEM-HEALTH-STATUS-ENUM

Read-only per-subsystem `status` enum (`ok` | `degraded` | `down` | `unknown`)
added to the existing `/api/system/health` surface, derived deterministically
from signals already persisted (`count` + `latest` per table) plus an
operator-fillable freshness-SLO map that ships EMPTY. No new schema, no
collectors, no alerting, no trading-policy change, no deploy required to be
complete. The dashboard Category-3 consumer that would *render* this status
stays deferred and out of scope.

- **Branch / worktree:** `feat/api-system-health-status-enum` @ `C:\projects\gecko-alpha-wt\health-enum` (off `origin/master` `32bd1f6b`)
- **Date:** 2026-05-30
- **Status:** REFOLDED 2026-05-30 to operator decision **D1** (4-value enum +
  empty operator-fillable SLO map). The earlier "Key flagged decision" section
  (D1 vs D2 vs D3) is RESOLVED in favour of **D1**; it is retained below as a
  decision record, not an open question. Implementation proceeds under D1.

## Operator decision (RESOLVED — overrides the doc's original D2 recommendation)

The operator chose **D1**: a **closed 4-value enum `ok | degraded | down |
unknown`** with an **initially-EMPTY, operator-fillable SLO map**. We do **NOT**
invent per-table freshness thresholds. Concretely:

- `status` ∈ {`ok`, `degraded`, `down`, `unknown`} (closed 4-value enum).
- `down` = the subsystem table is **UNREADABLE** (a genuine read-error / missing
  table). To make `down` reachable, `_table_stats` gains a MINIMAL read-only
  change: on a genuine table-read error it returns the distinguishable sentinel
  `{"count": -1, "latest": None}` (was `{"count": 0, "latest": None}`). The
  deriver maps `count == -1` → `down`. An empty table still returns
  `{"count": 0, "latest": None}` and is NOT `down`.
- `unknown` = table readable (`count >= 0`) but **NO SLO defined** for it in the
  map (the common case today — the map ships empty, so ~all 15 tables are
  `unknown`). Also: `count == 0` with an SLO defined → `unknown` (cannot assess
  freshness with zero rows). Also: unparseable `latest` with an SLO defined →
  `unknown` (cannot compute age honestly).
- `degraded` = SLO defined **and** the table is non-empty **and**
  `now - latest` strictly exceeds the SLO.
- `ok` = SLO defined **and** the table is non-empty **and** fresh (age within
  SLO; boundary `age == SLO` → `ok`).
- The SLO map is `HEALTH_FRESHNESS_SLO_MINUTES: dict[str, int]`, a **module-level
  constant in `dashboard/db.py`** (co-located with the only consumer,
  `get_system_health`, which reads it via `.get(table)`), shipping as `{}`.
  Operator populates `{table: minutes}` later as evidence accrues. **Empty map →
  every readable table = `unknown` today, which is the honest ship-state** (we
  never guess freshness for a table whose SLO we have not yet justified).

Rationale for module-level-constant-in-db.py over a Settings field: the only
reader is `get_system_health` in the same module; a `dict` Settings field would
add Pydantic-parsing surface and an import edge for zero benefit, and the map is
operator-edited in source (reviewable in git) rather than via `.env`. Documented
here per the design-convention.

All `dashboard/db.py` / `dashboard/api.py` / `scout/config.py` line numbers and
code excerpts below were read directly from `origin/master` `32bd1f6b` and
re-verified by a token-presence check at authoring time (hook + fact-check both
green).

---

## Hermes-first analysis

Per global CLAUDE.md §7b. Checked the Hermes skill hub
(`hermes-agent.nousresearch.com/docs/skills`) + awesome-hermes-agent ecosystem
for capabilities covering this work.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Service health status derivation (ok/degraded/down from internal signals) | None found — Hermes skills target agent tool-use / external-API orchestration, not deriving a status enum over a caller's own SQLite freshness columns | Build in-repo. The derivation is a pure function over gecko-alpha's *own* persisted `count`/`latest` values; there is no external service to call. |
| SLO / freshness-threshold evaluation | None found — no SLO-evaluation skill in the hub | Build in-repo. Thresholds mirror the existing `get_source_calls_health` writer-lag logic (`dashboard/db.py:3523`); reusing in-repo semantics, not a generic SLO engine. |
| Health-check endpoint scaffolding | Not applicable — the endpoint already exists (`/api/system/health`, `dashboard/api.py:1056`); this change is additive to existing FastAPI code | Extend existing endpoint; no new framework primitive. |

**awesome-hermes-agent ecosystem check:** scanned for health / SLO / observability
entries — the ecosystem is oriented to agent capability plugins (web, search,
chain RPC, social), none of which derive status from a local SQLite freshness
column. **Verdict:** no Hermes skill applies; this is a gecko-alpha-internal
derivation over its own persisted freshness signals — build in-repo.

---

## Drift check (§7a)

Searched the tree for an existing status-enum / subsystem-status primitive on the
`/api/system/health` surface before scoping:

- `get_system_health` (`dashboard/db.py:636`) currently returns the FLAT
  `{table: {count, latest}}` shape with **no** `status` field — verified verbatim
  in Ground truth §A. No status derivation exists here.
- `get_source_calls_health` (`dashboard/db.py:3523`) DOES compute freshness
  (`writer_freshness.minutes_since_last_observed`, `lag_threshold_minutes`,
  `schema_missing`) but only for the **source_calls** writer surface, exposed at a
  *different* endpoint (`/api/source-calls/health`, `dashboard/api.py:1455`). It
  is the canonical pattern to MIRROR, not a closure — it does not cover the 15
  tables in `get_system_health`.

**Conclusion:** no in-tree closure. The freshness *logic* exists (reuse its
semantics); the per-subsystem *status enum on the system-health surface* does
not. Proceed.

---

## Ground truth (verbatim from origin/master 32bd1f6b)

### §A. `get_system_health` — `dashboard/db.py:636-659`

Returns a flat dict keyed by table name; each value is the `_table_stats`
`{count, latest}` shape. This is the surface we extend. 15 tables, each paired
with its activity timestamp column:

```python
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
        ("briefings", "created_at"),
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
```

### §B. `_table_stats` — `dashboard/db.py:612-633` (THE most load-bearing fact)

```python
async def _table_stats(conn, table: str, time_col: str) -> dict:
    """Return count and latest timestamp for a table; tolerate missing tables.
    ...
    """
    if not _SQL_IDENTIFIER_RE.match(table):
        raise ValueError(f"invalid table identifier: {table!r}")
    if not _SQL_IDENTIFIER_RE.match(time_col):
        raise ValueError(f"invalid column identifier: {time_col!r}")
    try:
        cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
        count = (await cursor.fetchone())[0]
        cursor = await conn.execute(f"SELECT MAX({time_col}) FROM {table}")
        latest = (await cursor.fetchone())[0]
        return {"count": count, "latest": latest}
    except Exception:
        return {"count": 0, "latest": None}
```

**`latest` format / null behavior — VERIFIED from this source:**

- `latest` is the **raw `MAX(time_col)` value from SQLite**, returned **unparsed**.
  For these 15 tables the time columns store ISO-8601 strings (e.g.
  `"2026-05-30T11:42:08"`), so `latest` is an **ISO-8601 string** when rows exist.
  (The project's ISO convention is proven by `get_source_calls_health`, which does
  `datetime.fromisoformat(max_observed.replace("Z","+00:00"))` on an analogous
  `MAX(observed_at)` — §C.)
- `latest` is **`None`** in two distinct cases that look identical to the caller:
  1. the table exists but is **empty** (`MAX()` over no rows → SQL NULL → `None`);
  2. **any exception** — including a **missing table** (`OperationalError: no such
     table`) or any other error — is swallowed by the bare `except Exception`,
     returning `{"count": 0, "latest": None}`.
- **Critical consequence (corrects a naive design assumption):** a genuinely
  **missing/unreadable table is INDISTINGUISHABLE from an empty table** at the
  `_table_stats` boundary — both yield `{"count": 0, "latest": None}`. There is
  **no `schema_missing` signal** flowing out of `_table_stats` today (unlike
  `get_source_calls_health`, which detects `no such table` explicitly). This
  directly shapes the `down` rule and is flagged in Risks §6.

### §C. `get_source_calls_health` — `dashboard/db.py:3523-3737` (pattern to mirror)

The canonical freshness pattern. Verbatim-relevant excerpts:

- Defensive `schema_missing` flag on a missing table:
  ```python
  except aiosqlite.OperationalError as exc:
      msg = str(exc)
      if "no such table" not in msg or "source_calls" not in msg:
          raise
      ...
      return {**base_response, "schema_missing": True}
  ```
- Writer-freshness block — the **only freshness threshold literal on a health
  surface** (`lag_threshold_minutes`), plus the timezone-normalizing parse:
  ```python
  if max_observed:
      try:
          last_dt = datetime.fromisoformat(max_observed.replace("Z", "+00:00"))
          if last_dt.tzinfo is None:
              last_dt = last_dt.replace(tzinfo=timezone.utc)
          age_min = (now - last_dt).total_seconds() / 60.0
          base_response["writer_freshness"] = {
              "max_observed_at": max_observed,
              "minutes_since_last_observed": round(age_min, 1),
              "lag_threshold_minutes": 30,
          }
  ```
  Note: `lag_threshold_minutes` is the literal **`30`**, written **inline** (three
  times in this function), **not** a named module constant and **not** wired to a
  Settings field. The degraded-cutoff logic to mirror is
  `minutes_since_last_observed > lag_threshold_minutes`.

### §D. API wiring — `dashboard/api.py` (line numbers VERIFIED)

```python
# api.py:1056
    @app.get("/api/system/health")
# api.py:1057
    async def system_health():
# api.py:1058
        return await db.get_system_health(_db_path)
```

The endpoint passes the dict straight through, unmodified. The static liveness
stub `health_check()` is the separate `@app.get("/health")` at **api.py:1060-1061**
(it surfaces backup-heartbeat freshness; unrelated and untouched).
`get_source_calls_health` is wired separately at **api.py:1455**
(`return await db.get_source_calls_health(_db_path)`).

---

## Existing freshness / lag constants (pin against these — do NOT invent)

Scanned `dashboard/db.py` and `scout/config.py` for
`lag_threshold` / `_STALE` / `_THRESHOLD_MINUTES` / `STALE` / `freshness` / `SLO`
/ `MAX_AGE`. Findings:

**Real, named freshness-budget constants (reusable):**
- `scout/config.py:442` — `WRITER_THRESHOLD_MINUTES: int = 20` — the writer-lag
  budget Settings field. Its own comment (config.py:440-441): "Default 20min = 4×
  the 5min writer cron cadence." This is the **one named, importable freshness
  constant** and the right thing to point per-table SLOs at where a table tracks a
  periodic writer.
- `scout/config.py:137` — `HELD_POSITION_STALE_WARN_HOURS: int = 24` — held-position
  price staleness budget (domain-specific to open positions, not table-write
  freshness; cited for completeness, not reused here).

**Inline freshness literals (real thresholds, but NOT named/importable):**
- inside `get_source_calls_health` (`dashboard/db.py:3523-3737`) —
  `"lag_threshold_minutes": 30` (writer-lag cutoff for the source_calls surface;
  bare literal, repeated 3×).
- `dashboard/db.py:1761-1762` — `stale_warn_age = timedelta(hours=1)` /
  `stale_hard_age = timedelta(hours=2)` (open-position price-staleness, domain-
  specific).
- `dashboard/db.py:~1117/1427/1429/1620/1622/...` — repeated
  `price_staleness_minutes >= 60` / `>= 120` open-position checks (domain-specific
  to held-position price age, NOT table-write freshness).

**Interpretation:** There is **no per-table freshness SLO for the 15
system-health tables anywhere in tree.** The two real, named constants
(`WRITER_THRESHOLD_MINUTES=20`, `HELD_POSITION_STALE_WARN_HOURS=24`) are general /
domain-specific, not per-table. The only writer-lag *cutoff* on a health surface
(`lag_threshold_minutes=30`) is a bare inline literal the new code cannot import.
Therefore a per-table SLO mapping is a genuine **new primitive** (listed at top)
and is the crux design decision (§"Key flagged decision"). Where a per-table SLO
should track writer-lag cadence, it must reference `WRITER_THRESHOLD_MINUTES`
(the named constant) or the `30`-minute source_calls precedent — not a freshly
invented number.

---

## Design

### 1. Additive output shape — RECOMMENDED: option (a), `status` key inside each per-table dict

Two additive options considered:

- **(a) `status` key INSIDE each per-table dict** → `{count, latest, status}`.
  Lowest blast radius: existing keys `count`/`latest` keep their exact meaning and
  values; consumers reading `health[table]["count"]` / `["latest"]` are untouched.
  The only new surface is an extra key per inner dict.
- **(b) New top-level `subsystems` / `status_summary` block** alongside the flat
  tables. Zero risk to existing keys, but duplicates the table list and creates
  two sources of truth (flat block + summary) that can drift.

**Recommendation: (a).** Justified by the consumer survey below.

**Consumer evidence (who reads the current shape — VERIFIED by grep + direct read):**

- Backend producer/consumer:
  - `dashboard/api.py:1058` — `return await db.get_system_health(_db_path)`. The
    endpoint returns the dict unmodified; no key-shape assumption.
- Frontend (the Health tab) — `dashboard/frontend/components/HealthTab.jsx`:
  - `:130` — `fetch('/api/system/health')` (inside `Promise.all`), `:139` —
    `if (hRes.ok) setHealth(await hRes.json())`. The component reads per-table
    `count`/`latest` **by key name** when rendering; it does not assert an exact
    inner-dict key count. An added `status` key is inert here. (Compiled bundle
    `dashboard/frontend/dist/assets/index-*.js` mirrors this.)
- Tests — `tests/test_dashboard_api.py`:
  - `:910` `resp = await client.get("/api/system/health")`; asserts presence of
    table keys and `data["candidates"]["count"] == 3`, `["active_chains"]["count"]
    == 1`, `["chain_matches"]["count"] == 1` (reads `count` by name — unaffected).
  - `:926` `resp = await empty_client.get("/api/system/health")` (empty-DB case);
    asserts `data["candidates"]["count"] == 0` and `data["candidates"]["latest"]
    is None` (reads `count`/`latest` by name — unaffected).
  - **Neither test asserts a strict inner-dict length / exact key-set**, so option
    (a) does not break the existing suite (verified by reading both tests). The new
    PR should *add* an assertion that `status` is present and valid (TDD §9/§11),
    not rewrite these.

**What breaks under each option:**
- **(a):** No existing key changes type or value, so `count`/`latest` readers (api
  passthrough, JSX renderer, both tests) are unaffected. The only theoretical
  risk — a consumer asserting the inner dict has exactly 2 keys — does **not**
  occur in this tree (confirmed above).
- **(b):** Existing keys equally unaffected, but the new top-level block risks
  list-drift vs the flat tables and adds a second code path the deferred
  Category-3 consumer must reconcile.

(a) wins on lowest-blast-radius-without-mutating-existing-meaning, the stated
default.

**Concrete JSON example (old keys preserved + new `status`):**

```json
{
  "candidates":        { "count": 1671, "latest": "2026-05-30T11:42:08", "status": "ok" },
  "alerts":            { "count": 88,   "latest": "2026-05-30T11:31:50", "status": "ok" },
  "narrative_signals": { "count": 0,    "latest": null,                  "status": "down" },
  "predictions":       { "count": 240,  "latest": "2026-05-29T02:10:00", "status": "degraded" },
  "active_chains":     { "count": 3,    "latest": "2026-05-30T11:40:00", "status": "ok" }
}
```

`count` and `latest` are byte-for-byte what they are today; `status` is purely
additive.

### 2. Pure derivation functions (deterministic, injected `now`)

New module `dashboard/health_status.py`, no I/O, no DB — mirrors the audit-script
pattern (pure core + injected `now`) so tests are deterministic with a FIXED_NOW
and never call `datetime.now()` internally.

```python
# dashboard/health_status.py  (new pure module — stdlib + typing only)
from datetime import datetime, timezone
from typing import Literal, Mapping

SubsystemStatus = Literal["ok", "degraded", "down", "unknown"]  # closed 4-value enum (D1)

def derive_subsystem_status(
    stats: Mapping,            # one table's {"count": int, "latest": str | None}
    slo_minutes: int | None,   # per-subsystem freshness budget; None => no SLO defined => unknown
    now: datetime,             # INJECTED — tests pass FIXED_NOW; never datetime.now() inside
) -> SubsystemStatus:
    """Pure, unit-testable core. Returns one of the 4 enum values. See rules below."""
    ...
```

There is no `expect_nonzero` table and no separate `derive_system_health_status`
mapper under D1: the empty-SLO-map + `unknown` semantics make per-table
"structurally-expected-nonzero" judgments unnecessary (a quiet table is
`unknown`, not `down`; `down` is reserved for genuine read errors via the
`count == -1` sentinel). The per-table wiring is a trivial dict-comprehension
inside `get_system_health`:

```python
result[table] = {
    **stats,
    "status": derive_subsystem_status(
        stats, HEALTH_FRESHNESS_SLO_MINUTES.get(table), now
    ),
}
```

`get_system_health` (db.py) is the only DB-layer caller that changes. It gains an
optional `now: datetime | None = None` seam (defaults to
`datetime.now(timezone.utc)`) so the integration test is deterministic; the
single existing call site (`api.py:1079`, `db.get_system_health(_db_path)`) keeps
working unchanged because the new parameter is optional. Per-table it now returns
`{**stats, "status": ...}` (a new dict, preserving `count`/`latest`). Adding a
key to a returned dict is **not a write** — no DB mutation.

### 3. Per-subsystem freshness-SLO map (`HEALTH_FRESHNESS_SLO_MINUTES`) — ships EMPTY

Per the operator decision (D1), we do **NOT** invent per-table thresholds. The
constant scan found NO per-table freshness SLO in tree, and several tables
(active_chains, narrative_signals, predictions, chain_*, learn_logs,
agent_strategy, second_wave_candidates) are legitimately sparse/zero — inventing
a number for them would manufacture false `degraded` labels with no evidence.

The map ships as:

```python
# dashboard/db.py  (module-level)
# Operator-fillable freshness SLO per system-health table, in minutes.
# Ships EMPTY: every readable table therefore derives status "unknown" until the
# operator adds a justified {table: minutes} entry here (reviewable in git). Do
# NOT pre-populate with guessed thresholds — see design doc D1.
HEALTH_FRESHNESS_SLO_MINUTES: dict[str, int] = {}
```

**Today's ship-state consequence (honest, intended):** with the map empty, every
table that reads successfully (`count >= 0`) derives `status == "unknown"`. The
only non-`unknown` value reachable today is `down`, and only when a table is
genuinely unreadable (`count == -1` sentinel from `_table_stats`). `ok` and
`degraded` become reachable for a given table the moment the operator adds an SLO
entry for it. When a future per-table SLO is justified, candidates to cite for
the number are `WRITER_THRESHOLD_MINUTES` (config.py, =20) for writer-cadence
tables or the source_calls `30`-minute precedent — but that is a later operator
edit, not part of this change.

### 4. Decision rules (closed 4-value enum, D1)

For a subsystem with stats `{count, latest}`, SLO `slo_minutes` (`None` ⇔ not in
the map), injected `now`. Evaluated **in this order**:

1. **`down`** if `count == -1` (the `_table_stats` read-error sentinel — table
   missing/unreadable). This is the ONLY source of `down`. (An empty-but-present
   table is `count == 0` and never `down`.)
2. **`unknown`** if `slo_minutes is None` (no SLO defined for this table). The
   function does not guess freshness without a justified budget. *This is the
   common case today — the map ships empty.*
3. **`unknown`** if `count == 0` (table present but empty; cannot assess freshness
   with zero rows, even when an SLO is defined).
4. **`unknown`** if `latest` is missing/unparseable while an SLO is defined
   (cannot honestly compute age → do not guess). `latest is None` with
   `count > 0` is structurally impossible via `_table_stats` (§B: `latest is None`
   ⇔ `count <= 0`); the parse-failure branch covers a future-refactor or
   malformed-timestamp case.
5. **`degraded`** if (SLO defined, `count > 0`, `latest` parses) **and**
   `(now - parse(latest)).total_seconds() / 60 > slo_minutes` (strictly greater —
   boundary `== slo_minutes` is `ok`). Mirrors source_calls
   `minutes_since_last_observed > lag_threshold_minutes`. `parse` follows §C
   exactly: `datetime.fromisoformat(latest.replace("Z","+00:00"))`, then
   naive→UTC normalize.
6. **`ok`** otherwise (SLO defined, `count > 0`, `latest` parses, age within SLO
   inclusive of the boundary).

Decisions pinned (flagged for reviewer):
- **`count == 0` mapping → `unknown`** (NOT `down`). An empty present table is not
  broken; `down` is reserved for genuine unreadability (`count == -1`).
- **Unparseable `latest` + SLO defined → `unknown`** (NOT `degraded`): we cannot
  compute age, so the honest answer is "unknown", not a guessed staleness.
- **Boundary `age == SLO` → `ok`** (strict `>` for `degraded`).

---

## Decision record (RESOLVED → D1): how no-SLO tables are handled

> **RESOLVED by operator 2026-05-30 in favour of D1.** The enum is closed and
> 4-valued (`ok | degraded | down | unknown`); the SLO map ships empty. The three
> options below are retained as the decision record that produced D1; they are no
> longer open. (The original doc recommended D2; the operator overrode to D1.)

The enum was originally *specified closed* as `ok | degraded | down`, but the
constant scan proves **none of the 15 system-health tables has a freshness SLO in
tree**, and several tables (active_chains, narrative_signals, predictions,
chain_*, learn_logs, agent_strategy, second_wave_candidates) are legitimately
sparse/zero. Three mutually-exclusive options were considered:

- **(D1) Add `unknown` to the enum** for tables with `slo_minutes is None`. Enum
  becomes `ok | degraded | down | unknown`. Honest (never guesses freshness on a
  table with no SLO) but widens the "closed" enum — needs explicit sign-off that
  the contract is 4-valued.
- **(D2) Conservative per-table default SLO** (the §3 FLAGGED numbers) for every
  table, keeping the enum strictly 3-valued. Every table always resolves to
  ok/degraded/down. Risk: a wrong default mislabels a legitimately-sparse table as
  `degraded` (false alarm) — but since this is read-only with NO alerting, a false
  `degraded` is cosmetic, not operational.
- **(D3) Exclude no-SLO tables from status derivation** — emit `status` only for
  tables with a real SLO, leaving the rest with `count`/`latest` only. Keeps the
  enum 3-valued and never guesses, but produces a **ragged** shape (some tables
  have `status`, some don't), complicating the deferred Category-3 consumer.

**Operator chose D1** (never-guess semantics + empty operator-fillable SLO map).
Rationale: inventing per-table thresholds (D2) manufactures `degraded` labels with
no supporting evidence on tables that are legitimately sparse; D1 is the honest
ship-state — a table with no justified SLO reports `unknown` rather than a guessed
`ok`/`degraded`, and the operator populates the map as evidence accrues. The
read-only-no-alerting property means `unknown` is purely informational. (D3's
ragged shape is rejected: every table still carries a `status` key under D1, it is
just `unknown` until an SLO is added.)

---

## Anti-scope contract (enforceable)

This change is READ-ONLY and additive. The following are OUT OF SCOPE and must be
mechanically verifiable as absent in the implementation diff:

- **No writes.** The derivation is a pure function; `get_system_health` gains a
  `status` key in its returned dict but issues **no** `INSERT`/`UPDATE`/`DELETE`/
  `CREATE`/`ALTER`. *Enforceable:* `dashboard/health_status.py` imports only stdlib
  (`datetime`, `typing`); a smoke test asserts its import set ⊆ stdlib and it
  contains no `execute(`/`INSERT`/`UPDATE`/`aiosqlite` tokens.
- **No new endpoint.** Extend the existing `/api/system/health`; no new
  `@app.get`/`@app.post`. *Enforceable:* diff adds zero `@app.` decorators.
- **No new table/column.** Zero schema-migration files; no `CREATE TABLE`/`ADD
  COLUMN`. *Enforceable:* diff touches no migrations path.
- **No alerting / urgency tiers / remediation.** No Telegram/Discord/alerter
  import; no severity ranking beyond the 3- (or 4-) valued enum; no auto-suspend /
  kill-switch / config-flip. *Enforceable:* new-module imports are pure-stdlib;
  grep diff for `alerter`/`send_telegram`/`auto_suspend` → none.
- **No trading-policy change.** The derivation never feeds gate.py / scorer.py /
  evaluator.py. *Enforceable:* no import of `dashboard.health_status` from `scout/`
  trading paths.
- **Dashboard Category-3 consumer stays deferred.** No frontend change in this PR
  (HealthTab.jsx untouched).

Suggested CI guard: `tests/test_health_status_anti_scope.py` (import-set allowlist
+ write-SQL/alerter/endpoint-decorator token scan on the new module).

---

## TDD test plan (D1)

Pure-function tests in `tests/test_system_health_status.py` (no app import;
deterministic via `FIXED_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)`):

1. **fresh + SLO → ok**: `count=10`, `latest` = FIXED_NOW − 5 min, SLO 60 ⇒ `ok`.
2. **stale + SLO → degraded**: `count=10`, `latest` = FIXED_NOW − 120 min, SLO 60
   ⇒ `degraded`.
3. **count == -1 (read-error sentinel) → down**: regardless of SLO ⇒ `down`.
4. **count == 0 + SLO defined → unknown** (empty present table is not assessable,
   not `down`).
5. **count == 0 + no SLO → unknown**.
6. **count > 0 + no SLO (slo_minutes=None) → unknown** (ship-state default).
7. **boundary `age == SLO` → ok**; `age == SLO + epsilon` → `degraded` (pin strict
   `>`).
8. **unparseable `latest` + SLO defined → unknown** (cannot compute age; do not
   guess `degraded`).
9. **timezone normalization**: naive ISO (`"2026-05-30T11:55:00"`) and `Z`-suffixed
   (`"2026-05-30T11:55:00Z"`) both parse to the same age vs UTC `now`.
10. **injected-now determinism**: same `stats`+SLO with two `now` values straddling
    the threshold flip ok↔degraded deterministically; assert via monkeypatch that
    `dashboard.health_status` never calls `datetime.now`.

Integration / regression tests against `get_system_health` (`tmp_path` aiosqlite,
pytest-asyncio auto mode) — these import `dashboard.db` only (NOT the FastAPI
app), so they run on Windows regardless of the documented aiohttp/OpenSSL issue:

11. **additive-key non-breaking + flat-shape preserved** (REGRESSION): seed a DB;
    assert every per-table value has exactly `{"count", "latest", "status"}` and
    that `count`/`latest` are byte-for-byte what the pre-status code returned for
    the same fixture (golden recompute via direct `_table_stats`).
12. **empty SLO map → all readable tables `unknown`** (the ship-state today): seed
    a non-empty DB, call `get_system_health` with the shipped empty map and an
    injected `now`; assert every present table's `status == "unknown"` (and any
    genuinely-absent table, if simulated, is `down`).
13. **end-to-end with a temporarily-populated map** (does NOT touch the shipped
    constant): monkeypatch `HEALTH_FRESHNESS_SLO_MINUTES` to `{"candidates": 60}`,
    seed candidates fresh vs stale, assert `ok` / `degraded` accordingly with
    injected `now`.

App-level smoke (`GET /api/system/health` via the FastAPI TestClient) is covered
by the existing `tests/test_dashboard_api.py` `TestSystemHealth` cases; those
assert `count`/`latest` by name and remain green (additive `status` key only). On
Windows that file may not import due to the documented aiohttp/OpenSSL issue
(`reference_windows_openssl_workaround`) — if so it is CI-on-Linux-deferred and
the pure + db-layer files above carry the behavioral coverage.

---

## Risks / edge-cases flagged for reviewer

1. **PRIMARY — no-SLO tables (D1 vs D2 vs D3).** The enum is "closed" but no table
   has a real SLO and several are legitimately sparse. Pick D1 (`unknown`,
   4-valued) / D2 (defaults, 3-valued, recommended) / D3 (ragged, discouraged).
   Implementation enum cardinality is gated on this.
2. **Additive shape (a) vs (b).** Recommended (a). The one theoretical break risk
   — a consumer asserting the inner dict has exactly 2 keys — does NOT occur:
   `tests/test_dashboard_api.py:910/926` and the JSX renderer read keys by name
   (verified). Safe to ship (a).
3. **`latest` format coupling.** The derivation parses `latest` (verified §B: raw
   ISO string from `MAX(time_col)`). If `_table_stats` ever changes `latest`
   formatting, the parser breaks — TDD §7 pins the format so a future
   `_table_stats` change fails loudly here.
4. **Timezone correctness.** SQLite-stored timestamps may be naive; `now` is UTC.
   The parser MUST normalize naive→UTC (exactly as `get_source_calls_health` does,
   §C) or the stale math is off by the local offset. Confirm the 15 tables store
   UTC (naive or `Z`-suffixed) timestamps; TDD §7 covers both forms.
5. **`expect_nonzero` per table is a judgment call.** alerts, active_chains,
   narrative_signals, chain_*, learn_logs are legitimately zero (quiet window /
   disabled-state); §3 sets these `expect_nonzero=False` to avoid false `down`.
   Reviewer should sanity-check each row — a wrong `True` here is the most likely
   source of a misleading `down`.
6. **`down`-on-missing-table — RESOLVED via the `count == -1` sentinel (D1).**
   `_table_stats` historically swallowed every exception into
   `{"count":0,"latest":None}`, making a truly-missing/unreadable table
   indistinguishable from an empty one. Under D1 we add the minimal READ-ONLY
   change: on a genuine table-read error `_table_stats` returns
   `{"count": -1, "latest": None}` (the sentinel), which the deriver maps to
   `down`. An empty present table still returns `count == 0` → `unknown`.
   **Backward-compat with the existing hardening test:** the change is scoped so
   that the *generic* failure path the existing
   `tests/test_dashboard_hardening.py::test_table_stats_accepts_valid_identifiers`
   exercises (passing `conn=None`, which raises `AttributeError` inside the body)
   continues to return `count == 0`. Only a genuine SQLite read error
   (`aiosqlite.OperationalError` / `sqlite3.OperationalError` / `DatabaseError` —
   e.g. `no such table`) yields the `-1` sentinel; the catch-all
   `except Exception` still returns `count == 0`. This keeps the existing test
   green while making `down` reachable for real missing tables. The empty-DB
   integration test (`tests/test_dashboard_api.py:926`, asserting
   `candidates.count == 0` on the *empty-but-present* path) is unaffected — that
   path is a successful read returning 0 rows, never the error path.
7. **Read-only guarantee.** Adding a key to the returned dict is not a write, but
   confirm no caller persists `get_system_health` output back to the DB — the
   consumer survey §1 (api passthrough at api.py:1058 + JSX fetch + tests) is the
   evidence; none write it back.
8. **Determinism seam for the integration test.** `get_system_health` will call
   `datetime.now(timezone.utc)` in production; the end-to-end test (§10) needs a
   seam — either monkeypatch `datetime` in db, or factor `now` as a defaulted param
   `get_system_health(db_path, *, now=None)`. The pure functions are already
   deterministic; only the DB entrypoint needs the seam. **Flag whether adding a
   defaulted `now=None` param to `get_system_health` is acceptable** (it is
   additive and keeps the single existing call site at api.py:1058 working).

**New primitives introduced:** [config setting `TG_ALERT_DEDUP_WINDOW_HOURS` (int, default 24, ge=0); one new `tg_alert_log.outcome` string value `blocked_dedup_24h` (added via a table-rebuild CHECK-widening migration `_migrate_tg_alert_log_dedup_outcome` preserving ALL existing values); three structured log events `tg_alert_dispatched` (pre-send), `tg_alert_delivered` (post-send), `tg_alert_suppressed` (suppress path); a structured log on the previously-silent `db._conn is None` early-exit. NO new column, NO conviction sourcing, NO scoring change.]

> Slice 1 = strict 24h dedup ONLY; conviction-margin re-alert override DEFERRED to BL-NEW-TG-CONVICTION-AVAILABILITY + a later override build.

# Design: 24h per-token TG paper-trade-open strict dedup + audit log

Date: 2026-05-30
Branch: `feat/tg-alert-24h-dedup` (worktree `C:\projects\gecko-alpha-wt\tg-dedup`)
Status: BUILD (slice rescoped per operator decision 2026-05-30).
Scope: operator-approved BUILDABLE slice — strict 24h per-token dedup + audit ONLY.

## Scope of THIS build (operator decision, final)

Ship ONLY the strict 24h per-token dedup + audit log. The conviction-margin
re-alert OVERRIDE is DEFERRED to a separate future build (see "DEFERRED" section
below) because conviction is not reachable in the TG dispatch paths today
(confirmed by 2 Codex runs + structural review).

This slice is **dispatch-quality ONLY.** It never touches source ingestion,
never blocks a signal from being generated / scored / gated / paper-traded; it
only decides whether a paper-trade-open TG alert is SENT. The `paper_trades` row
is opened by the engine regardless of dispatch outcome; suppression skips ONLY
the Telegram send.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Alert dedup / notification suppression / rate-limiting | none found — checked `hermes-agent.nousresearch.com/docs/skills` for notification, alert, dedup, rate-limit, cooldown, throttle, idempotent-dispatch. Hermes covers outbound notification *delivery* primitives (xurl/OAuth — checked during BL-NEW-NARRATIVE-OPERATOR-ALERT-WIRE 2026-05-13, returned 503/none) but not stateful per-key dispatch suppression bound to a local relational store. | Build in-repo. This is a dispatch-quality gate reading/writing rows in gecko-alpha's own SQLite `tg_alert_log` table via the existing `Database` connection. No external capability maps to "suppress a TG send if this token_id was already sent within N hours." Custom code is correct. |

awesome-hermes-agent ecosystem check: scanned for notification-dedup / alert-throttling / idempotent-dispatch packages; none provide a SQLite-backed per-key dispatch cooldown. Verdict: no ecosystem component applies; this is a one-table, in-place extension of the existing `notify_paper_trade_opened` dispatcher.

## Problem & motivation

The same token frequently produces multiple paper-trade-open signals over a day
(different signal_types, or the same signal_type re-firing across cycles). Each
fires a separate Telegram alert once it clears the existing 6h per-token
cooldown, producing operator-facing noise. Goal: at most one paper-trade-open
alert per token per 24h. Every suppression must be auditable (DB row +
structured log) so noise reduction is measurable without a forward soak.

## Ground truth (verified against worktree source, file:line)

Base `686d0651` (worktree HEAD == base). Confirm line numbers after any rebase.

### `scout/trading/tg_alert_dispatch.py`
- `notify_paper_trade_opened(db, settings, session, *, paper_trade_id, signal_type, token_id, symbol, entry_price, amount_usd, signal_data)` (line 204). It takes `token_id: str` — NOT a `CandidateToken`. There is NO conviction in scope.
- Flow: eligibility gate → atomic claim under `async with db._txn_lock` (lines 245–284): compute `cutoff = now - TG_ALERT_PER_TOKEN_COOLDOWN_HOURS`; `SELECT 1 FROM tg_alert_log WHERE token_id=? AND outcome='sent' AND alerted_at>=? LIMIT 1`; if found → INSERT `blocked_cooldown` + commit + return; else INSERT pre-emptive `'sent'`, capture `sent_row_id`, commit. Then Minara lookup (outside lock), format, `alerter.send_telegram_message(..., parse_mode=None, raise_on_failure=True)` (lines 333–339), demote-to-`dispatch_failed` on error.
- `_check_cooldown(db, settings, token_id) -> bool` (line 94) — keyed on `token_id`, cutoff from `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS`. **NOT dead** — 4 live tests (tests/test_tg_alert_dispatch.py:94/111/131/148). Left semantically intact.
- There is currently NO success log in the dispatcher (only `tg_alert_dispatch_failed` on the failure path). The `if db._conn is None: return` early-exit (line 243) is silent.

**Existing outcome values:** `sent`, `blocked_eligibility`, `blocked_cooldown`, `dispatch_failed`, `announcement_sent`, `m1_5c_announcement_sent`.

### `scout/config.py`
- `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS: int = 6` (line 426). UPPERCASE field-name == env var (no `env_prefix`). New settings follow the same convention.

### `scout/db.py`
- `tg_alert_log` is CREATED inside `_migrate_tg_alert_eligible_v1()` (CREATE at line 3567) with the CHECK at line 3573 and index `idx_tg_alert_log_token ON tg_alert_log(token_id, alerted_at)` (line 3581). NOT in `_create_tables`.
- The CHECK is widened via a **table-rebuild** migration `_migrate_tg_alert_log_m1_5c_outcome()` (line 3616): `BEGIN EXCLUSIVE`, `paper_migrations` sentinel, sqlite_master substring guard, regex pattern-match on the CHECK, copy rows, DROP, RENAME, recreate index. Idempotent.
- Migrations are invoked sequentially in `initialize()` (db.py:90–...). `_migrate_tg_alert_log_m1_5c_outcome()` is at db.py:106.

## Proposed design (slice)

### Config (scout/config.py)
```python
TG_ALERT_DEDUP_WINDOW_HOURS: int = 24
# Per-token dedup window (hours) for paper-trade-open TG alerts. Once a token's
# alert is SENT, further alerts for the same token_id are suppressed for this
# many hours. SUPERSEDES the legacy TG_ALERT_PER_TOKEN_COOLDOWN_HOURS as the
# single live dispatch gate (see reconciliation). 0 disables dedup entirely
# (clean revert), with no off-by-one.
```
`ge=0` is enforced by Pydantic via a `Field(default=24, ge=0)` on the field.
`0` disables dedup: the implementation short-circuits the prior-row query when
`window_hours == 0` and always sends, so there is no boundary ambiguity.

`TG_ALERT_PER_TOKEN_COOLDOWN_HOURS` is KEPT for back-compat (existing `.env`
files + the standalone `_check_cooldown` helper and its 4 tests still reference
it) but NO LONGER drives the dispatch decision.

### Window reconciliation (24h supersedes 6h)
The inline atomic-claim block in `notify_paper_trade_opened` currently reads
`TG_ALERT_PER_TOKEN_COOLDOWN_HOURS` (6h). This slice switches that inline gate to
read `TG_ALERT_DEDUP_WINDOW_HOURS` (24h). After this change there is exactly ONE
live window authority: the inline dedup gate. 24h ⊃ 6h, so collapsing loses no
suppression the 6h gate gave. `_check_cooldown` remains intact (its tests
unchanged) but is no longer the gate the dispatcher consults — documented here so
a future reader does not assume two windows are both live.

### tg_alert_log schema widening
Add ONE new outcome enum value: `blocked_dedup_24h`. **No new column.**

Because `outcome` has a CHECK constraint, widening requires the table-REBUILD
migration template (reuse the `_migrate_tg_alert_log_m1_5c_outcome` shape:
`BEGIN EXCLUSIVE`, `paper_migrations` sentinel, sqlite_master substring guard,
regex CHECK pattern, copy/drop/rename, recreate index, schema_version row).

**CRITICAL:** the rebuilt CHECK list MUST preserve ALL existing values —
`sent`, `blocked_eligibility`, `blocked_cooldown`, `dispatch_failed`,
`announcement_sent`, `m1_5c_announcement_sent` — PLUS `blocked_dedup_24h`. (A
prior draft dropped `m1_5c_announcement_sent`; that regression is explicitly
forbidden and is asserted in a test.)

Migration rules:
- New method `_migrate_tg_alert_log_dedup_outcome()`, wired into `initialize()`
  AFTER `_migrate_tg_alert_log_m1_5c_outcome()` (so it operates on the
  already-m1_5c-widened CHECK).
- Idempotent via a `paper_migrations` sentinel name unique to THIS migration
  (`bl_tg_alert_log_dedup_outcome`) AND a guard string unique to THIS migration
  (presence of `blocked_dedup_24h` in the live `table_sql`), distinct from the
  m1_5c sentinel/guard.
- Recreate `idx_tg_alert_log_token` INSIDE the migration (the rebuild drops the
  old table; CREATE INDEX in `_create_tables` is a no-op for an existing table —
  memory `feedback_ddl_before_alter`, which caused the BL-060 crash).

### Dedup algorithm (slice — no conviction)
Replaces the inline atomic-claim block (lines 245–284). The structure (lock,
SELECT-then-INSERT, `sent_row_id` capture, demote-on-failure) is preserved; only
the cutoff source changes + the new outcome + the audit logs are added.

```text
1. Eligibility gate (unchanged) → blocked_eligibility on fail.
2. window = settings.TG_ALERT_DEDUP_WINDOW_HOURS
3. If db._conn is None → emit tg_alert_no_conn log (was silent), return.
4. async with db._txn_lock:
     a. if window > 0:
          cutoff = now - window*3600
          prior = SELECT 1 FROM tg_alert_log
                  WHERE token_id=? AND outcome='sent' AND alerted_at>=? LIMIT 1
          if prior:
              INSERT blocked_dedup_24h row (detail=f"window_h={window}")
              commit
              emit tg_alert_suppressed {token_id, signal_type, window_hours,
                                        prior_alerted_at, reason:"dedup_24h"}
              return  # do NOT send
     b. INSERT pre-emptive 'sent' row, capture sent_row_id, commit.
5. (outside lock) Minara lookup, format body.
6. emit tg_alert_dispatched {paper_trade_id, signal_type, token_id} (pre-send)
7. send_telegram_message(..., parse_mode=None, raise_on_failure=True)
8. emit tg_alert_delivered {paper_trade_id, signal_type, token_id} (post-send)
9. on send exception → demote pre-emptive row to dispatch_failed (unchanged).
```

When `window == 0` the prior-row query is skipped entirely and the dispatcher
always claims a `'sent'` row + sends (clean revert; no off-by-one).

### Audit logging (CLAUDE.md §12b)
- `tg_alert_dispatched` {paper_trade_id, signal_type, token_id} — BEFORE the send.
- `tg_alert_delivered` {paper_trade_id, signal_type, token_id} — AFTER the send returns.
- `tg_alert_suppressed` {token_id, signal_type, window_hours, prior_alerted_at, reason:"dedup_24h"} — on the suppress path.
- `tg_alert_no_conn` on the previously-silent `db._conn is None` early-exit.

These are structlog events, NOT Telegram sends. The actual TG send keeps
`parse_mode=None` (signal names contain underscores — §12b Class-3). Every path
(sent / suppressed / no-conn / failed) is auditable; no silent drop.

## DEFERRED — conviction-override (separate build)

The conviction-margin re-alert override (allow a re-alert inside the window when
the new signal's conviction materially exceeds the prior alert's conviction) is
**NOT built in this slice.** It is deferred because conviction is structurally
unreachable from the TG paper-trade dispatch path today:

- Conviction is computed only in `scout/gate.py` (≈gate.py:70:
  `conviction = quant_score*0.6 + narrative_score*0.4`, chain-boosted, 100-capped),
  invoked from EXACTLY ONE site `scout/main.py:1093`, inside the per-token alert
  loop.
- That call site is structurally DISJOINT from the paper-trade dispatch path:
  `signals.py` → `engine.open_trade(token_id: str)` → `_spawn_tg_alert` →
  `notify_paper_trade_opened`. `open_trade` takes `token_id` (a str), NOT a
  `CandidateToken`, so the conviction-bearing object never reaches the dispatcher.
- For the `first_signal` path, the trade opens BEFORE the gate runs (open at
  ≈main.py:1082 via `trade_first_signals`; gate at ≈main.py:1093), so conviction
  does not exist yet at dispatch time (`token.conviction_score` is `None`).
- The 6 non-`first_signal` dispatch paths (volume_spike, gainers_early,
  losers_contrarian, trending_catch, narrative_prediction, chain_completed) are
  driven by DB-snapshot dict rows and never hold a `CandidateToken` at all — no
  conviction field in scope.

Confirmed by 2 Codex runs + structural review. Building the override now would
require plumbing conviction across all 7 dispatch paths first. The override build
therefore depends on a new follow-up backlog item:

**BL-NEW-TG-CONVICTION-AVAILABILITY (PROPOSED)** — persist/derive conviction so
it is available across all 7 TG dispatch paths (e.g. a `token_id`-keyed lookup of
the persisted gate-level `conviction_score`, with per-cohort coverage measured
first). This design does NOT build that; the override build is gated on it.

Items explicitly removed from the live design (now deferred): the
conviction-margin override algorithm, the `conviction` `tg_alert_log` column, the
`_extract_score` helper, the `TG_REALERT_CONVICTION_MARGIN` setting, the
conviction-plumbing section, and the `allowed_realert_stronger` outcome value.
None are built in this slice.

## CG-slug known-accepted limitation
`token_id == candidates.contract_address`, which for `chain=coingecko` rows holds
the CoinGecko slug, not the on-chain address (memory
`feedback_cg_slug_not_address_for_cg_sourced_rows`). Dedup keys on `token_id`, so
the same underlying token appearing once as a CG slug and once as an on-chain
address is treated as two tokens and not deduped against each other. **KNOWN
ACCEPTED limitation, NOT fixed here.**

## Known non-audited path (follow-up)

`scout/trading/engine.py:_spawn_tg_alert` (≈engine.py:484) guards the dispatch
on a usable entry price: it computes
`effective_entry = trade.get("entry_price") or trade.get("price") or 0.0`,
coerces to `float`, and on `effective_entry <= 0` returns early at ≈engine.py:500
after emitting ONLY a `log.warning("tg_alert_skipped_invalid_entry", ...)`. On
that early return `notify_paper_trade_opened` is never called, so **NO
`tg_alert_log` row is written** — the drop is observable in journalctl but is
NOT captured in the dispatch audit table the rest of this slice relies on. Every
path INSIDE `notify_paper_trade_opened` (sent / blocked_eligibility /
blocked_dedup_24h / dispatch_failed) writes a row; this one upstream
invalid-entry-price early return is the single non-audited drop in the
paper-trade-open alert path.

This is a **pre-existing** gap, NOT introduced by this slice. Closing it
requires an `engine.py` change (out of this slice's anti-scope — `engine.py` is
explicitly forbidden below) plus a new `tg_alert_log.outcome` CHECK value (e.g.
`blocked_invalid_entry`), which is a second table-rebuild migration. It is
therefore documented here and deferred, NOT fixed in this slice.

**BL-NEW-TG-ALERT-INVALID-ENTRY-AUDIT (PROPOSED)** — make the invalid-entry-price
early return in `_spawn_tg_alert` auditable: write a `tg_alert_log` row with a new
`blocked_invalid_entry` outcome (table-rebuild CHECK widening, preserving all
existing values) instead of dropping silently with only a warning log. Scope: a
1-line `engine.py` change at the early-return site + one migration + tests.
Gated behind its own slice because it touches `engine.py` (anti-scope here).

## Anti-scope (runtime contract per memory `feedback_anti_scope_as_runtime_contract`)

MUST NOT:
- touch `scout/ingestion/*`, `scout/scorer.py`, `scout/gate.py`, `scout/main.py`,
  `scout/trading/signals.py`, or `scout/trading/engine.py`;
- compute, derive, recalibrate, or alter any score (this slice reads no
  conviction at all);
- block any signal from the pipeline or from opening a `paper_trades` row;
- add any HTTP endpoint or dashboard route;
- key dedup on `signal_type` (token_id grain only; signal_type still recorded per row);
- send any new Telegram message (suppression is silent; audit = DB row + structured log);
- be irreversible (controlled by `TG_ALERT_DEDUP_WINDOW_HOURS`; 0 = off).

Allowed files: `scout/config.py`, `scout/db.py`,
`scout/trading/tg_alert_dispatch.py`, `tests/`.

## TDD test plan (slice)

1. **suppress-within-24h**: prior `'sent'` row inside 24h → records
   `blocked_dedup_24h`, no send.
2. **allow-after-24h**: prior `'sent'` row older than 24h → sends; row `'sent'`.
3. **first-alert-for-token**: no prior row → sends.
4. **window=0 disables dedup**: every attempt sends (clean revert), no
   suppression rows, no off-by-one.
5. **existing 4 `_check_cooldown` tests pass UNCHANGED.**
6. **migration widens outcome preserving all 6 prior values + adds
   `blocked_dedup_24h`**: insert a row with each of the 7 values succeeds; an
   invalid value still rejected; `m1_5c_announcement_sent` regression guard.
7. **migration idempotent**: re-run is a no-op.
8. **dispatched / delivered / suppressed logs emitted on the right paths**
   (structlog capture).
9. **paper-trade row NOT affected by suppression** (suppression skips only the
   TG send; existing concurrency / demotion tests reconciled to the 24h window).

## Fold round 1 (2026-05-30 rescope)

- Retitled from "...dedup with conviction-margin re-alert override + ..." to
  "...strict dedup + audit log".
- REMOVED from the live design and moved to "DEFERRED — conviction-override":
  the conviction-margin override algorithm, the `tg_alert_log.conviction` column,
  `_extract_score`, the `TG_REALERT_CONVICTION_MARGIN` setting, the
  conviction-plumbing section (incl. dispatch-time lookup + secondary
  kwarg-thread mechanism), and the `allowed_realert_stronger` outcome. Rationale
  recorded: conviction is computed only at gate.py:70 via main.py:1093,
  structurally disjoint from the `signals.py → engine.open_trade(token_id:str) →
  _spawn_tg_alert → notify_paper_trade_opened` dispatch path; `open_trade` takes
  `token_id` not `CandidateToken`; first_signal opens before the gate; the 6
  non-first_signal paths are row-driven dicts with no token. New dependency
  filed: BL-NEW-TG-CONVICTION-AVAILABILITY (PROPOSED).
- Dedup window: 24h `TG_ALERT_DEDUP_WINDOW_HOURS` SUPERSEDES the legacy 6h
  `TG_ALERT_PER_TOKEN_COOLDOWN_HOURS` as the single live gate (reconciliation
  documented). `_check_cooldown` kept (4 live tests), semantically intact, no
  longer the gate.
- Outcome widening: exactly ONE new value `blocked_dedup_24h`; the rebuilt CHECK
  preserves all 6 prior values (m1_5c regression explicitly forbidden + tested).
- §12b audit logs added: `tg_alert_dispatched` / `tg_alert_delivered` /
  `tg_alert_suppressed` + a log on the previously-silent `db._conn is None`
  early-exit.
- Config bound: `ge=0`, `0` = disabled, handled with no off-by-one.
- First line (New primitives) updated to the slice-accurate list. Hermes-first
  table retained.

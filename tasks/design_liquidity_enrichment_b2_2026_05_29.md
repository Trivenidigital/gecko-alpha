**New primitives introduced:**
- New DB columns on `candidates`: `liquidity_usd_enriched` (REAL, nullable), `liquidity_enriched_source` (TEXT, nullable), `liquidity_enriched_at` (TEXT, nullable, ISO-8601), `liquidity_enriched_confidence` (TEXT, nullable, enum: `definite` / `multi_chain` / `cg_slug_unresolvable` / `dex_no_match` / `stale`)
- New schema migration row: `bl_new_liquidity_enrichment_v1` (in `paper_migrations` with `cutover_ts`)
- New cron script: `scripts/backfill_dexscreener_liquidity.py` (read CG slug or on-chain address, resolve to chain+address via CG `/coins/{id}`, look up liquidity via DexScreener `/tokens/v1/{chain}/{address}`, write to enrichment columns)
- New watchdog script: `scripts/check_liquidity_enrichment_lag.py` (per §12a freshness SLO)
- New dashboard read field on Today's Focus rows: `liquidity_usd_enriched`, `liquidity_enriched_source`, `liquidity_enriched_confidence`, `liquidity_enriched_at`
- New systemd timer or crontab entry for the cron writer (Phase 2 of the build sequence)

# Liquidity Enrichment Design — Option (b2) Phase 1 (2026-05-29)

**Backlog item:** `BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS` (PR-B in the Today's Focus product roadmap)

**Audit scope:** read-only design doc. Per operator's reviewer recommendation on PR #322 — choosing option (b2) "background cron backfill via DexScreener search" and shipping as read-only enrichment first. No implementation in this PR; the design here authorizes a separate phased build sequence.

## Operator-Pinned Guardrails (verbatim, 2026-05-29)

1. **No render-time external calls.** Cron writes; dashboard reads pre-populated DB.
2. **Persist source and timestamp**: `liquidity_usd`, `liquidity_source`, `liquidity_updated_at`, maybe `liquidity_confidence`.
3. **Do not use symbol-only resolution** unless explicitly flagged low-confidence.
4. **Dashboard renders `unavailable` or `unverified`**, never pretends liquidity is certain.
5. **Add freshness SLO/watchdog** if a new writer/table ships.
6. **No ranking, filtering, alerting, sizing, or execution** based on liquidity until coverage/accuracy is measured.

Each guardrail is enforced by a named design element below.

## Triage Context

Prior decision packet (PR #322, `tasks/decision_liquidity_backfill_2026_05_29.md`) selected option (b2). Headline state from that packet:

- `chain="coingecko"` rows: 0/995 coverage (0.0%); paper cohort is structurally CG-sourced.
- `scout/ingestion/coingecko.py` + `scout/models.py:105` hardcodes `liquidity_usd=0.0` because CG `/coins/markets` Demo does not surface liquidity.
- DexScreener + GeckoTerminal writers work correctly for DEX-sourced rows (35.4% global coverage).

## Critical Design Constraint Discovered Today (2026-05-29)

Probing `candidates` on srilu prod reveals that **`contract_address` for `chain="coingecko"` rows holds the CoinGecko coin SLUG**, not an on-chain contract address. Sample rows from the recent paper cohort:

| token_id (= contract_address) | symbol | signal_type |
|---|---|---|
| `staynex` | STAY | chain_completed |
| `xmaquina` | DEUS | chain_completed |
| `spark-2` | spk | narrative_prediction |
| `banana-gun` | banana | narrative_prediction |
| `billions-network` | BILL | chain_completed |
| `anime` | anime | narrative_prediction |
| `dex:solana:5UUH9RTDi...` | TROLL | tg_social |

The TROLL row demonstrates that some signal_types DO carry on-chain addresses (`dex:solana:<base58>` prefix). But the dominant paper-corpus signals (`chain_completed`, `narrative_prediction`) carry CG slugs.

**Implication:** DexScreener `/tokens/v1/{chain}/{address}` cannot be called directly for CG-slug rows. A resolution hop is required first: CG `/coins/{id}` returns a `platforms` mapping `{ethereum: "0x...", solana: "...", base: "0x..."}`. The cron then calls DexScreener per (chain, address) pair.

This is **deterministic CG-slug resolution, NOT symbol-fuzzy resolution** — honors guardrail #3.

## Phase 1 Design

### Schema migration

Add to `candidates` (nullable, no DEFAULT to preserve absence-vs-zero semantics):

```sql
ALTER TABLE candidates ADD COLUMN liquidity_usd_enriched REAL;
ALTER TABLE candidates ADD COLUMN liquidity_enriched_source TEXT;
ALTER TABLE candidates ADD COLUMN liquidity_enriched_at TEXT;
ALTER TABLE candidates ADD COLUMN liquidity_enriched_confidence TEXT;
```

Insert migration marker into `paper_migrations`:

```sql
INSERT INTO paper_migrations (name, cutover_ts) VALUES (
    'bl_new_liquidity_enrichment_v1',
    '<UTC-now at deploy time>'
);
```

**Why new columns vs writing to existing `liquidity_usd`:** the aggregator (`scout/aggregator.py:11-24`) does last-write-wins on `liquidity_usd` and the field is not in `_PRESERVE_FIELDS`. A cron-written value would be silently clobbered on the next CG re-ingest. Decoupled enrichment columns sidestep the aggregator entirely; dashboard `COALESCE(liquidity_usd_enriched, liquidity_usd, NULL)` becomes the read path.

### Confidence enum

| value | meaning | how to render in dashboard |
|---|---|---|
| `definite` | Single-chain DexScreener match for a CG-resolved (chain, address) pair; or direct DexScreener match for a `dex:chain:address`-prefixed `token_id` | "Liquidity: $X (DexScreener, <fresh-age>)" |
| `multi_chain` | CG `platforms` returns >1 chain; cron writes the HIGHEST-liquidity chain's value with `multi_chain` flag | "Liquidity: $X (DexScreener, multi-chain)" — operator-visible advisory |
| `cg_slug_unresolvable` | CG `/coins/{id}` returns no `platforms` mapping (slug-only token, no on-chain listing) | "Liquidity: unavailable" |
| `dex_no_match` | CG resolves to (chain, address) but DexScreener returns no pair | "Liquidity: unavailable" |
| `stale` | Cron last-success age > `LIQUIDITY_ENRICHMENT_STALE_SEC` (default: 3600s) | "Liquidity: unverified" |

`stale` enforces guardrail #4 — even fresh-looking data is shown as "unverified" if past the SLO.

### Cron writer — `scripts/backfill_dexscreener_liquidity.py`

**Resolution sequence per candidate row:**

1. If `liquidity_enriched_at` is fresher than `LIQUIDITY_ENRICHMENT_TTL_SEC` (default 1800s = 30min): skip.
2. If `contract_address` matches the `dex:<chain>:<address>` prefix shape: parse, call DexScreener `/tokens/v1/{chain}/{address}` directly. Skip CG hop.
3. Else (CG slug or other): call CG `/coins/{id}?localization=false&community_data=false&developer_data=false&tickers=false&market_data=false` to get `platforms` mapping.
   - If `platforms` is empty / missing: write `liquidity_enriched_confidence='cg_slug_unresolvable'`, clear `liquidity_usd_enriched`, stamp `liquidity_enriched_at`.
   - If `platforms` has 1 chain: call DexScreener `/tokens/v1/{platform-chain}/{address}`.
   - If `platforms` has >1 chains: call DexScreener for EACH chain, pick the highest-liquidity match, write with `confidence='multi_chain'`.
4. DexScreener response handling:
   - Pair found with `liquidity.usd > 0`: write `liquidity_usd_enriched = pair.liquidity.usd`, `liquidity_enriched_source = 'dexscreener_v1'`, stamp `liquidity_enriched_at`.
   - No pair / `liquidity.usd = 0`: write `liquidity_enriched_confidence = 'dex_no_match'`, clear value, stamp.
5. On HTTP 429 or 5xx after retries: structured log + skip; do NOT clobber existing `liquidity_enriched_at` (preserves last-success). Retry on next cron tick.
6. UPDATE statement format: `UPDATE candidates SET ... WHERE contract_address = ?` — no INSERT, no DELETE; cron writes only enrichment columns.

**Rate budget:**

- CG: 30 req/min Demo cap (= 0.5 req/s). Shared with existing ingest; coordinated via `scout/ratelimit.py` token-bucket. Cron must NOT monopolize the bucket.
- DexScreener: 300 req/min documented public quota; the existing per-chain ingest uses semaphore=5 concurrent.
- **Per-tick budget:** bounded by `LIQUIDITY_BACKFILL_BATCH_MAX=50` rows per 15-min tick. Each row consumes 1 CG call (slug resolution) + 1-3 DexScreener calls (per resolved chain). Worst-case per tick: 50 CG + 150 DexScreener calls over 15 minutes = ~3.3 CG req/min and ~10 DexScreener req/min, well within shared budgets.
- **Backfill drain time:** at 50 rows/tick × 4 ticks/hour = 200 rows/hour. 995-row backlog drains in ~5 hours of cron running (multiple ticks; bounded by ratelimiter).
- **Steady-state cost:** ~50-100 new CG-sourced candidates per day → 50-100 CG calls/day + 50-300 DexScreener calls/day. Absorbed in 1-2 cron ticks per day.

**Cron cadence:** every 15 min. Per-tick batch bounded by `LIQUIDITY_BACKFILL_BATCH_MAX=50` to share CG budget with ingest. Backlog drains over ~5 hours of ticks for the one-time 995-row initial run; steady-state ticks process new rows under their per-tick budget.

**Killswitch read at tick start:** the cron reads `settings.LIQUIDITY_ENRICHMENT_ENABLED` at the START of each 15-min tick (not just process-start). Toggling the flag in `.env` takes effect at the next tick without requiring service restart.

### Watchdog — `scripts/check_liquidity_enrichment_lag.py`

Per CLAUDE.md §12a: every new pipeline table / new writer ships with a freshness SLO and watchdog at the same PR.

- SLO: max(`liquidity_enriched_at`) within last 30 min for at least 80% of `candidates` rows opened in the last 7 days.
- Watchdog runs every 15 min (offset from cron by 5 min so the cron has time to produce rows).
- On SLO breach: structured log + curl-direct TG alert (per `project_vps_backup_rotation_2026_05_09.md` memory — NOT `scout.alerter`).
- Telegram alert body: `parse_mode=None` per CLAUDE.md §12b (signal-name-like text could contain underscores).

### Dashboard read path

`dashboard/db.py` `get_todays_focus(...)`:

- SELECT `c.liquidity_usd, c.liquidity_usd_enriched, c.liquidity_enriched_source, c.liquidity_enriched_at, c.liquidity_enriched_confidence` from joined `candidates`.
- Emit on each row: `liquidity_usd_effective = COALESCE(liquidity_usd_enriched, liquidity_usd)` (or NULL).
- Emit `liquidity_meta`: `{source, age_sec, confidence}` where age_sec = `now - liquidity_enriched_at` if enriched, else null.
- Mark `liquidity_meta.confidence='stale'` if `age_sec > LIQUIDITY_ENRICHMENT_STALE_SEC` regardless of stored confidence.

`/api/todays_focus` Pydantic envelope: add optional `liquidity_meta` to row schema; use the `response_model_exclude_none=True` + untyped `list[list]` pattern documented in `feedback_fastapi_wire_shape_reviewer_pattern.md` to avoid the PR-C 3-hotfix chain.

Dashboard frontend rendering rules:

- `liquidity_usd_effective` null → `"Liquidity: unavailable"` (no chip, no number).
- `liquidity_meta.confidence in ('cg_slug_unresolvable', 'dex_no_match')` → `"Liquidity: unavailable"`.
- `liquidity_meta.confidence == 'stale'` → `"Liquidity: unverified"` chip, no $ number shown.
- `liquidity_meta.confidence == 'multi_chain'` → `"Liquidity: $X (multi-chain)"` advisory text.
- `liquidity_meta.confidence == 'definite'` → `"Liquidity: $X"` plain.

Contract firewall (`scripts/check_todays_focus_contract.py`): extend `OPTIONAL_ROW_KEYS` to include `liquidity_meta`; extend `BANNED_PATTERNS` with `r"liquidity\s+(certain|guaranteed|verified)"` to block any UI text that might overclaim certainty.

### Pre-registered measurement substrate

Per guardrail #6 ("No ranking, filtering, alerting, sizing, or execution based on liquidity until coverage/accuracy is measured"), Phase 1 must produce:

- **Coverage metric (named denominator).** Coverage is measured on the **paper cohort denominator**, defined as:
  ```sql
  -- Denominator: candidates rows joined to paper_trades opened post-cutover
  SELECT c.* FROM candidates c
  INNER JOIN paper_trades p ON LOWER(c.contract_address) = LOWER(p.token_id)
  WHERE p.opened_at >= (SELECT cutover_ts FROM paper_migrations WHERE name='bl_new_liquidity_enrichment_v1')
  ```
  Numerator: same set WHERE `liquidity_enriched_confidence IN ('definite', 'multi_chain')`.
  Pre-register: **≥70%** before any downstream consumer is wired.

- **Accuracy spot-check (separate denominator, NOT the paper cohort).** The paper cohort is structurally CG-sourced with `liquidity_usd=0`, so paper-side overlap is ~0 by construction. Accuracy must be measured on the **broader candidates set** where the DEX-side ingest also writes:
  ```sql
  -- Denominator: candidates rows where BOTH writers populated > 0
  SELECT contract_address, liquidity_usd, liquidity_usd_enriched
  FROM candidates
  WHERE liquidity_usd > 0
    AND liquidity_usd_enriched > 0
    AND liquidity_enriched_confidence = 'definite'
  ```
  Numerator: same set WHERE `ABS(liquidity_usd_enriched - liquidity_usd) / liquidity_usd <= 0.20`.
  Pre-register: ≥20 samples; <80% within-band → halt before downstream consumer is wired.

- **Multi-chain rate:** % paper cohort rows with `confidence='multi_chain'`. Pre-register: track but no halt criterion; informational.

- **Unresolvable rate:** % paper cohort rows with `confidence IN ('cg_slug_unresolvable', 'dex_no_match')`. Pre-register: track; if >50%, surface as a design-revisit trigger (CG slug→platform mapping may be sparser than assumed).

**Measurement window (data-bound per §11):** 14 calendar days post-Phase-1-deploy OR n=100 paper cohort rows with enrichment writes, whichever comes first. Halt-the-soak if data-bound criterion fires earlier; do not run to calendar completion if data threshold met.

## Phased Build Sequence

The implementation is split into 3 PRs, gated by criteria:

### Phase 1a — Schema + Cron + Watchdog (single build PR)

Files:
- `scout/db.py` migration block: add 4 columns + migration marker.
- `scripts/backfill_dexscreener_liquidity.py`: cron writer.
- `scripts/check_liquidity_enrichment_lag.py`: watchdog.
- `scout/config.py`: new settings (`LIQUIDITY_ENRICHMENT_TTL_SEC`, `LIQUIDITY_ENRICHMENT_STALE_SEC`, `LIQUIDITY_BACKFILL_BATCH_MAX`, `LIQUIDITY_ENRICHMENT_ENABLED` killswitch).
- `tests/test_backfill_dexscreener_liquidity.py`: unit tests with `aioresponses` mocks for CG + DexScreener.
- `tests/test_check_liquidity_enrichment_lag.py`: watchdog tests.

NO dashboard changes in Phase 1a. NO systemd unit / crontab — operator manually runs the cron once for validation before scheduling. NO ranking/filtering/alerting/sizing consumers.

### Phase 1b — Dashboard read path

Files:
- `dashboard/db.py` `get_todays_focus`: select + coalesce + meta.
- `dashboard/api.py` Pydantic envelope: optional `liquidity_meta`.
- `dashboard/frontend/components/...`: render `unavailable` / `unverified` / `$X` / multi-chain.
- `scripts/check_todays_focus_contract.py`: extend `OPTIONAL_ROW_KEYS` + `BANNED_PATTERNS`.
- Tests: contract firewall, layout, banned-pattern static scan.

Phase 1b ships ONLY after Phase 1a is producing non-zero `liquidity_enriched_at` rows in prod for ≥24h.

### Phase 1c — Systemd timer / crontab + freshness gate

Files:
- `docs/deploy/liquidity_enrichment_cron.md`: runbook for cron + watchdog scheduling.
- Operator action: install systemd timer (or crontab entry); verify watchdog fires expected alert on test stale condition.

Phase 1c ships after Phase 1b dashboard surface is rendering the new fields correctly with manual cron runs.

### Phase 2 (NOT in this design)

Any downstream consumer of `liquidity_usd_enriched` — ranking, filtering, alerting, sizing, execution — is OUT OF SCOPE for Phase 1. Phase 2 requires:
1. Pre-registered measurement criteria above met.
2. A NEW design PR scoping the specific downstream consumer.
3. Operator approval, separate from Phase 1 approval.

This honors guardrail #6.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| DexScreener liquidity lookup | Hermes ecosystem has price/market skills but not a project-local DexScreener integration | Use existing `scout/ingestion/dexscreener.py` shape; in-tree. |
| CoinGecko `/coins/{id}` per-token resolution | Hermes ecosystem has CG skills but not slug→platform resolution | Use existing CG client + ratelimiter; in-tree. |
| Background cron / writer pattern | shift-agent has cron-style helpers; gecko-alpha has `scripts/check_*.py` watchdog pattern | Use existing in-tree pattern. |
| Freshness SLO + watchdog | per CLAUDE.md §12a memory + `scripts/check_dexscreener_lag.py` pattern | Use existing in-tree pattern. |

awesome-hermes-agent ecosystem check: no drop-in liquidity-enrichment skill or DexScreener-search backfill primitive that matches gecko-alpha's SQLite schema + ingest pattern. Verdict: in-tree implementation is justified.

## Drift Check (per CLAUDE.md §7a)

- `git fetch origin master && git log -20 origin/master`: master at `50fb570a` (PR #322 decision packet); no liquidity-enrichment branch in flight.
- No active branch implementing cron-backfill, schema migration, or dashboard liquidity surface.
- PR #310 (audit) + PR #311 (snapshot) + PR #322 (decision packet) are the only prior liquidity-side work; this design builds on them, no overlap.
- Hermes-side has no in-flight liquidity work per the prior packet's check.

## Anti-Scope (this PR)

This PR is design-only. The following are EXPLICITLY OUT OF SCOPE for this PR:

- No schema migration executed.
- No cron script written.
- No watchdog written.
- No dashboard read-path change.
- No systemd timer / crontab installed.
- No backlog status mutation on `BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS` (still PROPOSED — operator approves THIS design first; Phase 1a build is a separate PR).
- No downstream consumer (ranking, filtering, alerting, sizing, execution).
- No re-sourcing of paper signal pipeline from CG to DEX (multi-PR future-program).
- No `_PRESERVE_FIELDS` aggregator change (decoupled-columns design sidesteps this).
- No mutations to `candidates`, `paper_trades`, or any other runtime table.
- No CoinGecko paid-tier integration.

## Failure Modes Pre-Emptively Addressed

| Mode | Mitigation |
|---|---|
| CG ratelimit exhausted by backfill load | Bounded batch size (`LIQUIDITY_BACKFILL_BATCH_MAX=50`); shared rate-limiter coordinates with ingest. |
| DexScreener 429 during cron tick | Existing exponential backoff in `_get_json`; cron skips on failure, retries next tick, never clobbers `liquidity_enriched_at`. |
| Aggregator clobbers cron writes | Decoupled enrichment columns — aggregator never touches them. |
| Symbol-fuzzy resolution | DexScreener `/dex/search?q=<symbol>` is NEVER called in this design. Resolution is always CG-slug → platforms.address → DexScreener `/tokens/v1/{chain}/{address}`. |
| Stale enrichment displayed as fresh | `LIQUIDITY_ENRICHMENT_STALE_SEC` check in dashboard read path forces `confidence='stale'` regardless of stored value. |
| Multi-chain token displayed as single-chain | `confidence='multi_chain'` advisory in rendered UI. |
| Unresolvable CG slug (no `platforms`) | `confidence='cg_slug_unresolvable'` renders `Liquidity: unavailable`. |
| Dashboard render-time external call | Architecturally prohibited — dashboard reads DB only. Enforced by guardrail #1 + no DexScreener/CG client import in `dashboard/` code paths. |
| Killswitch needed mid-flight | `LIQUIDITY_ENRICHMENT_ENABLED=False` halts cron writes; existing rows preserved; dashboard renders `stale` after TTL passes; no rollback migration needed. Killswitch read at the START of each 15-min tick — no restart required. |
| Killswitch + watchdog interaction | Watchdog also reads `LIQUIDITY_ENRICHMENT_ENABLED`. If False, watchdog suppresses SLO-breach alerts (the cron is intentionally off). This prevents pager-fatigue during planned downtime. Operator MUST manually verify cron is off; the watchdog will not signal it. |
| Symbol-fuzzy regression introduced by future edit | Phase 1a build PR ships `tests/test_no_symbol_fuzzy_resolution.py`: a static grep test that fails if `scripts/backfill_dexscreener_liquidity.py` (or any cron-related path) contains `dex/search?q=`. Turns guardrail #3 into a runtime CI contract per the anti-scope-as-contract pattern (see `feedback_anti_scope_as_runtime_contract.md`). |
| §12b silent state-reversal pattern | Cron writes are not state reversals; no auto-suspend / auto-disable / kill-switch trip; no §12b alert wiring required. |

## Test Plan (for the Phase 1a/1b/1c build PRs, NOT this PR)

Phase 1a tests:
- Cron resolves CG-slug → DexScreener correctly on mocked CG `/coins/{id}` + DexScreener `/tokens/v1/...` responses.
- `dex:chain:address` prefix shortcut bypasses CG hop.
- `confidence='multi_chain'` written when CG returns >1 platform.
- `confidence='cg_slug_unresolvable'` written when CG returns empty `platforms`.
- `confidence='dex_no_match'` written when DexScreener returns no pair.
- 429 from CG or DexScreener does NOT clobber `liquidity_enriched_at`.
- `LIQUIDITY_ENRICHMENT_TTL_SEC` skip-fresh-rows logic.
- `LIQUIDITY_BACKFILL_BATCH_MAX` bound enforced.
- Killswitch `LIQUIDITY_ENRICHMENT_ENABLED=False` halts writes.

Phase 1b tests:
- Dashboard contract firewall accepts the new fields; rejects banned patterns.
- Layout tests: `unavailable` rendered when null; `unverified` when stale; `$X` when definite; `(multi-chain)` when multi-chain.
- Pydantic envelope: optional `liquidity_meta` does not break existing consumers.
- Static-scan tests: banned phrases (`liquidity certain`, `liquidity guaranteed`, `liquidity verified`) absent.

Phase 1c tests:
- Watchdog fires expected curl-direct TG alert on simulated stale condition.
- Watchdog does NOT fire on healthy state.

## Rollback

Phase 1a: drop the 4 enrichment columns; remove the migration marker row; stop the cron. Existing `liquidity_usd` write path is untouched, so paper_trades / scoring / ingest behavior continues unchanged.

Phase 1b: dashboard surface renders existing `liquidity_usd` only; remove the meta field from the Pydantic envelope.

Phase 1c: stop systemd timer / remove crontab entry; cron stops writing.

Killswitch (no rollback needed): set `LIQUIDITY_ENRICHMENT_ENABLED=False` in `.env`; restart cron service. Existing rows are preserved; new writes stop; dashboard renders existing rows as `stale` after TTL.

## Operator Approval Surface

The operator approves THIS DESIGN — that authorizes Phase 1a's build PR to be scoped against `tasks/plan_liquidity_enrichment_b2_phase_1a.md` (a separate, future PR). Each subsequent phase requires its own approval after the prior phase's success criteria are met:

- **Phase 1a → 1b gate:** ≥24h of successful cron writes; ≥50 non-zero `liquidity_enriched_at` rows; watchdog has not breached SLO during the window. (≥50 — not ≥1 — so the dashboard contract test has real data to render against across all 5 confidence states.)
- **Phase 1b → 1c gate:** Phase 1b dashboard surface rendering all 5 confidence states correctly on prod data.
- **Phase 1c → Phase 2 gate:** ≥70% definite/multi-chain coverage on the post-deploy paper cohort over 14 days OR n=100 rows; accuracy spot-check passes ±20% on ≥80% of overlap samples.

This PR stops at design approval. No build handoff, no schedule commitment, no implementation authorization beyond the Phase 1a scoping.

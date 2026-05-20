**New primitives introduced:** new SQLite table `source_calls` (per-call durable outcome ledger keyed by `(source_type, source_event_id)` — sidecar to existing `tg_social_signals` and `narrative_alerts_inbound`); new `scout/source_quality/` module with backfill + summary helpers; new `Database._migrate_source_calls_v1` migration helper; new read-only source-call lag watchdog script/check for §12a. NO new HTTP endpoint, NO dashboard read-model in this PR (read-model deferred to a follow-up).

# Plan: BL-NEW-SOURCE-CALL-OUTCOME-LEDGER

## Hermes-first analysis (mandatory gate per `docs/gecko-alpha-alignment.md` §Hermes-first analysis convention)

**Inputs checked:**
- Installed VPS Hermes skills (verified via SSH 2026-05-20T14:50Z): stock `social-media/xurl` (xdevplatform CLI), `research/{arxiv,blogwatcher,llm-wiki,polymarket,research-paper-writing}`, `data-science/jupyter-live-kernel`, ~22 other stock categories. First-party gecko-alpha-owned skills: `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, `coin_resolver`, `crypto_narrative_scanner` (all status=DRAFT, all under `/home/gecko-agent/.hermes/skills/`).
- `/home/gecko-agent/.hermes/cron/jobs.json`: 1 job (`gecko-x-narrative-scanner`).
- Public ecosystem fresh check (2026-05-20T15:30Z): CoinGecko Agent SKILL (endpoint/parameter reference only — `https://docs.coingecko.com/docs/skills`), GoldRush/Covalent Hermes integration and docs (wallet/holder/transfer/pricing plus OHLCV pair query — `https://goldrush.dev/agents/hermes-agent/`, `https://goldrush.dev/docs/api-reference/streaming-api/queries/ohlcv-pairs-query/`), HermesHub community registry (`relay-for-telegram`, `data-analyst`, `scrapling` — `https://github.com/amanning3390/hermeshub`), and awesome-hermes-agent (`agent-analytics-hermes-plugin` is a Hermes activity dashboard, not gecko-alpha source-call outcome attribution).
- Existing gecko-alpha docs: `tasks/research_hermes_crypto_skills_2026_05_14.md`, `tasks/findings_hermes_first_debt_audit_2026_05.md`, `docs/gecko-alpha-alignment.md`.
- Existing backlog: BL-032 (X/social roadmap), TG/X outcome linkage design (PR #184).

**Required-format table:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| X/KOL collection | YES — stock `social-media/xurl` + first-party `kol_watcher` orchestrated by `crypto_narrative_scanner` cron, already operational (last 5 cycles 34-101s, jobs.json `last_status=ok`, 355 rows in `narrative_alerts_inbound`) | **USE_HERMES** — no rebuild; gecko-alpha already consumes via HMAC POST → `narrative_alerts_inbound` |
| TG source ingestion | NO stock skill (HermesHub has `relay-for-telegram` but it's outbound notification, not curated-channel reader). gecko-alpha-owned `scout/social/telegram/listener.py` + `tg_social_messages` + `tg_social_signals` (842 rows) is the production listener | **KEEP_CUSTOM** — durable Telethon listener + DB tables stay; no Hermes substitute |
| source-call outcome attribution | NO Hermes skill exists for "tie source-of-call to forward outcome with paper-trade linkage." Stock skills cover ingestion/classification only; outcome attribution is a domain-specific persistence concern. | **KEEP_CUSTOM** — this is the NEW primitive proposed in this PR |
| source-quality analytics | NO Hermes skill for per-source aggregation against gecko-alpha forward returns. `data-analyst` (HermesHub) is generic. CoinGecko Agent SKILL is endpoint-knowledge only. GoldRush can provide historical OHLCV for token pairs but does not map TG/X source events to gecko-alpha trades or maintain the operator's source-quality ledger. | **KEEP_CUSTOM** for ledger/summary; **DEFER / USE_AS_REFERENCE** for GoldRush historical-price expansion after API-key/cost and pair-mapping review |
| X cost governance | NO — `social-media/xurl` is the API CLI, no built-in cost-governor. | **KEEP_CUSTOM** (DEFER implementation) — file `BL-NEW-X-KOL-COST-GOVERNOR` design-only follow-up |
| dashboard/reporting | NO Hermes dashboard primitive. gecko-alpha already has `dashboard/frontend/components/XAlertsTab.jsx` + `TGAlertsTab.jsx` + `TGDLQPanel.jsx`; new source-quality view EXTENDS these, doesn't duplicate. | **KEEP_CUSTOM** (DEFER implementation) — summary helper/report in this PR; HTTP endpoint + frontend tab in a follow-up |

**awesome-hermes-agent ecosystem check** (per AGENTS.md): fresh 2026-05-20 check found no source-quality / outcome-attribution / KOL-scoring skill for gecko-alpha. `agent-analytics-hermes-plugin` is read-only Hermes multi-project analytics, not a crypto source-call outcome ledger. Gemwatch-like commercial products prove the problem class exists but are not Hermes skills and would not preserve gecko-alpha's durable DB/audit boundary.

**Verdict summary:**
- The PR adds NO duplicate of Hermes-owned work. The X/KOL collection path is preserved as-is.
- The new `source_calls` table is the canonical attribution layer that no Hermes skill provides.
- gecko-alpha-owned `kol_watcher`/`narrative_classifier`/etc. ARE NOT stock Hermes-ecosystem adoption — they are gecko-alpha first-party skills that happen to run on the Hermes runtime (per calibration in `memory/feedback_hermes_diagnosis_discipline.md`). Counting them toward "Hermes utilization" would be over-claiming.

## Drift-check evidence (per §7a)

### Schemas (verified via SSH 2026-05-20T14:50Z)

| Table | Rows | Key columns | FK to paper_trades? |
|---|---|---|---|
| `tg_social_messages` | (raw msgs, unique on channel+msg_id) | `channel_handle, msg_id, posted_at, sender, text, cashtags, contracts, urls` | no (parent of tg_social_signals) |
| `tg_social_signals` | **842** | `message_pk` (FK msg), `token_id, symbol, contract_address, chain, mcap_at_sighting, resolution_state, source_channel_handle, alert_sent_at, paper_trade_id, created_at` | **YES** — already FK |
| `narrative_alerts_inbound` | **355** | `event_id` (unique), `tweet_id, tweet_author, tweet_ts, tweet_text, tweet_text_hash, extracted_cashtag, extracted_ca, extracted_chain, resolved_coin_id, narrative_theme, urgency_signal, classifier_confidence, classifier_version, received_at` | NO |
| `paper_trades` | 1582 | (existing) | (target) |
| `paper_trade_entry_snapshots` | 12 | (new sidecar from PR #200) | FK |

### Existing linkage gap

- TG: **6 of 842 signals** (0.7%) have `paper_trade_id IS NOT NULL`. The vast majority of TG calls never become paper trades — exactly the operator's "noise" problem.
- X: **0 of 355** narrative_alerts_inbound rows have `paper_trade_id` linkage at all (the table has no `paper_trade_id` column). Forward-trace from X-call → trade is undocumented.

### Existing source list (live data)

**TG channels** (top by call count): `@cryptoyeezuscalls` 223, `@nebukadnaza` 174, `@Alt_Crypto_Gems` 166, `@detecter_calls` 140, `@thanos_mind` 76, `@alohcooks` 63.

**X authors** (top by call count): `gem_insider` 91, `_Shadow36` 58, `blknoiz06` 38, `xbtDLN` 28, `CrashiusClay69` 28, `DegenerateNews` 25, `KingBoyDarling` 24, plus ~6 others.

### Forward-price source candidates

- **`price_cache` (8459 rows)**: current snapshot only, NO time series — can supply `price_at_call` for currently-known tokens but cannot produce forward windows.
- **`gainers_snapshots` (49,491 rows, 212 unique tokens)**: time-series via `snapshot_at` + `price_at_snapshot`. Time-series ✓ but coverage limited to tokens that hit CG gainers boards (typically post-pump). Can produce forward windows for high-visibility tokens.
- **`losers_snapshots`**: same price-bearing shape as gainers (`snapshot_at` + `price_at_snapshot`), different cohort.
- **`trending_snapshots`**: NO price column. Do not use for price or forward-return windows; it can only contribute non-price coverage context.
- **`score_history`**: only has `score`, not price.

**Verdict on forward-price:** best-effort from `gainers_snapshots`+`losers_snapshots` only via `(coin_id, snapshot_at, price_at_snapshot)` lookup; mark price fields missing when no bounded time-series exists for the token. **Do not silently rank on covered rows only:** every source-quality summary must show all-call count, resolvable coverage rate, unresolvable rate, eligible distinct-cluster count, and duplicate rate. Forward-return ranking is labeled `resolvable_cg_board_cohort` unless/until a broader historical-price source such as GoldRush OHLCV is explicitly added.

### Existing partial dashboards

- `dashboard/frontend/components/XAlertsTab.jsx` (reads `get_x_alerts`)
- `dashboard/frontend/components/TGAlertsTab.jsx` (reads `get_tg_social_alerts`)
- `dashboard/frontend/components/TGDLQPanel.jsx`
- `dashboard/db.py:get_tg_social_per_channel_cashtag_today` (already per-channel — would extend)

**Decision:** EXTEND existing dashboards later; do NOT duplicate. This PR ships the ledger + summary helper/report only. HTTP endpoint and frontend changes are follow-ups.

## Primitive shape (proposed)

```sql
CREATE TABLE source_calls (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type              TEXT NOT NULL CHECK (source_type IN ('tg', 'x')),
    source_id                TEXT NOT NULL,       -- TG channel handle OR X handle
    source_event_id          TEXT NOT NULL,       -- tg_social_signals.id::text OR narrative_alerts_inbound.event_id
    token_id                 TEXT,                -- coin_id; nullable until resolved
    symbol                   TEXT,
    contract_address         TEXT,
    chain                    TEXT,
    call_ts                  TEXT NOT NULL,       -- ISO-8601; the source's posted_at / tweet_ts (NOT received_at)
    call_kind                TEXT NOT NULL CHECK (call_kind IN ('first_mention','repeat_mention','ca_call','cashtag_only','unknown')),
    cluster_identity         TEXT NOT NULL,
    cluster_identity_kind    TEXT NOT NULL CHECK (cluster_identity_kind IN ('token_id','contract','symbol','source_event')),
    duplicate_cluster_key    TEXT NOT NULL,       -- sha256(source_type|source_id|cluster_identity|date_bucket) for cluster grouping
    duplicate_rank_in_cluster INTEGER NOT NULL DEFAULT 1,
    resolved_state           TEXT NOT NULL,       -- mirrors upstream resolution_state / resolved_coin_id presence
    price_at_call            REAL,
    price_at_call_snapshot_at TEXT,
    price_source             TEXT,
    price_age_sec            INTEGER,
    mcap_at_call             REAL,
    forward_30m_pct          REAL,
    forward_1h_pct           REAL,
    forward_6h_pct           REAL,
    forward_24h_pct          REAL,
    max_favorable_pct_24h    REAL,
    max_adverse_pct_24h      REAL,
    time_to_peak_min         REAL,
    linked_paper_trade_id    INTEGER,
    linkage_method           TEXT NOT NULL DEFAULT 'none'
        CHECK (linkage_method IN ('none','direct_tg','heuristic_x')),
    linkage_confidence       TEXT NOT NULL DEFAULT 'none'
        CHECK (linkage_confidence IN ('none','direct','heuristic','conflict')),
    outcome_status           TEXT NOT NULL CHECK (outcome_status IN ('pending','partial','complete','unresolvable')),
    missing_fields           TEXT NOT NULL
        CHECK (json_valid(missing_fields) AND json_type(missing_fields) = 'array'),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (linked_paper_trade_id) REFERENCES paper_trades(id) ON DELETE RESTRICT,
    UNIQUE (source_type, source_event_id)
);
CREATE INDEX idx_source_calls_source_ts ON source_calls(source_type, source_id, call_ts);
CREATE INDEX idx_source_calls_token_ts ON source_calls(token_id, call_ts);
CREATE INDEX idx_source_calls_cluster ON source_calls(duplicate_cluster_key);
CREATE INDEX idx_source_calls_outcome ON source_calls(outcome_status, call_ts);
```

### Coverage contract (per `feedback_coverage_contract_verification_internal_consistency.md`)

- `outcome_status='pending'` ⇒ `missing_fields` lists every forward field
- `outcome_status='partial'` ⇒ `missing_fields` lists the subset still unresolvable
- `outcome_status='complete'` ⇒ `missing_fields='[]'` AND all forward fields populated
- `outcome_status='unresolvable'` ⇒ `missing_fields` lists ALL forward fields with "no_time_series" tag; row is filed but won't be re-attempted

### Idempotency

- `UNIQUE(source_type, source_event_id)` PK enforces 1 row per upstream event.
- Backfill uses `INSERT ... ON CONFLICT(source_type, source_event_id) DO UPDATE SET ...` so mutable source-derived fields can refresh while preserving the original `created_at`.
- Re-running backfill is safe; the latest forward-window resolution wins (via `updated_at`).

### Duplicate clustering

`cluster_identity` is computed using this fallback order:

1. `token_id` when present (`cluster_identity_kind='token_id'`)
2. `chain || '|' || contract_address` when contract is present (`cluster_identity_kind='contract'`)
3. normalized `symbol` when symbol/cashtag is present (`cluster_identity_kind='symbol'`)
4. `source_event_id` when no token identity exists (`cluster_identity_kind='source_event'`)

`duplicate_cluster_key = sha256(source_type || '|' || source_id || '|' || cluster_identity || '|' || date_bucket_utc)`
where `date_bucket_utc = strftime('%Y-%m-%d', call_ts)`.

`duplicate_rank_in_cluster` is assigned by `ROW_NUMBER() OVER (PARTITION BY duplicate_cluster_key ORDER BY call_ts)` at backfill/update time.

**Why this shape:** prevents "channel X mentioned token Y five times in one day" from looking like 5 conviction signals. Source-quality summaries default to distinct eligible clusters (`duplicate_rank_in_cluster = 1`), not raw calls. Raw calls and duplicate-rate are still reported separately so spam remains visible.

### No future-state leakage discipline

When computing `price_at_call`, `mcap_at_call`, and forward windows:

0. Normalize all timestamps through Python UTC datetime parsing before comparisons. Do not rely on SQLite lexical ordering across `...Z`, `+00:00`, and `YYYY-MM-DD HH:MM:SS` formats.
1. Read `gainers_snapshots`/`losers_snapshots` rows with `snapshot_at <= call_ts` for the at-call value (use most-recent ≤ call_ts) with horizon-specific freshness:
   - 30m metric: at-call price age <= 15m
   - 1h metric: at-call price age <= 30m
   - 6h/24h/extrema metrics: at-call price age <= 60m
   Store `price_at_call_snapshot_at`, `price_source`, and `price_age_sec`; otherwise suppress the affected metric(s).
2. Read snapshots within bounded forward windows:
   - 30m: `[T+30m, T+45m]`
   - 1h: `[T+1h, T+90m]`
   - 6h: `[T+6h, T+7h]`
   - 24h: `[T+24h, T+28h]`
   If no row falls inside the window, that field is missing.
3. Compute `max_favorable_pct_24h`, `max_adverse_pct_24h`, and `time_to_peak_min` ONLY from snapshots in `[T, T+24h]`. Rows after `T+24h` are not eligible for 24h extrema.
4. NEVER read `price_cache.updated_at > call_ts` for at-call values. Prefer not to use `price_cache` at all for this PR because it is current-state, not historical.
5. `narrative_alerts_inbound.classifier_confidence` and `tg_social_signals.mcap_at_sighting` ARE valid at-call values (they were stamped at upstream-event time).

### Linked-paper-trade strategy

- TG: `tg_social_signals.paper_trade_id` already exists → copy directly into `source_calls.linked_paper_trade_id` with `linkage_method='direct_tg'` and `linkage_confidence='direct'`.
- X: NO existing column. Use temporal-windowed correlation: a paper_trade is linked to an X call if `paper_trades.token_id == narrative_alerts_inbound.resolved_coin_id` (when resolved) AND `narrative_alerts_inbound.received_at <= paper_trades.opened_at <= narrative_alerts_inbound.received_at + 1h`. Exactly one match writes `linked_paper_trade_id` and marks `linkage_method='heuristic_x'`, `linkage_confidence='heuristic'`. Multiple matches write no concrete paper-trade id, mark `linkage_confidence='conflict'`, and expose `linkage_conflict_count`. Discovery-quality forward windows still use `call_ts=tweet_ts`; strategy linkage uses `received_at` because gecko-alpha cannot act before receiving the alert.
- Do NOT denormalize paper-trade PnL into `source_calls`. Summary helpers join `paper_trades.pnl_usd` at read time so paper-trade close writers stay unchanged.

### Backfill strategy

1. **TG path**: read `tg_social_signals` (842 rows) + join `tg_social_messages` for `posted_at`. Insert one `source_calls` row per `tg_social_signals.id`. Use `source_id = source_channel_handle`, `source_event_id = tg_social_signals.id::text`, `call_ts = tg_social_messages.posted_at`.
2. **X path**: read `narrative_alerts_inbound` (355 rows). Insert one `source_calls` row per `event_id`. Use `source_id = tweet_author`, `source_event_id = event_id`, `call_ts = tweet_ts`.
3. **Forward-window pass**: for each row, query snapshot tables; populate forward fields where possible; set `outcome_status` accordingly.
4. **Paper-trade linkage pass**: for TG copy `paper_trade_id`; for X run temporal correlation.

Backfill is rerunnable. Each pass uses SQLite UPSERT for new rows + refresh.

### Low-n discipline

Source-quality summaries that group by `source_id` MUST suppress ranking unless both gates pass: at least 10 **eligible distinct clusters** and a configured minimum resolvable coverage rate (default 0.50). Raw call count alone is not enough because repeat spam can inflate sample size. Summaries must expose:

- raw call count
- distinct cluster count
- eligible distinct cluster count (clusters with metric-present rows)
- duplicate rate
- resolvable coverage rate
- unresolvable rate
- `rank_status` (`insufficient_sample`, `biased_low_coverage`, `rankable_resolvable_cg_board_cohort`)

Aligns with `memory/feedback_n_gate_verdicts_against_dashboard_noise.md`.

### NO trading-PnL contamination

Linked paper-trade PnL reflects gecko-alpha's strategy outcome on a trade that WAS opened — NOT the source-call discovery quality. It is read by joining `source_calls.linked_paper_trade_id` to `paper_trades.pnl_usd`, not denormalized into `source_calls`. **Source-quality reports MUST distinguish:**
- Discovery quality: did the source surface a token that pumped forward? (forward_*_pct fields)
- Strategy quality: did gecko-alpha's gate make money on the call's token? (`paper_trades.pnl_usd` joined at read time)

A source can be high-quality at discovery (great early calls) but the gate may still reject them; conversely a source can be low-quality at discovery but happen to overlap winning trades. The two metrics stay separate by design.

## Watchdog / SLO (§12a per global CLAUDE.md)

`source_calls` is a new pipeline table. Per §12a: every new pipeline table MUST ship with a freshness SLO + watchdog. This PR includes the check, not a deferred placeholder.

1. **Per-pass freshness counter**: backfill helper emits `source_calls_backfill_summary` with `inserted`, `updated`, `tg_seen`, and `x_seen`. Unledgered lag is reported by the dedicated watchdog.
2. **Lag watchdog**: add `scripts/check_source_calls_lag.py` returning JSON plus `scripts/source-calls-lag-watchdog.sh` wrapper. It exits non-zero if any upstream `tg_social_signals` / `narrative_alerts_inbound` row older than 30 minutes is missing a corresponding `source_calls` row. This monitors upstream-to-ledger lag, not `MAX(updated_at)` alone, so quiet periods do not false-alert.
3. **SLO**: when source-call backfill is scheduled, ledger lag must be under 30 minutes for all upstream rows. Initial PR does not schedule it; the runbook states "not scheduled" explicitly and the watchdog is operator-runnable before scheduling.

## Acceptance criteria (pre-registered)

1. **Migration idempotent** — `Database._migrate_source_calls_v1()` runs twice without error or duplicate sentinels.
2. **CHECK constraints enforced** — INSERT with invalid `source_type` or `outcome_status` raises sqlite3.IntegrityError.
3. **TG backfill from `tg_social_signals`** — synthetic seeded rows are inserted exactly once; `posted_at` populates `call_ts`; `source_id`/`source_event_id` are correct. Live row counts are smoke evidence only, not unit-test fixtures.
4. **X backfill from `narrative_alerts_inbound`** — synthetic seeded rows are inserted exactly once; `tweet_ts` populates `call_ts`; `received_at` is used only for strategy-linkage eligibility. Live row counts are smoke evidence only.
5. **`UNIQUE(source_type, source_event_id)` enforced** — rerunning backfill does not create duplicates and refreshes mutable fields via UPSERT.
6. **Duplicate cluster key correctness** — same (source, cluster_identity, date_utc) → same cluster_key; unresolved rows use the documented fallback identity; `duplicate_rank_in_cluster` monotonically assigned by call_ts.
7. **Forward-window leakage prevention** — for a synthetic row with `call_ts=T`, tests verify: mixed timestamp formats parse to the same UTC timeline; pre-window rows (`T` to `T+30m`) do not satisfy `forward_30m`; too-late rows outside tolerance do not satisfy the window; 24h extrema ignore rows after `T+24h`; horizon-specific stale at-call prices suppress affected metrics.
8. **Paper-trade linkage — TG path** — `source_calls.linked_paper_trade_id` matches `tg_social_signals.paper_trade_id` 1:1 post-backfill.
9. **Paper-trade linkage — X path heuristic** — temporal correlation matches a known paper-trade where applicable; row is unlinked when no candidate paper_trade exists.
10. **Coverage contract** — `outcome_status='complete'` implies `missing_fields='[]'`; `outcome_status` in {'pending','partial','unresolvable'} implies non-empty `missing_fields`.
11. **No regression** — `tg_social_signals` / `narrative_alerts_inbound` / `paper_trades` writers unchanged; all adjacent tests pass.
12. **Low-n summary helper** — `compute_source_quality_summary(min_sample=10)` excludes sources with <10 eligible distinct clusters, not <10 raw calls.
13. **No future leakage / no survivorship hiding in summary** — summary computes forward metrics only from rows with metric-present outcomes, but always reports all-call denominator, resolvable coverage rate, unresolvable rate, and duplicate rate. Rankings are disabled unless sample and coverage gates pass; any ranking is labeled `rankable_resolvable_cg_board_cohort` until broader historical pricing exists.
14. **§12a lag watchdog** — seeded upstream rows older than threshold with no ledger row make the watchdog fail; matching ledger rows make it pass; quiet periods with no upstream rows do not fail.
15. **Migration sentinel uniqueness** — migration uses `paper_migrations.name='bl_source_calls_v1'` plus `schema_version.version=20260522` (or next available version verified at build time); running twice creates exactly one sentinel in each table. Rollback deletes by the version primary key, not free-text description.

## Test plan

`tests/test_source_call_outcome_ledger.py` covers the pre-registered criteria above. Migration test uses tmp_path Database fixture (same pattern as `tests/test_entry_snapshot.py`).

## Rollback

| Edit | Procedure |
|---|---|
| Migration | `DROP TABLE source_calls` + `DELETE FROM paper_migrations WHERE name='bl_source_calls_v1'` + `DELETE FROM schema_version WHERE version=20260522` (or the final build-time version). Idempotent. |
| Backfill | No-op if not run; if run, `DROP TABLE source_calls` is the only state-reset path (idempotent + safe — no other writer depends on it yet) |

## Reviewer focus (P1.4)

Two vectors, plus one mandatory Hermes-first-adequacy lens (per operator's gate):

- **Vector A — Data-leakage / statistical validity (Hermes-first-adequacy folded in):** Bounded forward windows? No sparse-window leakage? Coverage/unresolvable denominator visible? Low-n threshold based on distinct eligible clusters? Duplicate clustering correct? Does this avoid converting spam into false confidence? **AND: was Hermes-first done honestly?** (Fresh public check? Installed-vs-stock distinction? Durable DB vs ephemeral memory? GoldRush historical-pricing boundary?)
- **Vector B — Structural / schema / runtime:** Schema normalization; idempotent writes; backfill rerunnable; no break to TG/X/paper-trade flows; CHECK constraints exhaustive; FK semantics correct; §12a lag watchdog included; migration sentinel unique; coverage contract internal-consistency holds.

## Open questions for reviewers

1. Is the X-side temporal-correlation heuristic (1h window post-`received_at`) the right fallback for paper-trade linkage, or should it be tighter/wider?
2. Should `duplicate_cluster_key` use `date_bucket_utc` (1-day window) or a tighter `hour_bucket`? Day is operator-readable; hour catches faster repeats. Plan default: day bucket, with duplicate rate surfaced.
3. Should the migration also add a `coingecko_meme_score`-style operator-pinned weight column for future per-source policy use, or strictly stay measurement-only? Recommendation: strictly measurement-only.
4. Should `scout/source_quality/` summary module stay in this PR? Recommendation: yes, because the table without a low-n/coverage-aware summary risks misuse.

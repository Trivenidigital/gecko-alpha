**New primitives introduced:** new SQLite table `source_calls` (per-call durable outcome ledger keyed by `(source_type, source_event_id)` — sidecar to existing `tg_social_signals` and `narrative_alerts_inbound`); new `scout/source_quality/` module with backfill + summary helpers; new `Database._migrate_source_calls_v1` migration helper; freshness watchdog row in the existing watchdog infra (§12a). NO new HTTP endpoint, NO dashboard read-model in this PR (read-model deferred to a follow-up).

# Plan: BL-NEW-SOURCE-CALL-OUTCOME-LEDGER

## Hermes-first analysis (mandatory gate per `docs/gecko-alpha-alignment.md` §Hermes-first analysis convention)

**Inputs checked:**
- Installed VPS Hermes skills (verified via SSH 2026-05-20T14:50Z): stock `social-media/xurl` (xdevplatform CLI), `research/{arxiv,blogwatcher,llm-wiki,polymarket,research-paper-writing}`, `data-science/jupyter-live-kernel`, ~22 other stock categories. First-party gecko-alpha-owned skills: `kol_watcher`, `narrative_classifier`, `narrative_alert_dispatcher`, `coin_resolver`, `crypto_narrative_scanner` (all status=DRAFT, all under `/home/gecko-agent/.hermes/skills/`).
- `/home/gecko-agent/.hermes/cron/jobs.json`: 1 job (`gecko-x-narrative-scanner`).
- Public ecosystem: CoinGecko Agent SKILL (endpoint/parameter reference only — `https://docs.coingecko.com/docs/skills`), GoldRush/Covalent Hermes integration (wallet/holder/transfer/pricing — `https://goldrush.dev/agents/hermes-agent/`), HermesHub community registry (`relay-for-telegram`, `data-analyst`, `scrapling` — `https://github.com/amanning3390/hermeshub`).
- Existing gecko-alpha docs: `tasks/research_hermes_crypto_skills_2026_05_14.md`, `tasks/findings_hermes_first_debt_audit_2026_05.md`, `docs/gecko-alpha-alignment.md`.
- Existing backlog: BL-032 (X/social roadmap), TG/X outcome linkage design (PR #184).

**Required-format table:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| X/KOL collection | YES — stock `social-media/xurl` + first-party `kol_watcher` orchestrated by `crypto_narrative_scanner` cron, already operational (last 5 cycles 34-101s, jobs.json `last_status=ok`, 355 rows in `narrative_alerts_inbound`) | **USE_HERMES** — no rebuild; gecko-alpha already consumes via HMAC POST → `narrative_alerts_inbound` |
| TG source ingestion | NO stock skill (HermesHub has `relay-for-telegram` but it's outbound notification, not curated-channel reader). gecko-alpha-owned `scout/social/telegram/listener.py` + `tg_social_messages` + `tg_social_signals` (842 rows) is the production listener | **KEEP_CUSTOM** — durable Telethon listener + DB tables stay; no Hermes substitute |
| source-call outcome attribution | NO Hermes skill exists for "tie source-of-call to forward outcome with paper-trade linkage." Stock skills cover ingestion/classification only; outcome attribution is a domain-specific persistence concern. | **KEEP_CUSTOM** — this is the NEW primitive proposed in this PR |
| source-quality analytics | NO Hermes skill for per-source aggregation against forward returns. `data-analyst` (HermesHub) is generic. CoinGecko Agent SKILL is endpoint-knowledge only. | **KEEP_CUSTOM** — summary helper in `scout/source_quality/` reads the ledger; no Hermes replacement |
| X cost governance | NO — `social-media/xurl` is the API CLI, no built-in cost-governor. | **KEEP_CUSTOM** (DEFER implementation) — file `BL-NEW-X-KOL-COST-GOVERNOR` design-only follow-up |
| dashboard/reporting | NO Hermes dashboard primitive. gecko-alpha already has `dashboard/frontend/components/XAlertsTab.jsx` + `TGAlertsTab.jsx` + `TGDLQPanel.jsx`; new source-quality view EXTENDS these, doesn't duplicate. | **KEEP_CUSTOM** (DEFER implementation) — backend summary endpoint in this PR; frontend tab in a follow-up |

**awesome-hermes-agent ecosystem check** (per AGENTS.md): no source-quality / outcome-attribution / KOL-scoring skill exists in the published list (verified by reading `tasks/findings_hermes_first_debt_audit_2026_05.md` lines 52-58 — HermesHub + PRB agent-skills lists already audited).

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
- **`losers_snapshots`, `trending_snapshots`**: similar shape, different cohort.
- **`score_history`**: only has `score`, not price.

**Verdict on forward-price:** best-effort from `gainers_snapshots`+`losers_snapshots`+`trending_snapshots` via `(coin_id, snapshot_at)` lookup; mark `outcome_status='unresolvable'` when no time-series exists for the token. **Explicit point-in-time discipline:** when computing `price_at_call`, only read snapshots with `snapshot_at <= call_ts`; for forward windows, only read snapshots with `snapshot_at >= call_ts + window`.

### Existing partial dashboards

- `dashboard/frontend/components/XAlertsTab.jsx` (reads `get_x_alerts`)
- `dashboard/frontend/components/TGAlertsTab.jsx` (reads `get_tg_social_alerts`)
- `dashboard/frontend/components/TGDLQPanel.jsx`
- `dashboard/db.py:get_tg_social_per_channel_cashtag_today` (already per-channel — would extend)

**Decision:** EXTEND existing dashboards; do NOT duplicate. Backend summary endpoint in this PR; frontend changes follow-up.

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
    duplicate_cluster_key    TEXT,                -- sha256(source_type|source_id|coin_id|date_bucket) for cluster grouping
    duplicate_rank_in_cluster INTEGER NOT NULL DEFAULT 1,
    resolved_state           TEXT NOT NULL,       -- mirrors upstream resolution_state / resolved_coin_id presence
    price_at_call            REAL,
    mcap_at_call             REAL,
    forward_30m_pct          REAL,
    forward_1h_pct           REAL,
    forward_6h_pct           REAL,
    forward_24h_pct          REAL,
    max_favorable_pct_24h    REAL,
    max_adverse_pct_24h      REAL,
    time_to_peak_min         REAL,
    linked_paper_trade_id    INTEGER,
    linked_paper_pnl_usd     REAL,
    outcome_status           TEXT NOT NULL CHECK (outcome_status IN ('pending','partial','complete','unresolvable')),
    missing_fields           TEXT NOT NULL,       -- JSON array; '[]' when complete; coverage contract per memory/feedback_coverage_contract_verification_internal_consistency.md
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
- Backfill uses `INSERT OR IGNORE` for the row-creation pass, then `UPDATE` to refresh outcomes.
- Re-running backfill is safe; the latest forward-window resolution wins (via `updated_at`).

### Duplicate clustering

`duplicate_cluster_key = sha256(source_type || '|' || source_id || '|' || coin_id || '|' || date_bucket_utc)`
where `date_bucket_utc = strftime('%Y-%m-%d', call_ts)`.

`duplicate_rank_in_cluster` is assigned by `ROW_NUMBER() OVER (PARTITION BY duplicate_cluster_key ORDER BY call_ts)` at backfill/update time.

**Why this shape:** prevents "channel X mentioned token Y five times in one day" from looking like 5 conviction signals. Source-quality summaries can choose to count `duplicate_rank_in_cluster = 1` only ("first mention per day per channel per coin"), or count all with explicit duplicate-rate disclosure.

### No future-state leakage discipline

When computing `price_at_call`, `mcap_at_call`, and forward windows:

1. Read `gainers_snapshots`/`losers_snapshots`/`trending_snapshots` rows with `snapshot_at <= call_ts` for the at-call value (use most-recent ≤ call_ts).
2. Read snapshots with `snapshot_at >= call_ts + window` for each forward window (use first ≥ call_ts + window).
3. NEVER read `price_cache.updated_at > call_ts` for at-call values.
4. `narrative_alerts_inbound.classifier_confidence` and `tg_social_signals.mcap_at_sighting` ARE valid at-call values (they were stamped at upstream-event time).

### Linked-paper-trade strategy

- TG: `tg_social_signals.paper_trade_id` already exists → copy directly into `source_calls.linked_paper_trade_id`.
- X: NO existing column. Use temporal-windowed correlation: a paper_trade is linked to an X call if `paper_trades.token_id == narrative_alerts_inbound.resolved_coin_id` (when resolved) AND `paper_trades.opened_at` is within 1h after `narrative_alerts_inbound.received_at`. **First match wins** (linked_paper_trade_id = MIN(paper_trades.id) satisfying both criteria). This is heuristic; flag as known-imperfect.
- `linked_paper_pnl_usd` updates when the trade closes (read `paper_trades.pnl_usd`).

### Backfill strategy

1. **TG path**: read `tg_social_signals` (842 rows) + join `tg_social_messages` for `posted_at`. Insert one `source_calls` row per `tg_social_signals.id`. Use `source_id = source_channel_handle`, `source_event_id = tg_social_signals.id::text`, `call_ts = tg_social_messages.posted_at`.
2. **X path**: read `narrative_alerts_inbound` (355 rows). Insert one `source_calls` row per `event_id`. Use `source_id = tweet_author`, `source_event_id = event_id`, `call_ts = tweet_ts`.
3. **Forward-window pass**: for each row, query snapshot tables; populate forward fields where possible; set `outcome_status` accordingly.
4. **Paper-trade linkage pass**: for TG copy `paper_trade_id`; for X run temporal correlation.

Backfill is rerunnable. Each pass uses `INSERT OR IGNORE` for new rows + `UPDATE` for refresh.

### Low-n discipline

Source-quality summaries that group by `source_id` MUST suppress (or visually-tag) any source with `total_calls < 10` as "insufficient sample." This is enforced at the summary-helper level, not at the table level. Aligns with `memory/feedback_n_gate_verdicts_against_dashboard_noise.md`.

### NO trading-PnL contamination

The `linked_paper_pnl_usd` field reflects gecko-alpha's strategy outcome on a trade that WAS opened — NOT the source-call discovery quality. **Source-quality reports MUST distinguish:**
- Discovery quality: did the source surface a token that pumped forward? (forward_*_pct fields)
- Strategy quality: did gecko-alpha's gate make money on the call's token? (linked_paper_pnl_usd)

A source can be high-quality at discovery (great early calls) but the gate may still reject them; conversely a source can be low-quality at discovery but happen to overlap winning trades. The two metrics stay separate by design.

## Watchdog / SLO (§12a per global CLAUDE.md)

`source_calls` is a new pipeline table. Per §12a: every new pipeline table MUST ship with a freshness SLO + watchdog. Two-part:

1. **Per-pass freshness counter**: backfill helper emits `state.source_calls_inserted_this_pass` + `state.source_calls_updated_this_pass` to structured log on every run.
2. **Watchdog**: extend the existing watchdog infra (e.g., add a row to a centralized table-freshness daemon's monitored-tables list) to alert if `MAX(updated_at)` is more than 6h stale. Implementation deferred to a follow-up — file `BL-NEW-SOURCE-CALLS-FRESHNESS-WATCHDOG` in this PR.

## Acceptance criteria (pre-registered)

1. **Migration idempotent** — `Database._migrate_source_calls_v1()` runs twice without error or duplicate sentinels.
2. **CHECK constraints enforced** — INSERT with invalid `source_type` or `outcome_status` raises sqlite3.IntegrityError.
3. **TG backfill from `tg_social_signals` (842 rows)** — inserts 842 rows; reads `posted_at` correctly; populates `source_id`/`source_event_id`.
4. **X backfill from `narrative_alerts_inbound` (355 rows)** — inserts 355 rows; uses `tweet_ts` for call_ts.
5. **`UNIQUE(source_type, source_event_id)` enforced** — rerunning backfill is a no-op (INSERT OR IGNORE).
6. **Duplicate cluster key correctness** — same (channel, coin_id, date_utc) → same cluster_key; `duplicate_rank_in_cluster` monotonically assigned by call_ts.
7. **Forward-window leakage prevention** — for a synthetic row with `call_ts=T`, the helper that computes `forward_30m_pct` MUST query snapshots with `snapshot_at >= T + 30min` (test inserts adversarial future-dated rows and verifies they're rejected).
8. **Paper-trade linkage — TG path** — `source_calls.linked_paper_trade_id` matches `tg_social_signals.paper_trade_id` 1:1 post-backfill.
9. **Paper-trade linkage — X path heuristic** — temporal correlation matches a known paper-trade where applicable; row is unlinked when no candidate paper_trade exists.
10. **Coverage contract** — `outcome_status='complete'` implies `missing_fields='[]'`; `outcome_status` in {'pending','partial','unresolvable'} implies non-empty `missing_fields`.
11. **No regression** — `tg_social_signals` / `narrative_alerts_inbound` / `paper_trades` writers unchanged; all adjacent tests pass.
12. **Low-n summary helper** — `compute_source_quality_summary(min_sample=10)` excludes sources with <10 calls.
13. **No future leakage in summary** — summary only counts source_calls with `outcome_status IN ('partial','complete')` for forward metrics; pending/unresolvable rows excluded from forward stats.

## Test plan

`tests/test_source_call_outcome_ledger.py` covers all 13 criteria above. Migration test uses tmp_path Database fixture (same pattern as `tests/test_entry_snapshot.py`).

## Rollback

| Edit | Procedure |
|---|---|
| Migration | `DROP TABLE source_calls` + `DELETE FROM paper_migrations WHERE name='bl_source_calls_v1'` + `DELETE FROM schema_version WHERE description='bl_source_calls_v1'`. Idempotent. |
| Backfill | No-op if not run; if run, `DROP TABLE source_calls` is the only state-reset path (idempotent + safe — no other writer depends on it yet) |

## Reviewer focus (P1.4)

Two vectors, plus one mandatory Hermes-first-adequacy lens (per operator's gate):

- **Vector A — Data-leakage / statistical validity (Hermes-first-adequacy folded in):** Forward-window queries respect `snapshot_at >= call_ts + window`? Low-n threshold enforced? Duplicate clustering correct? Does this avoid converting spam into false confidence? **AND: was Hermes-first done honestly?** (Did the session distinguish first-party gecko-alpha skills from stock catalog? Did it avoid replacing durable DB with ephemeral memory? Did it avoid duplicate custom code where a Hermes bridge exists?)
- **Vector B — Structural / schema / runtime:** Schema normalization; idempotent writes; backfill rerunnable; no break to TG/X/paper-trade flows; CHECK constraints exhaustive; FK semantics correct; §12a watchdog satisfied (filed as follow-up); coverage contract internal-consistency holds.

## Open questions for reviewers

1. Is the X-side temporal-correlation heuristic (1h window post-`received_at`) the right fallback for paper-trade linkage, or should it be tighter/wider?
2. Should `duplicate_cluster_key` use `date_bucket_utc` (1-day window) or a tighter `hour_bucket`? Day is operator-readable; hour catches faster repeats.
3. Should the migration also add a `coingecko_meme_score`-style operator-pinned weight column for future per-source policy use, or strictly stay measurement-only? Recommend strictly measurement-only.
4. Should we file a separate PR for the `scout/source_quality/` summary module, or include it in this PR?
5. The §12a watchdog — file as follow-up `BL-NEW-SOURCE-CALLS-FRESHNESS-WATCHDOG` OR include in this PR? Recommend follow-up (separation: ledger is the substrate; freshness alarm is the SLO surface).

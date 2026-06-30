**New primitives introduced:** (1) X token-identity **resolution loop** — internal job that resolves unresolved `source_calls(source_type='x')` rows to a priceable identity and writes back `resolved_coin_id`/`token_id` + reason; (2) `source_call_price_snapshots` table + **forward-only snapshot writer** keyed by resolved identity / (contract_address, chain); (3) **X coverage observability** metrics + read-only endpoint; (4) **X performance silent-failure watchdogs** (§12a freshness + coverage alarms); (5) **influencer performance ranking report** (deferred until data accrues). Everything else reuses existing `source_calls` ledger primitives.

# Design — X Influencer Performance Accrual

- **Date:** 2026-06-30
- **Branch:** `design/x-influencer-performance-accrual`
- **Status:** DESIGN / SPEC — **not for deployment** (see §7 deploy gate)
- **Origin:** operator steer 2026-06-30 — "based on performance so far, show top-10 influencer accounts; asset performance must be the parameter, not volume of alerts." Investigation found there is **no asset-performance data** to rank on. This spec builds the forward-accrual path so the ranking becomes possible. Operator chose Path 2 (fix forward + accrue data).

---

## §1 Problem statement + evidence

A performance-based influencer ranking is **not computable today** because the per-source performance ledger has zero performance data for X. Verified on prod `/root/gecko-alpha/scout.db` 2026-06-30:

| source_type | calls | has `price_at_call` | has `forward_24h_pct` | has `max_favorable_pct_24h` |
|---|---|---|---|---|
| **x** | **2,140** | **0** | **0** | **0** |
| tg | 1,371 | 4 | 4 | 4 |

All 2,140 X calls are `resolved_state='unresolved'` / `outcome_status='unresolvable'`:
- 2,067 `cashtag_only` (symbol extracted, no token mapping)
- 73 `ca_call` (contract address present, but still `token_id=NULL`)

The TG side (4 rows with real prices) proves the pricing machinery works — this is an **X-side resolution + price-coverage gap**, not a dead pipeline. It is the BL-PRICE-COVERAGE / PR #390 gap, confirmed live (PR #390 merged `d76fb7fb`, **not deployed**).

**Volume is the only populated dimension** (e.g., `gem_insider` 530 alerts) — and the operator explicitly ruled volume out as a performance proxy. Correctly so: there is zero evidence those calls were profitable.

---

## §2 Hermes-first analysis

Per CLAUDE.md §7b. Full checklist receipt: `tasks/.hermes-check-receipts/x-influencer-performance-accrual.json` (tag: `extends-Hermes`, 1 Hermes / 9 net-new).

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Tweet ingestion + classification (KOL polling → inbound) | yes — `gecko-x-narrative-scanner` (deployed, hourly) | **use it** — already the upstream feed; reused unchanged |
| CA→coin_id / cashtag→token price-truth resolution | none — CLAUDE.md §7b: "Hermes is the brain, not price/execution truth" | build custom (DB price-truth) |
| Price-at-call / forward-return snapshotting over crypto tokens | none — price truth explicitly carved out of Hermes (deployed-surface doc §91) | build custom |
| Coverage observability / silent-failure watchdogs | none — §12a/§12b are custom-infra disciplines | build custom |
| Influencer ranking analytics over DB rows | none | build custom |

awesome-hermes-agent ecosystem check: no price-oracle / DEX-PnL skill in the hub; price truth is deliberately out of Hermes scope. **Verdict:** net-new dominance is correct and expected — this is price-truth/PnL work, which §7b reserves for custom DB-truth code. The only Hermes-ownable step (ingestion) is already deployed and reused.

---

## §3 Current state (deployed) and root cause

Citations into `scout/source_quality/ledger.py` (verified 2026-06-30):

- **Writer:** `backfill_source_calls()` (:278) → `_fetch_x_rows()` (:332) reads `narrative_alerts_inbound` and sets `token_id = resolved_coin_id`, `resolved_state = CASE WHEN resolved_coin_id IS NOT NULL THEN 'resolved' ELSE 'unresolved'` (:344). Since Hermes leaves `resolved_coin_id` NULL, **every X row enters unresolved with no token_id.**
- **Outcome refresh:** `refresh_source_call_outcomes()` (:497) → `_fetch_snapshot_rows(conn, token_id)` (:142) queries **only** `gainers_snapshots`/`losers_snapshots WHERE coin_id = token_id`. With `token_id=NULL`, `price_rows=[]`.
- **Compute:** `_compute_outcome()` (:167) with empty `price_rows` → `price_at_call=None` → every window `no_time_series` → `outcome_status='unresolvable'` (:195–197). **The forward-return math (:213, :234–241) is correct and fully reusable** — it just never receives a price series for X.
- **Observability:** `compute_source_quality_summary()` (:528) computes per-source coverage but has no X-specific unresolved-by-reason split and no alarm. Lag watchdog (`scripts/check_source_calls_lag.py`) tracks ledger lag, not price/forward coverage.

**Two root gaps for X:** (a) no token-identity resolution step; (b) no price feed covering these tokens keyed by their identity.

**Why historical backfill is impossible:** forward returns require a price snapshot *at* `call_ts` and again at +30m/+1h/+6h/+24h. Those were never captured (`has_price=0`). Illiquid-memecoin price at an arbitrary past minute is not retrievable weeks later. Therefore the fix is **forward-only**; the May–Jun window is permanently unrankable.

---

## §4 Design

Pipeline (per new/immature X call): **resolve identity → snapshot price_at_call → snapshot forward horizons → compute (reuse) → persist outcome + reason → observe → watchdog**.

### §4.1 Token-identity resolution loop (new primitive)
A cyclic internal job (runs alongside the existing source-calls writer) that, for `source_calls` rows with `source_type='x'` AND `resolved_state='unresolved'` AND call age < 24h (only freshly-resolvable calls matter; stale ones can't get a call-time price):
1. **CA path (preferred, unambiguous):** if `contract_address` + `chain` present → resolve via `contract_coin_map` (I1) → `coin_id`; reuse `scout/api/narrative_resolver.py:resolve_ca` logic. Even with no CG `coin_id`, a (contract, chain) pair is directly priceable on DEX (§4.2), so identity = the CA itself.
2. **Cashtag path (ambiguous, low-confidence):** symbol→coin_id is the harder BL-NEW-NARRATIVE-SYMBOL-RESOLVER problem; attempt confidence-gated resolution, tag `linkage_confidence`, and keep these in a **separate cohort** (§4.6). Cashtag-only with no CA that cannot be resolved stays unresolved — surfaced honestly as a coverage limit, never guessed.
3. Write back `resolved_coin_id` (on `narrative_alerts_inbound`, so PR #390 observability stays coherent) and `token_id`/`resolved_state`/reason on `source_calls`. **PR #390 is a prerequisite** — it classifies/repairs CA resolution — but it does NOT itself create performance data.

### §4.2 `source_call_price_snapshots` + forward-only snapshot writer (new primitive)
- New table `source_call_price_snapshots(id, identity_key, identity_kind {coin_id|contract}, chain, price, snapshot_at, source {gt|dex|cg}, created_at)`.
- A writer cycle selects **active** X source_calls (resolved identity, outcome_status ∈ {pending, partial}, call age < 25h) and fetches current price by identity:
  - **DEX-by-CA primary** (GeckoTerminal pool / DexScreener) — covers arbitrary memecoins, which is the whole point.
  - CG snapshot fallback when identity is a listed `coin_id`.
- The first snapshot for a call (taken at ingest, `call_ts` typically ≤ ~1h old) serves as `price_at_call` — within the existing 1h staleness tolerance (`_compute_outcome` :230). Subsequent snapshots accrue across the +30m/+1h/+6h/+24h horizons.
- Extend `_fetch_snapshot_rows` (:142) to also read `source_call_price_snapshots` for the identity. **`_compute_outcome` is reused unchanged** — it just gets a populated `price_rows`.

### §4.3 Outcome reason taxonomy
Extend `missing_fields` reasons with explicit X-unresolved causes: `unresolved_no_ca`, `unresolved_ca_no_coin`, `cashtag_low_confidence`, `price_provider_error`, `dead_pool_no_liquidity`. Every unresolved X call carries a machine-readable reason — no silent NULLs.

### §4.4 Coverage observability (read-only)
Extend `compute_source_quality_summary` + add a read-only endpoint/report exposing, for `source_type='x'`: total calls, resolved count + rate, `price_at_call` coverage, forward-24h coverage, max-favorable coverage, and **unresolved-by-reason split**. These are the metrics that tell us when a ranking becomes trustworthy.

### §4.5 Silent-failure watchdogs (§12a + coverage alarms)
- **§12a freshness:** `source_call_price_snapshots` is a new pipeline table → ship with a freshness SLO + watchdog at the same PR (alert if there are active X calls but zero snapshot writes in N minutes).
- **Coverage alarms (operator's list):** fresh X calls but zero price snapshots; resolved calls with no forward fill 24h post-call; price-provider error-rate spike; all-null performance fields after window maturity.
- All alerts: `parse_mode=None` (signal/handle names contain `_`), and emit `*_alert_dispatched` + `*_alert_delivered` structured logs around the Telegram call (CLAUDE.md §12b delivery-traceability rule).

### §4.6 Ranking design (DEFERRED — do not publish until §8 gate met)
When data exists, rank influencers by **realized forward performance**, not volume:
- Minimum N calls per influencer (n-gate) with `INSUFFICIENT_DATA` shown otherwise.
- Report **median** forward return and **hit-rate** (fraction with positive forward_24h / ≥+50% peak), not just max winners — avoids one moonshot dominating.
- **Separate CA-resolved vs symbol-resolved cohorts** — never mix unambiguous CA performance with low-confidence cashtag joins (name-join contamination, ref ANSEM backtest lesson).
- Show sample size / confidence on every row. De-weight spam volume (rank-1 per duplicate cluster, reusing existing `duplicate_cluster_key`).

---

## §5 Non-goals / guardrails (operator-mandated)

1. **Never present a volume ranking as a performance ranking.** Until §8, the honest answer is "we cannot rank X influencers by performance yet."
2. **No backfill** of cashtag-only May–Jun calls as reliable performance data — not safely reconstructible.
3. **Path 1 (CA-anchored retro sample) is forensic-only** if ever run — must be labeled "CA-anchored historical sample, not representative, small-n, not a leaderboard." Not part of this build.
4. **No deploy during the DEX soak** without separate operator approval (§7). Dev/spec/PR work only.

---

## §6 Build sequence (net-new commits only)

| # | Commit | Scope | ~LOC | Tests |
|---|---|---|---|---|
| C1 | X identity resolution loop | §4.1 — reuse `resolve_ca`+`contract_coin_map`, DEX fallback, write-back + reason | ~120 | ~8 |
| C2 | `source_call_price_snapshots` + forward-only writer | §4.2 — DDL migration, DEX-by-CA fetch, extend `_fetch_snapshot_rows` | ~140 | ~8 |
| C3 | Reason taxonomy + coverage observability | §4.3/§4.4 — extend summary + read-only endpoint | ~100 | ~8 |
| C4 | Silent-failure watchdogs | §4.5 — §12a freshness + coverage alarms, parse_mode=None, dispatched/delivered logs | ~90 | ~6 |
| C5 | **(deferred)** Influencer ranking report | §4.6 — n-gate, cohorts, median + hit-rate | ~80 | ~6 |

C1 depends on PR #390 (resolution prerequisite) being deployed. C5 stays unbuilt/unpublished until §8.

---

## §7 Deployment gate + revert

- **DEX-soak gate:** deployment restarts `srilu` and changes runtime SHA during the active DEX-instrumentation soak. **Do not deploy without separate operator approval.** PR review + merge are fine; the deploy is a distinct decision.
- **Sequenced rollout:** deploy PR #390 first (resolution), confirm CA resolution works, then C1→C4. C5 only after §8.
- **Revert:** each commit is independently revertible; the snapshot writer is gated by a default-off flag (deploy-without-activate pattern) so a merge is inert until the operator flips it.

## §8 When the ranking unlocks (data-bound, not calendar-bound)

Publish the influencer ranking only when, per CLAUDE.md §11a: each ranked influencer has ≥ N forward-complete calls (N TBD, suggest ≥10 CA-resolved), AND coverage metrics (§4.4) show price-at-call + forward-24h coverage above a floor. At current X volume (~50 calls/day across 17 active KOLs, but only ~2 CA-calls/day), CA-resolved cohorts will accrue slowly — expect ~3–4 weeks for the higher-CA posters, longer for cashtag-only. Halt-and-publish the moment the data threshold is met; do not wait for a fixed calendar date.

## §9 Open questions for operator

1. **Cashtag-only scope:** invest in symbol→coin_id resolution (BL-NEW-NARRATIVE-SYMBOL-RESOLVER) to cover the 97% cashtag-only majority, or ship CA-resolved-only first (smaller, unambiguous) and treat cashtag coverage as phase 2?
2. **Ranking N-gate:** minimum complete-calls per influencer before a row is shown (proposed ≥10)?
3. **Price source priority:** GeckoTerminal vs DexScreener as primary DEX price for CA snapshots (latency / coverage / rate-limit trade-off)?

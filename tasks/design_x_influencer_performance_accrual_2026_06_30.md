**New primitives introduced:** (1) X token-identity **resolution loop** — internal job that resolves unresolved `source_calls(source_type='x')` rows to a priceable identity and writes back `resolved_coin_id`/`token_id` + reason; (2) `source_call_price_snapshots` table + **forward-only snapshot writer** keyed by resolved identity / (contract_address, chain); (3) **X coverage observability** metrics + read-only endpoint; (4) **X performance silent-failure watchdogs** (§12a freshness + coverage alarms); (5) **influencer performance ranking report** (deferred until data accrues). Everything else reuses existing `source_calls` ledger primitives.

# Design — X Influencer Performance Accrual

- **Date:** 2026-06-30
- **Branch:** `design/x-influencer-performance-accrual`
- **Status:** DESIGN / SPEC — **DRAFT for formal review**; operator open-questions **locked 2026-06-30** (§9); **not for deployment** (see §7 deploy gate)
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

### §4.0 Design rationale — contract identity is authoritative
> **Contract identity is authoritative for first-pass performance accrual. Cashtags are inventory / unresolved metadata until a separately evaluated symbol resolver exists.**

Empirical basis (`findings_x_assets_called_inventory_2026_06_30.md` §5): the same Solana contract `5UUH9…BhgH2` appears across 5 influencers labeled inconsistently (`$TROLL` for most, `$SOL` for `gem_insider`). The cashtag extractor is unreliable even on CA-bearing tweets, so **the first accrual PR must key on `(contract_address, chain)`, never on ticker identity.** Cashtag-only calls remain inventory + unresolved-by-reason until a separate symbol resolver is built and evaluated on its own merits.

### §4.1 Token-identity resolution loop (new primitive)
A cyclic internal job (runs alongside the existing source-calls writer) that, for `source_calls` rows with `source_type='x'` AND `resolved_state='unresolved'` AND **call age < the price-at-call staleness tolerance** (see §4.2 — a call older than the first-snapshot tolerance can never get a valid call-time anchor and is marked, not faked):
1. **CA path (preferred, unambiguous, and the FIRST-PR target cohort):** if `contract_address` + `chain` present, the **priceable identity is the `(contract_address, chain)` pair itself**, surfaced via the existing `_identity(row)` helper (`ledger.py:94`, which already returns `(key, "contract")` for CA-only rows). `contract_coin_map`/`resolve_ca` are consulted **opportunistically** to attach a CG `coin_id` *when one exists*, but **the target cohort (illiquid memecoins) has `coin_id=NULL`** — `resolve_ca` returns None for them (`narrative_resolver.py:120` requires `coin_id IS NOT NULL`). **Pricing therefore does NOT depend on coin_id resolution** — it goes DEX-by-CA (§4.2). This corrects an earlier framing that treated `resolve_ca` as the mechanism; it only covers the CG-listed minority.
2. **Cashtag path — NOT resolved in the first implementation (operator decision 2026-06-30).** symbol→coin_id is a separate primitive (BL-NEW-NARRATIVE-SYMBOL-RESOLVER) with ticker collisions, chain ambiguity, and false-join risk; it is explicitly **out of scope for the first accrual PR**. Cashtag-only calls remain: (a) parsed inventory, (b) `unresolved_*` with an explicit reason in coverage (§4.3/§4.4), and (c) a future symbol-resolver design/backlog item. No fuzzy resolution — never guessed.
3. Mark `resolved_state='resolved'` + store the priceable identity (`(contract,chain)`, plus `coin_id` if opportunistically found) on `source_calls`; write back `resolved_coin_id` on `narrative_alerts_inbound` only when a real coin_id exists (so PR #390 observability stays coherent). **PR #390 is a prerequisite** — it classifies/repairs CA resolution — but it does NOT itself create performance data.

### §4.2 `source_call_price_snapshots` + forward-only snapshot writer (new primitive)
- New table `source_call_price_snapshots(id, identity_key, identity_kind {coin_id|contract}, chain, price, snapshot_at, source {gt|dex|cg}, created_at)`.
- **GeckoTerminal OHLCV/price-by-pool client is NET-NEW (prerequisite commit C0), not reuse.** The existing `scout/ingestion/geckoterminal.py` exposes only `fetch_trending_pools` — there is **no OHLCV / pool-by-CA / historical endpoint**. C0 adds a GT price fetcher with a **CA→pool-address hop** (GT prices are keyed by pool, not token CA) and its own rate-limit budget (GT free ~30/min, shared with trending-pools ingest). Until C0 exists the snapshot writer cannot price anything.
- A writer cycle (see cadence below) selects **active** X source_calls (resolved identity, `outcome_status ∈ {pending, partial}`, within the forward-window horizon) and fetches **current** price by identity:
  - **GeckoTerminal is the PRIMARY forward-return price source** (operator decision); DexScreener is **fallback/cross-check**; CG only when identity is a listed `coin_id`.
  - **One source per call's forward series.** A single call's price_at_call + horizon snapshots MUST all carry the **same `source`**. If GT is missing at a required horizon, the writer does NOT silently substitute DEX/CG for that horizon's delta — the window is marked `mixed_source_unavailable`, never computed cross-source. Any outcome row whose series spans >1 source is flagged `source_mixed` and **excluded from rankings**.
  - **Every snapshot stores its `source`** (gt|dex|cg).
- **Writer cadence ≤10–15 min, decoupled from the hourly scanner.** The forward windows are tight: `_compute_outcome` requires the +30m delta within `[call+30m, call+45m]` and a `price_at_call` no older than **900s** for the 30m window / **1800s** for 1h (`WINDOWS` `ledger.py:28`). An hourly writer cannot populate 30m/1h at all. **Consequence:** under hourly *ingestion*, the 30m/1h horizons are structurally lossy → the **X ranking anchors on `forward_24h_pct` (primary) and `forward_6h_pct`**, not 30m/1h. The snapshot writer itself runs ≤15 min so the windows it *can* serve are reachable.
- **`price_at_call` anchoring (hard rule).** The first snapshot for a call is recorded with `snapshot_at` = the actual fetch time and **only becomes `price_at_call` when `|snapshot_at − call_ts| ≤ STALENESS_TOLERANCE`** (default 900s, matching the tightest forward gate). If the first snapshot exceeds tolerance, `price_at_call` stays **NULL** and the row is marked `unresolved_stale_first_snapshot` (§4.3) — **never back-anchored to a stale price.** (For forward-only capture the first snapshot is ≥ `call_ts`; the writer records it at/just-after call and `_compute_outcome`'s at-or-before selection must accept a within-tolerance just-after anchor — the one `_compute_outcome` change this design owns; the forward-return *arithmetic* at `:213`/`:234–241` is otherwise reused verbatim.)
- **Outcome-loop rewrite (this design OWNS it — not a drop-in extension).** `refresh_source_call_outcomes` (`ledger.py:501`) currently `SELECT id, token_id, call_ts` and `:509` short-circuits `price_rows=[]` whenever `token_id` is falsy — so CA-only rows (coin_id NULL) never reach pricing. C1/C2 change the SELECT to include `contract_address, chain`, derive the identity via the existing `_identity(row)` helper, and pass `(identity_key, identity_kind)` into a **generalized `_fetch_snapshot_rows`** that reads `source_call_price_snapshots` keyed by `(identity_key, identity_kind)` (today it only does `WHERE coin_id = ?`). The reusable asset is the forward-return arithmetic inside `_compute_outcome`; the identity plumbing and the `:509` call-site gate are net-new.

### §4.3 Outcome reason taxonomy
The X-call reason is a **NOT NULL column with a `pending_resolution` DEFAULT** — a row can never exist with a NULL reason (closes the silent-NULL gap). Terminal/transient codes:
- `pending_resolution` (default — identity not yet attempted), `pending_first_snapshot` (resolved, snapshot writer not yet run)
- `unresolved_no_ca` (cashtag-only, no CA — deferred cohort), `unresolved_ca_no_coin` (CA present, DEX pricing still failed), `cashtag_low_confidence`
- `unresolved_stale_first_snapshot` (first snapshot outside the price_at_call tolerance — §4.2)
- `price_provider_error`, `dead_pool_no_liquidity` (no price at call), `liquidity_vanished_after_call` (valid `price_at_call`, but the pool went dark at forward horizons — so `max_adverse` would silently understate a rug; flagged, not scored as mildly-adverse)
- `mixed_source_unavailable` (GT missing at a horizon; no cross-source substitution)

Any X row not matching a terminal reason carries a `pending_*` reason — never NULL.

### §4.4 Coverage observability (read-only)
Extend `compute_source_quality_summary` + add a read-only endpoint/report exposing, for `source_type='x'`: total calls, resolved count + rate, `price_at_call` coverage, forward-24h coverage, max-favorable coverage, and **unresolved-by-reason split**. These are the metrics that tell us when a ranking becomes trustworthy.

### §4.5 Silent-failure watchdogs (§12a + coverage alarms)
- **§12a freshness (reads OUTPUT rows, not a heartbeat — per §12c):** `source_call_price_snapshots` is a new pipeline table → ship a concrete freshness SLO at the same PR. Expected write rate = (active immature X calls) × (1 / writer-cadence); with cadence ≤15 min, **alert if there exist active X calls but zero snapshot rows written in the last 30 min** (2× cadence). The SLO must distinguish "writer down" (zero writes despite active calls) from "writer ran, nothing priceable" (writes attempted, all `price_provider_error`/`dead_pool` — a coverage problem, not a liveness one).
- **Coverage alarms (operator's list):** fresh X calls but zero price snapshots (30-min SLO above); resolved calls with no forward fill 24h post-call; price-provider error-rate spike; all-null performance fields after window maturity.
- All alerts: `parse_mode=None` (signal/handle names contain `_`), and emit `*_alert_dispatched` + `*_alert_delivered` structured logs around the Telegram call (CLAUDE.md §12b delivery-traceability rule).

### §4.6 Ranking design (DEFERRED — do not publish until §8 gate met)
When data exists, rank influencers by **realized forward performance**, not volume:
- **N-gate denominator = deduped 24h-resolved calls, NOT raw `complete`.** Because 30m/1h are structurally lossy under hourly ingest (§4.2), requiring `outcome_status='complete'` (all four forwards) would starve the gate to permanent INSUFFICIENT_SAMPLE. The denominator is **`eligible_distinct_clusters` with `forward_24h_pct IS NOT NULL` and `duplicate_rank_in_cluster = 1`** — reusing the existing rank-1 dedup at `compute_source_quality_summary` (`ledger.py:558`, fields `eligible_distinct_clusters` / `per_horizon_eligible_counts` already exist). Never count raw rows (duplicate-cluster spam inflates).
- **N-gate (operator decision 2026-06-30):** **≥10** such calls per influencer to display a row; **10–29** → **low-confidence** label; **≥30** → **trusted-ranking** label; **INSUFFICIENT_SAMPLE** if too few influencers clear ≥10 — never publish a thin top-10.
- Report **median** `forward_24h_pct` and **hit-rate** (fraction with positive forward_24h / ≥+50% `max_favorable_pct_24h`), not just max winners — avoids one moonshot dominating.
- **Separate CA-resolved vs cashtag cohorts** — never mix unambiguous CA performance with cashtag joins (name-join contamination, ref ANSEM backtest lesson). The first ranking is **CA-resolved-only** by construction (§4.1). `source_mixed` rows (§4.2) are excluded.

---

## §5 Non-goals / guardrails (operator-mandated)

1. **Never present a volume ranking as a performance ranking.** Until §8, the honest answer is "we cannot rank X influencers by performance yet."
2. **No backfill** of cashtag-only May–Jun calls as reliable performance data — not safely reconstructible.
3. **Path 1 (CA-anchored retro sample) is forensic-only** if ever run — must be labeled "CA-anchored historical sample, not representative, small-n, not a leaderboard." Not part of this build.
4. **No deploy during the DEX soak** without separate operator approval (§7). Dev/spec/PR work only.
5. **No fuzzy symbol resolver in the first implementation** — cashtag→coin_id is deferred to a separate primitive (§4.1).
6. **Standing boundaries (unchanged):** no gate recalibration, no threshold change, no scoring change, no trading-alert behavior change, no proxy scoring, no paid feed, no DEX-soak logic change. DEX observe-only soak unchanged; next checkpoint remains the 7-day report.

---

## §6 Build sequence (net-new commits only)

_(LOC re-estimated after the 2026-06-30 structural review — the earlier "reuse" framing understated C1/C2; see §11.)_

| # | Commit | Scope | ~LOC | Tests |
|---|---|---|---|---|
| **C0** | **GT OHLCV/price-by-pool client (prerequisite)** | §4.2 — net-new GT price fetcher, CA→pool-address hop, own rate-limit budget, `price_provider_error` path. Nothing prices without this. | ~120 | ~8 |
| C1 | X identity resolution loop + outcome-loop rewrite | §4.1 — CA-keyed `_identity` path first-class; **rewrite `refresh_source_call_outcomes` SELECT + `:509` gate**; opportunistic coin_id; NOT-NULL reason | ~140 | ~10 |
| C2 | `source_call_price_snapshots` + forward-only writer | §4.2 — DDL migration, ≤15-min writer, one-source-per-series, price_at_call tolerance gate, **generalize `_fetch_snapshot_rows` to `(identity_key, identity_kind)`** | ~160 | ~10 |
| C3 | Reason taxonomy + coverage observability | §4.3/§4.4 — extend `compute_source_quality_summary` (reuse `eligible_distinct_clusters`/`per_horizon_eligible_counts`) + read-only endpoint | ~100 | ~8 |
| C4 | Silent-failure watchdogs | §4.5 — §12a freshness (30-min SLO, reads rows) + coverage alarms, parse_mode=None, dispatched/delivered logs | ~90 | ~6 |
| C5 | **(deferred)** Influencer ranking report | §4.6 — n-gate on deduped-24h denominator, cohorts, median + hit-rate | ~80 | ~6 |

C0 → C1 → C2 are a hard dependency chain. C1 depends on PR #390 deployed.

C1 depends on PR #390 (resolution prerequisite) being deployed. C5 stays unbuilt/unpublished until §8.

---

## §7 Deployment gate + revert

- **DEX-soak gate:** deployment restarts `srilu` and changes runtime SHA during the active DEX-instrumentation soak. **Do not deploy without separate operator approval.** PR review + merge are fine; the deploy is a distinct decision.
- **Sequenced rollout:** deploy PR #390 first (resolution), confirm CA resolution works, then C1→C4. C5 only after §8.
- **Revert:** each commit is independently revertible; the snapshot writer is gated by a default-off flag (deploy-without-activate pattern) so a merge is inert until the operator flips it.

## §8 When the ranking unlocks (data-bound, not calendar-bound)

Publish the influencer ranking only when, per CLAUDE.md §11a: each ranked influencer has **≥10 deduped 24h-resolved CA calls** (the locked gate, §4.6 — not "complete"), AND coverage metrics (§4.4) show price-at-call + forward-24h coverage above a floor. **Reality check from the inventory:** only 73 CA-bearing calls exist over 41 days (~1.8/day) and they concentrate in `trade` (22), `frostxxbt` (16), `blknoiz06` (8), `gem_insider` (8) — so **at most ~3–4 influencers can plausibly clear ≥10 CA-resolved-24h calls even after weeks**, and a "top-10" may be structurally impossible on CA-only data; expect to show INSUFFICIENT_SAMPLE for most. This is the honest accrual ceiling — surface it, don't force a ranking. Halt-and-publish the moment the data threshold is met; do not wait for a calendar date.

## §9 Operator decisions (locked 2026-06-30)

1. **Cashtag-only scope → CA-resolved-only ships first.** symbol→coin_id is a separate primitive (collisions / chain ambiguity / false-join); not bundled. Cashtag-only stays parsed inventory + unresolved-by-reason + future backlog (§4.1).
2. **Ranking N-gate → ≥10 complete calls to display; low-confidence label 10–29; trusted ≥30; INSUFFICIENT_SAMPLE if too few clear ≥10** (§4.6).
3. **Price source → GeckoTerminal primary (CA/pool OHLCV); DexScreener fallback/cross-check; `source` stored per snapshot; no silent mixing** (§4.2).

## §10 Spec-review checklist for #392 (operator-stated) — verified 2026-06-30

- [x] no volume-as-performance anywhere — guardrail reviewer CLEAN (§1/§4.6/§5.1)
- [x] no historical cashtag backfill — CLEAN (§3/§5.2)
- [x] no fuzzy symbol resolver in the first implementation — CLEAN (§4.1/§5.5/§9.1)
- [x] forward-only snapshotting — CLEAN (§4.2)
- [x] unresolved-by-reason coverage present — hardened to NOT-NULL + default (§4.3)
- [x] watchdog for "fresh calls but zero price snapshots" — concrete 30-min SLO, reads rows (§4.5)
- [x] n-gated rankings only — denominator hardened to deduped-24h (§4.6)
- [x] CA and cashtag cohorts separated — CLEAN (§4.6); `source_mixed` also excluded
- [x] no deploy during DEX soak without separate operator approval — CLEAN (§5.4/§7)

## §11 Formal review resolution (2026-06-30, two-vector dispatch per CLAUDE.md §8)

Reviewed on two orthogonal vectors before any code. **No forbidden-behavior leaks**; structural review caught real gaps where the "reuse" framing understated the build (verified against `ledger.py`). Changes applied to this spec:

| # | Vector | Finding (verified) | Resolution |
|---|---|---|---|
| 1 | structural | `refresh_source_call_outcomes` (`:501`/`:509`) short-circuits `price_rows=[]` on falsy `token_id` → CA-only never priced; `_identity()` helper exists but unconsumed | §4.1/§4.2 now **own the `:501` SELECT + `:509` gate rewrite**; reuse limited to forward-return arithmetic (C1 LOC ↑) |
| 2 | structural | GeckoTerminal client has **no OHLCV/pool-by-CA** endpoint — "primary source" was unbuilt | Added **C0 prerequisite** GT price client + CA→pool hop (§4.2, §6) |
| 3 | structural | hourly ingest vs 900s/1800s staleness gates → 30m/1h silently lossy | Writer cadence **≤15 min**; X ranking **anchors on 24h/6h**; n-gate denominator = deduped-24h, not "complete" (§4.2/§4.6) |
| 4 | structural | `resolve_ca`/`contract_coin_map` return coin_id=None for target memecoins | §4.1 demotes them to opportunistic; **CA-keyed identity is the pricing path** |
| 5 | structural | n-gate "≥10 complete" starves; differs from existing eligibility denominator | §4.6 reuses `eligible_distinct_clusters` + `forward_24h_pct NOT NULL` + rank-1 |
| 6 | structural | dead-pool: `price_at_call`-then-vanish silently understates `max_adverse` | New `liquidity_vanished_after_call` reason (§4.3) |
| A | guardrail | `price_at_call` could be written from a stale first snapshot ("typically ≤1h") | **Hard tolerance rule** + `unresolved_stale_first_snapshot` (§4.2/§4.3) |
| B | guardrail | reason taxonomy asserted exhaustive but had silent-NULL gaps | reason column **NOT NULL, `pending_resolution` default** (§4.3) |
| C | guardrail | per-snapshot `source` stored, but a series could blend sources across horizons | **one-source-per-series** rule; `mixed_source_unavailable` / `source_mixed`-excluded (§4.2/§4.6) |

**Net effect:** scope grew by one prerequisite commit (C0) and the C1/C2 LOC estimates roughly doubled — the spec is now honest that this is a net-new CA-keyed DEX-pricing path, not a thin reuse. Product decisions and all guardrails unchanged.

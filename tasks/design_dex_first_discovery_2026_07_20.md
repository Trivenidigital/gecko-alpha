**New primitives introduced:** `scout/ingestion/gt_new_pools.py` (GT new-pools poller), `dex_pool_discoveries` table + `dex_discovery_v1` migration, `DEX_DISCOVERY_*` settings block. Everything else reuses deployed primitives (contract_coin_map, signal_outcome_ledger, GoPlus safety, §12a watchdog pattern, ACCELERATION research-lane precedent).

# Design — DEX-first discovery, phased (2026-07-20)

Strategic basis: `investigation/DECISION_MEMO.md` H4 ("source coverage gap —
the hardest wall"). Goal: see Solana runners at the **graduation moment**
(first DEX pool) instead of the CG-listing moment (hours-to-days late; ANSEM:
477× at DEX stage vs 21× at CG stage). Scope discipline: observe-first —
no scorer/gate/alert/paper change until the lane's own data justifies it.

## Phase 0 — enable what is already built (operator, zero code)

The I1/I2/I3 DEX instrumentation substrate is **fully shipped** (PR #385, 7
components, 40 tests, runbook) and **inert on the VPS**
(`DEX_INSTRUMENTATION_ENABLED` unset, tables not yet created). Its 2–4 week
soak is the prerequisite clock for ANY gate recalibration downstream.

Action (per `tasks/runbook_dex_instrumentation_enablement_2026_06_29.md`):
set `DEX_INSTRUMENTATION_ENABLED=true` + `TELEGRAM_HEALTH_CHAT_ID`, restart,
verify per runbook §3. **Every day this stays off delays the recalibration
evidence by a day.** Independent of Phases 1–3; start immediately.

## Phase 1 — GT new-pools research lane (observe-only, this PR series)

### What
Poll GeckoTerminal `GET /networks/{network}/new_pools` (public API, keyless —
NOT on the CG credit budget; same host/backoff conventions as the existing
`geckoterminal.py` / `gt_ohlcv.py` modules). Solana first (`CHAINS` already
covers solana/base/ethereum). Record every first-seen pool to a new
`dex_pool_discoveries` table. **No candidate emission, no scoring, no alerts,
no paper trades** — the ACCELERATION_ENABLED research-lane precedent
(`main.py:1087-1097`), not the ingestion-gather seam.

### Table (new, additive)
```
dex_pool_discoveries(
  id INTEGER PK,
  network TEXT NOT NULL,
  pool_address TEXT NOT NULL,
  base_token_address TEXT NOT NULL,     -- mint = contract-native identity
  base_token_symbol TEXT,
  quote_token_symbol TEXT,
  pool_created_at TEXT,                 -- from GT attributes
  first_seen_at TEXT NOT NULL,          -- our clock: discovery latency = first_seen - created
  fdv_usd REAL, liquidity_usd REAL, volume_h1_usd REAL,
  goplus_safe INTEGER,                  -- nullable; Phase 1b enrichment
  UNIQUE(network, pool_address)
)
```
Migration `dex_discovery_v1`, additive CREATE-IF-NOT-EXISTS (same shape as
`dex_instrumentation_v1`). Migration-bearing PR ⇒ two-vector review brief
(fresh install / upgrade-with-data / rollback) before the merge ask.

### Reuse wiring (the leverage)
1. **Identity**: upsert `(base_token_address, network)` into the I1
   `contract_coin_map` with `source='gt_new_pools'`, `coin_id=NULL`. This
   gives the DEX corpus **forward identity at launch** — closing the spec's
   documented gap ("linkage is retroactive; never-listing fizzles invisible").
   When the I1 resolver later maps a CG listing to the same address, the join
   `discovery → CG-listing` yields per-token listing lag for free.
2. **Outcomes**: enroll a budgeted subset of discoveries into
   `signal_outcome_ledger` (`kind='gated_out_sample'`, `surface='dex_new_pool'`,
   token_id `dex:{network}:{address}`) so the existing DexScreener poller
   labels r15m/r1h/r4h/r24h/r7d forward returns with zero new code.
   Safety: REC-06 already bars `dex:` ids from the paper price lane, so this
   cannot open trades; respects the existing enrollment eviction cap; the
   MEASUREMENT-AFFECTING caveat (ledger price_cache rows) is documented and
   already accepted for the ledger's other dex: enrollments.
3. **Watchdog**: one §12a output-row freshness check on
   `dex_pool_discoveries` (pattern copy of the CG-ingestion watchdog).

### Settings (all default-safe)
```
DEX_DISCOVERY_ENABLED=False            # master gate; off = byte-identical
DEX_DISCOVERY_NETWORKS=["solana"]      # start narrow
DEX_DISCOVERY_POLL_EVERY_N_CYCLES=3    # ~1 GT call / 3 min / network
DEX_DISCOVERY_MIN_LIQUIDITY_USD=1000   # drop dust pools at ingest
DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE=3  # bounded ledger pressure
```

### CONTRACT (acceptance / stop gates, per LOOPS.md)
- **Done (build)**: flag off ⇒ zero behavior change (tests assert no GT
  new_pools call); flag on ⇒ discoveries accumulate, dedup holds, ledger
  rows label, watchdog fires on staleness. TDD; black; CI green.
- **7-day evidence report** (the product): discoveries/day; discovery
  latency (first_seen − pool_created, target median < 5 min); % later
  CG-listed + median listing lag (the H5 number, measured not estimated);
  % reaching 2×/5×/10× within 7d (from ledger labels); % unlabelable.
- **Stop gates**: GT 429 pressure → raise poll interval (config-only);
  ledger eviction pressure from enrollments → lower enroll budget; zero
  10×-class hits AND zero CG-list overlap after 14d → thesis weakened,
  report and hold Phase 2.

## Phase 2 — fresh-token signal class (gated on Phase 1 data)
Only if Phase 1 shows real lead time: holder velocity via paid-key GT
`top_holders`, pool-age/liquidity trajectory, buy/sell ratio from I3-style
snapshots — scored in a NEW parallel score field (never the 11-signal quant
score), consumed by nothing until Phase 3.

## Phase 3 — alerting (gated on Phase 2)
DEX-lane alerts through the **#466 detection-lane pattern** (quality gate +
scarce score-ordered slots + funnel log), never the retired conviction gate.
Paper trades for dex: ids stay blocked until a real price path exists
(REC-06 stands).

## Out of scope (recorded, not forgotten)
Pre-graduation pump.fun watcher (bonding-curve stage; needs Helius/WebSocket,
$0-49/mo — backlog.md:2030). Phase 1's graduation-moment coverage is the
free 80%; revisit after the 7-day report quantifies what pre-graduation
coverage would add.

## Rollout order
1. Operator: Phase 0 enablement (today, runbook exists).
2. PR-A: migration + poller + capture wiring + tests (flag off).
3. PR-B: ledger enrollment + watchdog + §12a wiring + tests (flag off).
4. Operator: flag on, 7-day soak, evidence report → Phase 2 decision.

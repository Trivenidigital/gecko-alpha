# Missed-Gainers Gap — Diagnosis & Gap-Fill Design (2026-06-02)

**Operator directive (2026-06-02):** fill the gaps, build the infra to catch the missed gainers
early; cap the tradeable/detection universe at **$200M mcap** (operator never trades above $200M);
autonomous 24h, coordinate with local Codex (xhigh). Source: Top Gainers Tracker showing 575/652
(88.2%) caught, **77 MISSED**.

## What the tracker actually measures (scout/gainers/tracker.py:98)
`gainers_snapshots` records every CG token at +20%/24h. `compare_gainers_with_signals` marks a
gainer DETECTED (`is_gap=0`) if ANY of 4 surfaces saw it **before `first_gainer_at + 5min`**:
- **narrative** → `predictions` (coin_id/symbol)
- **pipeline** → `candidates` (contract_address/ticker, `first_seen_at`)
- **chains** → `signal_events` (token_id, with LIKE for symbols ≥4 chars)
- **spikes** → `volume_spikes` (coin_id)

So **"missed" = no surface saw the token until >5 min after it had already crossed +20%/24h.**
The big lead times (avg 287h) are "we'd been tracking it for ages"; the misses are tokens we had
no record of until the pump was underway.

## Live srilu data on the 77 missed (is_gap=1)
- Moves: min 20%, **avg 129%, max 3507%** — many are large, high-value misses.
- **76 of 77 are ≤$200M** (1 above). So the $200M cap loses ~nothing; the gaps ARE in-range.
- mcap: 12 <$500K, 65 $500K–$500M, **0 >$500M**. coin_ids are CG slugs (CG-listed, reachable).
- **40 of 77 exist in `candidates`** — BUT `detected_by_pipeline=0` for all 77, so those 40 were
  `first_seen_at` **AFTER** the pump (>5min late). I.e., **we ingest 40/77 reactively, once they
  enter the CG top-movers/gainers pages — too late to be "early."** The other ~36 we never ingested
  pre-pump (or only under a different identifier).
- The scorer scores this band ~0 (avg quant_score 0.1, max 2) — CG-listed $500K–$200M tokens lack
  the DEX data (liquidity/txns/age) the scorer needs and are above the $500K scorer corpus — but
  that's moot for the tracker, which only needs an early `candidates` row, not a high score.
- Surface recall on the 575 hits: chains **555** (workhorse ~97%), narrative 220, pipeline 85,
  spikes 77 (overlapping). The chains surface (`signal_events`) carries detection; it doesn't reach
  these CG-only tokens.

## Interim read (the attribution matrix below CONFIRMS this is coverage-dominant)
We are not blind to most of these tokens — we eventually ingest 40/77. The problem is our CG scan
lanes are **reactive**: top_movers (1h change), trending, by_volume, midcap (rank 251–1000, 24h≥25%)
all surface tokens that are **already moving/ranked**. By the time a ≤$200M token shows up in those
pages, it has usually already done the +20%. There is no **proactive** lane that scans the ≤$200M
band *before* the move. The binding constraint is the CG free tier (6/min; cycles run 69–188s under
429 backoff per PR #349 instrumentation) — so we cannot just "scan everything."

## Gap-fill design hypothesis (to validate with Codex + attribution)
1. **$200M ceiling** across the markets-watcher/midcap config (GAINERS_MAX_MCAP 500M→200M, midcap
   ceiling, any other $500M gate). Frees CG budget by dropping the $200M–$500M slice the operator
   never trades — that freed budget funds proactivity.
2. **Proactive ≤$200M scanning** — reallocate freed budget to scan a slice of the ≤$200M universe
   *before* it tops the movers list (e.g., a volume/acceleration-ranked page, or new-listings), so
   `candidates`/a velocity surface get an early `first_seen_at`.
3. **Velocity/acceleration surface** — flag tokens accelerating (1h/4h price + volume) *before* the
   24h +20% completes, using `price_cache` + `volume_history_cg` we already maintain. Either extend
   an existing surface or add a 5th (writing where the tracker counts it). Flag-gated + §12a watchdog.

**Honest limits:** truly-new tokens with no pre-pump CG volume can't be caught from CG data alone
(need DEX new-pools / social — the narrative/chains surfaces' domain); recall won't reach 100%, and
looser thresholds trade precision for noise — the false-positive cost must be measured, not assumed.

## Open / next
- Per-surface attribution (Task #9): confirm the 40 are reactive-late + quantify how many of the 36
  could be caught by a deeper ≤$200M scan vs are genuinely un-pre-observable.
- Codex xhigh opinion (in flight): agree on earliness diagnosis; smallest infra that moves recall;
  budget math for the $500M→$200M reallocation.
- §7b Hermes-first before building.

## Attribution matrix results (srilu, 2026-06-02) — DECISIVE
Cohort = 77 missed. By mcap-at-gainer-snapshot: 16 ≤$200M, 0 >$200M, 61 null (no snapshot mcap;
current price_cache says ~all ≤$200M). Key counts over all 77:
- **Ingested before the pump (first_seen < appeared+5m): 0/77.** Ever-in-candidates: 40 (all AFTER).
- **Pre-pump `volume_history_cg`: only 11 have ≥3 samples, 5 have 1–2, 61 have ZERO.**
- **Omitted surfaces fired before the pump: momentum_7d 0, slow_burn 0, velocity_alerts 0.**

### Refined diagnosis
The dominant gap (**61/77, ~79%**) is **COVERAGE/CADENCE** — we have NO pre-pump price/volume history
for them because our CG lanes only fetch them reactively once they top the movers list. An
acceleration detector over *existing* history can only reach the ~11–16 with history. Wiring the
omitted surfaces (momentum/slow_burn/velocity) into the tracker does NOT help these 77 (they didn't
fire) — though it improves the metric's honesty + credits real detections on OTHER gainers.

### Phased plan (incremental, §11 data-bound)
**Increment 1 — safe/additive, ship first (helps the minority + improves measurement + sets the $200M frame):**
1. `gainer_acceleration` detector (Codex design): 1h+4h price + volume expansion over `volume_history_cg`,
   $500K–$200M, cooldown, persisted table, zero extra CG calls. Research-only (no alert/paper-trade
   until precision measured). Catches the ~11–16 with pre-pump history.
2. Wire `gainer_acceleration` (+ momentum_7d + slow_burn + velocity_alerts) into
   `compare_gainers_with_signals` as additional surfaces so real detections are credited.
3. `$200M` ceiling — multi-knob (Codex's list): GAINERS_MAX_MCAP, VOLUME_SPIKE_MAX_MCAP,
   MOMENTUM_7D_MAX_MCAP, SLOW_BURN_MAX_MCAP, COINGECKO_MIDCAP_SCAN_MAX_MCAP, PAPER_MAX_MCAP,
   VELOCITY_MAX_MCAP. **NOT** MAX_MARKET_CAP=500K (scorer corpus). + §12a watchdog for the new table.

**Increment 2 — the dominant lever (coverage), bigger/budget-bound, design with soak data:**
4. Proactive ≤$200M coverage lane: reshape CG `/coins/markets` rank-banded scanning to build pre-pump
   `volume_history_cg` for a deeper ≤$200M slice, funded by dropping the >$200M fetches (the cap saves
   budget ONLY if pages/rank-windows shrink — Codex). Use the PR-2 `run_cycle_s`/`ingestion_s`
   instrumentation to keep within the 6/min budget. Soak + measure recall lift on the tracker.

### Honest ceiling
CG free-tier budget fundamentally bounds pre-pump coverage; truly-new/low-volume tokens need
DEX new-pools / social sources (the narrative/chains surfaces' domain). Recall will not reach 100%.

## CORRECTION (2026-06-02, supersedes the attribution above) — timestamp-comparison bug

While building the surface wiring I found the tracker's lead-time comparison silently drops
**same-day** early detections — and it had **contaminated the attribution matrix above**.

### The bug
Every detector writes `detected_at` via Python `datetime.isoformat()` (`2026-..T..+00:00`), and
`store_top_gainers` writes `snapshot_at` the same way (verified on srilu: spikes/signal/pred/cand/
snap all isoformat-T). The surface checks compared `detected_at < datetime(appeared,'+5 minutes')`.
`datetime(...)` returns **space-format** `YYYY-MM-DD HH:MM:SS`, so the comparison is `'..T..' <
'.. ..'` — a TEXT compare where byte 10 is `'T'`(0x54) vs `' '`(0x20). `'T' > ' '`, so a same-day
detection always compares **greater** than the bound and is dropped. Only detections on an **earlier
calendar day** (where bytes 0-9, the date, already differ) were credited. The existing pytest used
`datetime('now')` (space-format) on both sides, so it never reproduced the prod path.

### Measured impact on srilu (652 tracked, 77 gaps)
- `spikes_credited_now=77` but `spikes_correct=102` (datetime-normalized) → the bug under-credited
  the spikes surface alone by 25.
- **Of the 77 gaps, 31 (40%) are FALSE GAPS** — detected early by an EXISTING surface (chains 31,
  pipeline 5, spikes 1) but dropped by the same-day bug. `recoverable_existing=31`.
- Wiring momentum/velocity/slow_burn (correctly normalized) recovers **+14 → 45 (58%)** of the 77.
- **32 are true residual** coverage gaps (genuinely unobserved pre-pump).

### The earlier matrix was wrong
The "DECISIVE" matrix's "omitted surfaces fired before the pump: momentum 0 / slow_burn 0 /
velocity 0" was produced by the same string-compare bug. Normalized: **momentum 10, velocity 13,
slow_burn 10** of the 77. The `volume_history_cg` density counts (11 / 5 / 61) were NOT contaminated
(that query used a different comparison) — so the acceleration detector's reachable backfill cohort
is still ~11, and its real value is **forward** early-warning, not backfill recovery.

### The fix (this PR, the dominant lever)
Wrap both sides of all four existing surface checks (and the new helper) with `datetime()` so the
comparison is on normalized space-format timestamps. Empirically re-verified: the existing spikes
check now credits a same-day isoformat-T detection (`detected_by_spikes=1`, lead 60min). Regression
test `test_compare_credits_same_day_isoformat_spike` pins it.

### Prod metric shift (operator-visible on next deploy + recompute)
caught **575 → ~620** (recovers 31 false gaps + ~14 new-surface), gaps **77 → ~32**, hit-rate
**88.2% → ~95%**, and `avg_lead_minutes` **drops** (it had been averaging only the long-lead
prior-day detections; same-day catches have shorter, more honest leads). This is a measurement
correction, not a behavior regression — the tracker was always this good; the bug under-reported it.

### Revised lever order
1. **Timestamp-comparison fix** — recovers 31/77, zero new infra. THE lever.
2. **Surface wiring** (momentum/velocity/slow_burn into the tracker) — +14.
3. **Acceleration detector** — forward early-warning; small backfill (~11 with history).
4. **Coverage (Increment 2)** — the 32 true residuals.

**New primitives introduced:** `gainer_acceleration` table + detector; `detected_by_acceleration` / `detected_by_momentum` / `detected_by_slow_burn` / `detected_by_velocity` columns on `gainers_comparisons`; `ACCELERATION_*` config keys; (Increment 2, separate PR) a proactive ≤$200M coverage scan lane.

# Gainer Gap-Fill Design (2026-06-02)

Closes the Top-Gainers-Tracker recall gap (77 missed). Evidence + attribution:
`tasks/findings_missed_gainers_gap_2026_06_02.md`. Operator directive: fill the gaps, build the
infra, cap the universe at **$200M** (never trades above), autonomous, co-ordinate with Codex (xhigh).

## Hermes-first analysis (§7b)
| Domain | Hermes skill found? | Decision |
|---|---|---|
| crypto market scanning / CoinGecko ingestion | none (skill hub catalog failed to load; not a Hermes domain) | build custom |
| token price/volume acceleration / velocity detection | none found | build custom |
| early-pump / gainer detection | none found | build custom |

awesome-hermes-agent ecosystem: no crypto-market-detection capability. The project's own stance
(`backlog.md:185`) is explicit — Hermes is the durable-memory / scheduling / routing layer; repo-grounded
detection/implementation is the custom/Codex layer. **Verdict: build custom** (detection over our own
`volume_history_cg`/CG data); no Hermes/external library fits.

## Problem (attribution matrix, decisive)
Of 77 missed gainers (`is_gap=1` — no surface saw them before `appeared_on_gainers_at + 5min`):
- **0/77 ingested before the pump**; 40 are in `candidates` but only *after* (reactive-late).
- **61/77 have ZERO pre-pump `volume_history_cg`** (11 have ≥3 samples, 5 have 1–2).
- Omitted surfaces (`momentum_7d` / `slow_burn` / `velocity_alerts`) fired before the pump on **0/77**.
- ~all ≤$200M (the cap loses nothing).

So the **dominant gap (~79%) is COVERAGE** — we have no pre-pump data because our CG lanes fetch these
tokens only reactively. An acceleration detector over *existing* data reaches only the ~11–16 with
history; wiring omitted surfaces doesn't help these 77 (didn't fire) but does fix the tracker's honesty
and credit real detections on *other* gainers.

## Increment 1 (this PR) — additive, research-only, zero extra CG calls
### 1a. `gainer_acceleration` detector
New module (`scout/gainers/acceleration.py`): for each coin with ≥N recent `volume_history_cg` rows,
compute **1h and 4h price change** + **volume expansion vs a short trailing baseline**, filter
`$500K ≤ mcap ≤ $200M` + a volume floor, apply a per-coin cooldown, and persist detections to a new
`gainer_acceleration(coin_id, symbol, name, change_1h, change_4h, vol_expansion, market_cap,
current_price, detected_at)` table. Runs in the EVALUATE path over data already in the DB → **0 CG
calls**. Flag `ACCELERATION_ENABLED` (default **True** — this is the gap-fill activation; do NOT ship
disabled = deploy-without-activate). Detection-only: **no Telegram alert, no paper-trade** until
precision is measured (`ACCELERATION_ALERT_ENABLED` / `ACCELERATION_PAPER_ENABLED` default False).
For the manual-research use case this still fills the gap — the early detections surface on the
dashboard/tracker so the operator sees them early.

### 1b. Tracker surface wiring
Migration (additive columns, low-risk): `gainers_comparisons` gains `detected_by_acceleration`,
`detected_by_momentum`, `detected_by_slow_burn`, `detected_by_velocity` (+ `*_lead_minutes`).
`compare_gainers_with_signals` (`scout/gainers/tracker.py:98`) checks `gainer_acceleration`,
`momentum_7d`, `slow_burn_candidates`, `velocity_alerts` (coin_id, `detected_at < appeared+5min`) and
clears `is_gap` when any matches. Makes real early detections count — the tracker stops under-reporting.

### 1c. `$200M` ceiling — multi-knob (verify each key exists in config.py)
Set to 200_000_000: `GAINERS_MAX_MCAP` (500M), `VOLUME_SPIKE_MAX_MCAP`, `MOMENTUM_7D_MAX_MCAP`,
`SLOW_BURN_MAX_MCAP`, `COINGECKO_MIDCAP_SCAN_MAX_MCAP`, `PAPER_MAX_MCAP`, `VELOCITY_MAX_MCAP` (50M).
**Do NOT touch `MAX_MARKET_CAP=500K`** (scorer micro-cap corpus — out of scope). Note (Codex): the
cap narrows downstream rows/noise but does NOT by itself cut CG requests — request savings come in
Increment 2 via page/rank-window changes.

### 1d. §12a watchdog
`gainer_acceleration` is a new pipeline table → pair with a freshness/row-rate watchdog (clone the
`check_source_calls_lag.py` shape) + crontab entry, or document the SLO if write-rate is bursty/low.

### Tests
Unit: acceleration math (1h/4h change + vol expansion) over synthetic `volume_history_cg`; mcap/volume
filters; cooldown/dedup; `compare_gainers_with_signals` credits each new surface; the migration is
idempotent + additive. Locally runnable (no aiohttp in the detector/tracker).

### Risks / rollback
Additive (new table + columns + a detector that only reads existing data). `ACCELERATION_ENABLED=False`
fully reverts the detector; the tracker-surface columns are inert if the tables are empty. Precision
(false "accelerations") is measured during soak before any alert/paper-trade is enabled. The $200M cap
is reversible per-knob via .env.

## Increment 2 (separate PR, after Inc-1 soak) — coverage lane (dominant lever)
Reshape CG `/coins/markets` rank-banded scanning to build **pre-pump** `volume_history_cg` for a deeper
≤$200M slice, **funded by dropping the >$200M fetches** (rank/page windows), staying within the 6/min
budget (use PR-2 `run_cycle_s`/`ingestion_s` instrumentation). Soak + measure recall lift. Higher
blast radius (touches the binding CG constraint) → its own design + reviews.

## Anti-scope
No paper-trade/alert from acceleration until precision measured; no scorer-corpus ($10K–$500K) change;
no CG free-tier budget increase; no urgency/ranking semantics added to existing trader surfaces.

## Honest ceiling
CG free-tier budget bounds pre-pump coverage; truly-new / low-volume tokens need DEX new-pools / social
sources (the narrative/chains surfaces' domain). Recall will not reach 100%.

## Codex xhigh design-review dispositions (2026-06-02) — accepted
- **Framing:** Increment 1 is measurement + minority recall (~11–16), NOT the dominant fix; Increment 2
  (coverage) is the real recall lever. Ship Inc 1 first (additive, 0 CG calls, research-only).
- **Acceleration thresholds (research-only):** `1h ≥ 8%`, `4h ≥ 12%`, `vol_expansion ≥ 2.0x`,
  `min_samples ≥ 3`. (Promotion-to-alert later: `1h ≥ 12%`, `4h ≥ 18%`, `vol_expansion ≥ 3.0x`,
  `min_samples ≥ 6`.) Lenient min_samples now so the small reachable cohort isn't thrown away.
- **Volume caveat:** `volume_history_cg.volume_24h` is a CG 24h *snapshot*, not interval volume → vol
  expansion is noisy/false-positive-prone → keep tracker/dashboard-only (no alert/paper) until measured;
  price acceleration (1h/4h) is the stronger leg, vol_expansion a soft filter.
- **Wiring scope (broader than first stated):** `gainers_comparisons` add `detected_by_acceleration/
  momentum/slow_burn/velocity` (+ `*_lead_minutes`); update ALL surface enumerations —
  `compare_gainers_with_signals` (~tracker.py:148), the comparison INSERT (~:258), the SELECT/read path
  (~:321), the stats lead-time union (~:354), the dashboard read path, and tests. Add a
  `(coin_id, detected_at)` index on `gainer_acceleration`.
- **Watchdog = execution heartbeat, NOT row-rate** — zero acceleration rows can be healthy; the watchdog
  must verify the detector RAN each cycle (heartbeat file / log), not that it produced rows.
- **null-mcap handling:** skip rows with null mcap from the acceleration filter + emit skip telemetry.
- **$200M knobs:** the 6 reduced (done); leave `VELOCITY_MAX_MCAP` (velocity stays disabled under
  Option A). Caveat for Increment 2: `COINGECKO_MIDCAP_SCAN_MIN_MCAP=$10M` excludes $500K–$10M, so any
  reused midcap lane won't cover the lower band.
- **Increment 2 is budget REALLOCATION, not additive scan:** dropping >$200M after fetch saves no CG
  calls (pages are paid); need page/rank-window changes (rotate a deeper ≤$200M rank-band page in place
  of a higher one). Design separately with the soak data.
- Also: track strict pre-gainer lead separately from the existing `+5min` tolerance.

## REVISION (2026-06-02) — timestamp-comparison bug is the dominant lever

Building the wiring surfaced a silent-failure bug that reframes Increment 1 (full evidence:
`findings_missed_gainers_gap_2026_06_02.md` §CORRECTION). The tracker's lead-time check compared
isoformat-T `detected_at` against `datetime()`'s space-format output, silently dropping all
**same-day** early detections (`'T'` 0x54 > `' '` 0x20 at byte 10). srilu measurement: **31 of the
77 "missed" (40%) are false gaps** the existing surfaces already detected early; the bug also
contaminated this doc's original attribution (momentum/velocity/slow_burn were 10/13/10, not 0).

**Increment 1 now leads with the fix** (in priority order):
1. **Normalize the comparison** — wrap both sides of the 4 existing surface checks + the new helper
   with `datetime()`. Recovers 31/77, zero new infra. Regression test pins the isoformat-T path the
   old `datetime('now')` tests never hit. Operator-visible metric shift: caught 575→~620, gaps
   77→~32, hit-rate 88.2%→~95%, avg_lead drops (more honest). Measurement correction, not regression.
2. **Surface wiring** (acceleration + momentum/slow_burn/velocity, all normalized) — +14.
3. **Acceleration detector** — reframed as **forward** early-warning (backfill cohort ~11 with
   history); still research-only, still the right additive infra for the user's "beat Highlights by
   minutes" use case.
4. **$200M cap** + **§12a heartbeat watchdog** (`scripts/acceleration-heartbeat-watchdog.sh`,
   execution-heartbeat via the `acceleration_scan_complete` journal line, NOT row-rate).
   Watchdog scheduling is **opt-in** per the repo's Telegram-alerting-watchdog convention
   (`cron/README.md`); the operator enables the crontab line. The 31/45/32 decomposition is
   reproducible via `scripts/audit_missed_gainers.py` (validated on srilu).
   Same timestamp bug was also fixed in `scout/trending/tracker.py` (flagged by review).

Increment 2 (coverage) still owns the 32 true residual gaps.

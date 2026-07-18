# Phase 3 — Case replay table

Read-only reconstruction from committed evidence. Rows marked `[NEEDS-DB]`
require `investigation/case_replay.py --db /root/gecko-alpha/scout.db <tokens>`
on the VPS — this repo carries no DB, and `gainers_snapshots` 7-day retention
means several answers are only in (or already gone from) prod.

Funnel-stage vocabulary (per brief): **detected** (row exists in any ingest
table) → **signaled** (quant ≥ MIN_SCORE=65) → **gated** (conviction
evaluated) → **alerted** (operator ping sent).

## Part (a) — the "24 detected-never-alerted 5x" cohort

The "24" count is not reproducible from committed evidence (see FINDINGS §1).
The verifiable cohort is the clean same-asset 30d corpus (n=672 measurable
CG-native contracts, findings_same_asset_under_gate_cohort_30d_2026_06_28.md):

| Token | Seen? | Peak score | Killed at stage | Earliest possible alert vs pump | Hypothetical exit-machinery PnL |
|---|---|---|---|---|---|
| ANSEM (the-black-bull) | YES — CG stage at $4.3M mcap; DEX stage ($190K) seen but unmeasurable (CG row mcap=0) | 50 → decayed 13 (≤18 in clean cohort) | **signaled** — never reached 65; gate never ran | CG-stage entry = 21× later; DEX-stage entry (477×) invisible to all sources (H4). Even instant CG-stage alert captures 21× | `[NEEDS-DB]` — 21× peak through peak_fade (~10pp giveback at high peaks) ≫ realized $0; exact path needs snapshot series |
| bitads | YES — CG-native | ≤18 | **signaled** (never ≥65) | reactive CG lane, post-move entry; 49.9× peak from first-seen | `[NEEDS-DB]` |
| main-street-usd | YES — CG-native | ≤18 | **signaled** (never ≥65) | as above; 11.5× peak | `[NEEDS-DB]` |
| tensor, myro, return-to-memes, catwifhat (raw-list residue) | join-contamination **artifacts** — copycat/CG-vs-DEX name collisions; excluded from clean cohort | — | (measurement artifact, not a funnel kill) | — | — |
| remaining ~18-21 of "24" | **unverifiable** — source list uncommitted (backlog_fable_analysis_2026_07_10.md) | — | — | — | run case_replay.py with the operator's list `[NEEDS-DB]` |

## Part (b) — operator-named meme runners

| Token | Seen? | Killed at stage | Earliest possible alert vs pump start | Hypothetical exit-machinery PnL |
|---|---|---|---|---|
| **ANSEM** (+3,354% dashboard case) | YES (CG stage only) | **signaled** — score 50 vs gate 65; ALSO dashboard-classified TOO_LATE/score-0 at the runner moment (moved_already.py:1-10) | Pump started at DEX stage ($190K); first structurally possible sighting = CG listing at $4.3M — **the 22× early premium is unreachable with current sources** (H4+H5) | 21× ceiling from CG entry; through peak_fade ≈ 90% peak-capture on the one comparable (ASTEROID) `[NEEDS-DB]` for exact path |
| **CATCASH** | `[NEEDS-DB]` — no committed table row references it. If pump.fun pre-graduation launch: structurally invisible to DexScreener (needs paid boost + graduated pair), GT (needs trending rank), CG (needs listing) | expected: **never detected** (H4) — confirm with case_replay.py | If never ingested: N/A — source gap, not a bug | N/A until seen |
| **JOTCHUA** | `[NEEDS-DB]` — same as CATCASH; no committed reference | expected: **never detected** (H4) — confirm | as above | as above |
| CATWIFHAT | ambiguous — product_note_trade_now_lane_2026_06_30.md:20 says "recorded but never alerted"; slow_burn §5b flags the catwifhat join as contaminated (copycat collision) | **signaled** at best; measurement contaminated | `[NEEDS-DB]` disambiguate real vs copycat row | `[NEEDS-DB]` |
| cat-in-hood, dodo, bycocket, cash-dog-in-hood (PR #466 eval cohort, 2026-07-11→14) | YES — CG trending, detected pre-trend with leads +460/+433/+132 min | **alerted-stage kill**: detection lane was ON but ungated → all 5 daily slots consumed by score-0 noise; these were never sent | Earliest-possible alert = detection time, which was 2-7.5h BEFORE trending — **early enough to matter**; the kill was slot-starvation, fixed by #466's quality gate (8/10 recall) | `[NEEDS-DB]` — post-detection price paths in gainers_snapshots (7d window has expired; only postmortem rows if #459 were on) |

## Structural answers to the brief's four questions for the pump.fun class

1. **Ever ingested?** Pre-graduation: NO source can carry it — DexScreener
   discovery = paid boosts only (dexscreener.py:15,92,116-120) and
   pre-graduation tokens have no pair at all; GT = trending-rank-reactive
   (geckoterminal.py:104,117-123); CG = listing-gated, hours-to-days late
   (findings_missed_gainers_gap_2026_06_02.md:26,37-41). A pump.fun
   new-deploy watcher exists only as an unbuilt backlog line
   (backlog.md:2030,2047). **Structural gap, not a bug.**
2. **If ingested, what killed it?** For everything measurable: the
   score-vs-gate wall (max 59 vs 65; winners ≤18) — i.e., killed at
   **signaled**, before gate or alert logic ever ran.
3. **Would the earliest-possible alert have mattered?** Split verdict: for
   CG-stage sightings of monsters, yes (ANSEM 21× from the CG row) — but the
   477× DEX-stage premium is unreachable without new sources. For the #466
   cohort, detection genuinely led trending by 2-7.5h — early enough, killed
   downstream.
4. **Hypothetical exit PnL:** exit machinery is the proven component
   (peak_fade: +$62/trade at <5pp giveback, n=139; the counterfactual grid
   says tighter peak-fade adds +41% on high-peak trades). Exact per-token
   paths: `ledger_backfill.py` + `case_replay.py` on prod.

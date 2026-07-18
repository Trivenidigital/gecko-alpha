# DECISION MEMO — fix gecko / retarget gecko / sunset → unchain

Read-only investigation, 2026-07-18. Evidence base: `investigation/FINDINGS.md`
(every claim cited there), `investigation/replay_table.md`. This memo lays out
what the data says per option. **No recommendation is made.**

---

## Hypothesis adjudication

| # | Hypothesis | Verdict | Decisive evidence |
|---|---|---|---|
| H1 | Gate miscalibration is the dominant failure; detection is fine | **PARTIAL** | The gate wall is real — max score 59 vs MIN_SCORE=65, 0 alerts since 06-21, gate now retired (#440). But the mechanism was a **ceiling drop** (2026-06-02 renormalization), not a threshold raise, and "detection is fine" fails for the class that matters: the measured ≥10× winners scored **≤18** — no plausible recalibration (p99≈45-55) catches tokens scoring 18. Recalibrating the conviction gate fixes the *silence*, not the *selectivity*. |
| H2 | Frozen-suppression-lock compounds H1; PR #424 needed | **SUPPORTED (with a caveat)** | Three combos latched at `parole_exhausted` incl. the only historically-profitable lane (`gainers_early`, +$894 n=128 at its keep-verdict; dark 7.5 weeks behind TWO kills). #424 is merged (5d5cfb6) but **deploy-to-prod unconfirmed**, and revival additionally requires clearing the *signal-level* auto-suspend — reviving only one gate leaves the other blocking. Caveat: the 2026-05-19 auto-suspend itself was justified (regime shift), so unlatching ≠ profit. |
| H3 | Signals target the wrong archetype | **SUPPORTED** | Of 11 divisor signals a minutes-old runner can fire only `market_cap_range` (≤8) + `solana_bonus` (5) ≈ 13 raw pts vs the 65 needed. `token_age` awards 0 below 3h AND above 7d (ANSEM was 9 days old → 0); `vol_acceleration`/`momentum_ratio`/`vol_liq_ratio` all need 24h-7d history; `holder_growth` (25 pts) is a phantom (no MORALIS key). The scorer is built for a 12-48h-old, CG-listed, pre-pump accumulation pattern — not social-velocity runners. |
| H4 | Source coverage gap — runner venues never ingested | **SUPPORTED — the hardest wall** | Pre-graduation pump.fun tokens are invisible to ALL three discovery sources by construction (DexScreener = paid boosts + needs a graduated pair; GT = trending-rank-reactive; CG = listing-gated, hours-to-days). "Truly-new tokens with no pre-pump CG volume can't be caught from CG data alone" (findings_missed_gainers_gap:54). The pump.fun new-deploy watcher was spec'd and never built (backlog.md:2030,2047). ANSEM's 477× lived at the DEX stage; gecko's first possible sighting was the CG listing at 22× worse entry. |
| H5 | Latency — alerts structurally too late for this class | **PARTIAL** | For the DEX-stage premium: yes — CG-listing lag (hours-to-days) forfeits ~22× of ANSEM's 477×. But the CG-stage entry still offered 21×, and the #466 evaluation proved detection can lead CG-trending by 2-7.5h (cat-in-hood +460 min) — when a lane actually sends. Latency is fatal only for the sub-hour launchpad archetype; for the "CG-listed monster" sub-class the binding constraint was H1/H3 (score wall), not time. |

**Interaction that matters most:** H4 and H3 are the same wall seen from two
sides — the sources can't see the token until it's already CG/GT-visible, and
by then the scorer's "early" features have aged out. Fixing either alone
changes little.

---

## Option 1 — FIX (bet: H1+H2 dominate)

**What must change (from the evidence, minimal set):**
1. Restore CG ingestion (Demo-key quota dead since 07-13) — prerequisite for
   everything; command pack in tasks/ops_pack_priorities_2026_07_18.md.
2. Confirm #424 is live in prod (P1.suppression_state query) and execute the
   two-gate `gainers_early` revival per docs/runbook_gainers_early_revival.md.
3. Replace the retired conviction gate with the **detection-lane pattern that
   already works**: #466's evidence-validated quality gate (quant ≥1 → 8/10
   early-catch recall, ~15x precision, ~3.7 alerts/day) + flag the lane ON.
4. Recalibrate or delete MIN_SCORE=65 (it gates MiroFish, which has been idle
   since 06-01 — 50 jobs/day of paid narrative capacity unused).

**What the fixed system earns (case-replay says):** the honest ceiling is the
CG-native corpus — 3 of 672 measurable contracts ≥10× in 30d, all scoring
≤18. A fixed gecko alerts reliably on the #466 cohort class (2-7.5h pre-trend
leads) and on CG-listed monsters at 21×-class entries, feeding the proven exit
machinery (+$62/trade in the <5pp-giveback bucket). It does NOT catch
launchpad runners. **Effort: days** (config + already-merged PRs + operator
deploys). **This is the cheap option and its EV is bounded by the CG-native
corpus.**

## Option 2 — RETARGET (bet: H3 dominates)

**Salvageable:** exit machinery (the one proven edge), paper-trade engine +
outcome ledgers (both implemented), watchdog/alerting infra (§12a/§12b
pattern, mature), aggregator/dedup, the TG plumbing.

**Not salvageable for the runner archetype:** all three discovery sources
(H4 says the tokens never arrive) and 9 of 11 signals (history-dependent).
Retargeting means: new ingestion (pump.fun/launchpad WebSocket or indexer,
DEX new-pools), new signal class (holder velocity — requires funding a
Moralis/Helius key; social velocity; bonding-curve progress), sub-minute
cadence. That is a new detection front-end grafted onto gecko's back-end —
**effort: weeks, and it converges on Option 3's build anyway**, with the
constraint that gecko's 60s CG-budgeted loop and CG-shaped schema come along.

## Option 3 — SUNSET → UNCHAIN (bet: H4+H5 dominate)

**What a successor must have that gecko structurally lacks:**
1. **Launch-time sources:** pump.fun new-deploy + graduation events, DEX
   new-pool streams — push, not poll (gecko's fastest loop is 60s and its
   sources are all reactive rankings).
2. **A latency budget in seconds-to-minutes** end-to-end (gecko: CG-listing
   lag hours-to-days + 60-190s cycles + 180s MiroFish on the retired path).
3. **Fresh-token signal class:** holder-growth (funded key), social/cashtag
   velocity, bonding-curve progress, deployer/wallet heuristics — none exist
   in gecko; the 24h/7d-history signals cannot be transplanted.
4. **Contract-native identity** (mint address as primary key) — gecko's
   coin_id↔contract linkage is retroactive and "never-listing fizzles are
   permanently invisible" (spec_dex_outcome §96-100).

**Transplant shortlist (proven, portable):**
1. **Exit machinery FIRST** — peak_fade/trailing params + the high-peak
   15%-retrace grid result (+41% lift); the only empirically proven edge in
   both PolyApex and gecko history. Rebuilding exits from scratch discards
   the one thing that works.
2. `signal_outcome_ledger` (self-labeling emissions — solves day-one
   measurability, gecko's costliest historical gap).
3. §12a/§12b watchdog + alert-hygiene infra (output-row freshness SLOs,
   parse_mode discipline, suppression-reversal paging).
4. The #466 scarce-slot quality-gate pattern for alert budgets.
5. Paper-engine + checkpoint/peak instrumentation (entry-stack already
   battle-tested).

**Effort: weeks-to-months for the front-end; the transplants cut the back-end
to near-zero.** The unmeasurable-corpus problem means its EV can't be
computed from gecko's data — that is itself a finding: gecko cannot even
*measure* the class it's missing.

---

## The one-line data summary the decision turns on

Where do you want to hunt?
- **CG-native corpus** (measurable, 3/672 ≥10× in 30d, winners score ≤18):
  Option 1 makes gecko alert there within days, feeding proven exits.
- **Launchpad-runner corpus** (ANSEM 477× class): gecko has never seen one at
  launch and cannot with current sources, signals, or cadence — H4+H3+H5
  each independently block it. Options 2 and 3 both mean building a new
  detection front-end; Option 3 does it without gecko's CG-shaped
  constraints, carrying over the five proven components above.

Blank spots an operator VPS run would fill before deciding (scripts ready):
60-day live score distribution, current suppression state (#424 live?),
90-day ledger backfill aggregates, CATCASH/JOTCHUA ever-seen verdicts.

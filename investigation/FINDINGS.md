# gecko-alpha Forensic Investigation — FINDINGS

Date: 2026-07-18 · Read-only investigation · No pipeline code was modified.

**Evidence classes used below** (per claim):
- `[git]` — verified against this repo's git history at origin/master `749882e`.
- `[code]` — file:line at origin/master.
- `[doc]` — committed findings/runbook/design doc (dated prod-DB probes; the
  probe data itself is not re-runnable here).
- `[NEEDS-DB]` — requires the prod `scout.db` / journald; ready-to-run
  read-only collection tools are in `investigation/vps_query_pack.sh`,
  `ledger_backfill.py`, `case_replay.py`.

**Environment limitation:** this session has no SSH to the prod VPS. Every
`[NEEDS-DB]` item has a corresponding query in the collection pack; run it and
the numbers drop straight into the blanks.

---

## 0. Headline findings

1. **The repo's git history is truncated.** Only 56 commits exist, ALL dated
   2026-07 (earliest `1f42c22`, 2026-07-03). Nothing from May or June survives
   in git; that era exists only in code comments and committed markdown. `[git]`
2. **Commit `f2b85e49` does not exist** (`git cat-file -t f2b85e49` → not a
   valid object). The "gate threshold raised above ceiling on 2026-06-01 via
   f2b85e49" claim is wrong in mechanism and unverifiable in attribution — but
   **right in effect**, with the direction inverted: the **2026-06-02
   social-denominator renormalization dropped the reachable score ceiling to
   ~54–59 under an unchanged `MIN_SCORE=65`**. Documented at
   `scout/config.py:116-124` and `docs/gecko-alpha-alignment.md:98-104`:
   "0/1,995 candidates scored, MiroFish unreached (0 jobs since 06-01)." `[code][doc]`
3. **The conviction gate is not mis-tuned today — it is RETIRED.**
   `CONVICTION_GATE_ENABLED=False` (default) at `scout/config.py:125`, shipped
   by PR #440 (`f066e5e`, 2026-07-10). `_run_conviction_gate_and_alert`
   returns immediately, skipping gate.evaluate + MiroFish + send_alert
   (`scout/main.py:1283-1307`). `[git][code]`
4. **The system is currently fully dark end-to-end**: primary ingestion (CG)
   dead since 2026-07-13 16:12Z on Demo-key quota exhaustion (1,559 backoff
   events, zero pages — closed by #465, activation pending); conviction gate
   retired; all newer alert lanes default-OFF (`DETECTION_ALERT_LANE_ENABLED`,
   `TRADE_SURFACE_TG_ALERTS_ENABLED`, `MOVED_ALREADY_POSTMORTEM_ENABLED`).
   `[code][doc: tasks/ops_pack_priorities_2026_07_18.md]`

## 1. Prior findings — verified / refuted

| Prior claim | Verdict | Evidence |
|---|---|---|
| "Alert funnel died 2026-06-01 via commit f2b85e49: gate raised above reachable ceiling (46–59)" | **PARTIAL — effect confirmed, mechanism inverted, attribution unverifiable.** No such commit; no threshold change in visible history (`CONVICTION_THRESHOLD=75`, `MIN_SCORE=65` throughout). The 2026-06-02 renormalization *lowered the ceiling* below the fixed gate. Ceiling band 46–59 confirmed: max observed score 59 (DUMPSTR) over 1,606 contracts 06-21→06-28; only 2 ever exceeded 55; 0 reached 65. | `[git]` log -S sweeps; `[doc]` findings_slow_burn_under_gate_backtest_2026_06_28.md §2-3; `[code]` config.py:112-125 |
| "Frozen-suppression-lock identified; PR #424 built; merge status unknown" | **VERIFIED + resolved: #424 IS MERGED** (`5d5cfb6`, 2026-07-10), staged for operator Deploy #3. **Deploy-to-prod NOT confirmable from the repo** — runbook_gainers_early_revival.md still treats "#424 nightly refresh live" as a precondition to verify. Affected combos: `gainers_early` + `losers_contrarian` (latched mid-May), `chain_completed` (latching ~2026-07-04). | `[git]` 5d5cfb6; `[doc]` tests/fixtures/frozen_lock_chain_completed_snapshot.md, docs/runbook_deploy3_2026_07.md, docs/runbook_gainers_early_revival.md:1-27 · `[NEEDS-DB]` P1.suppression_state query confirms live state |
| "Signal-outcome ledger designed; implementation unknown" | **REFUTED (in a good way): TWO ledgers are IMPLEMENTED.** (a) `source_calls` (design_source_call_outcome_ledger_2026_05_20) shipped as `scout/source_quality/ledger.py`, migration db.py:5300. (b) Newer `signal_outcome_ledger` (`scout/outcome_ledger.py`, 2026-07-02 edge audit) wired at emission sites main.py:1402, engine.py:266/683, signals.py:70. Built precisely because history is uncomputable: "the alerts table has 33 lifetime rows with no usable price, so gate counterfactuals are impossible" (outcome_ledger.py:3-6). | `[code]` |
| "All 24 tokens that achieved 5x+ were detected but never alerted" | **UNSUPPORTED as stated; qualitative core CONFIRMED.** No committed doc contains "24". The rigorous same-asset cohort (n=672 measurable CG-native contracts, 30d) found **3** ≥10× (bitads 49.9×, ANSEM 21×, main-street-usd 11.5×) — **all scored ≤18, none alerted**. An earlier raw "6 winners" list was 4/6 join-contamination artifacts. The real prize class (DEX-mint corpus, ANSEM 477× at DEX stage) is **structurally unmeasurable** (no contract↔coin_id link; 7d snapshot retention). The precise "24" most plausibly lives in `tasks/backlog_fable_analysis_2026_07_10.md` — **which is cited by backlog.md as authoritative but is NOT committed to the repo.** | `[doc]` findings_same_asset_under_gate_cohort_30d_2026_06_28.md §3; findings_slow_burn_under_gate_backtest §5; product_note_trade_now_lane_2026_06_30.md:18-22 |
| "Near-zero avg return at fixed 24h; all realized edge from exit machinery (peak_fade, trailing) vs stop_loss drag" | **CONFIRMED.** All-time closed 1,396 trades: +$0.48/trade avg (near-zero). Edge concentrates in giveback control: `<5pp` giveback bucket n=139 → **+$8,655 (100% win)**; stop_loss bucket → −$2,632 over 32 trades in the gainers_early window; expired/stop_loss largest bucket 30d: **567 trades, −$9,857**. peak_fade holds 4× longer with 5× less giveback than trailing (at ≥50% peaks: 10.2pp vs 50.8pp giveback). High-peak counterfactual: tighter peak-fade = +41% lift. | `[doc]` findings_profit_patterns_2026_05_19.md; findings_sustain_winners_cut_losers_2026_05_11.md §3-4; findings_high_peak_giveback.md §0/§4/§11; findings_gainers_early_autosuspend_attribution_2026_05_29.md |

## 2. Phase 0 — state reconstruction

**Git since 2026-05-15:** impossible as scoped — history starts 2026-07-03.
The 56 July commits are the #419–#466 wave (gate retirement #440, dispatch
quarantine #437, frozen-lock fix #424, live-trading LIVE-0x, watchdogs). `[git]`

**What is running right now** `[doc + NEEDS-DB]`: repo evidence says the
pipeline loop may be up but its primary source (CG) has been quota-dead since
2026-07-13; `trending_snapshots` writer dead since then; conviction gate
retired; MiroFish idle since 06-01. Live process/cron state: run
`vps_query_pack.sh` sections P0.processes / P0.last_writes_per_stage /
P0.last_alert_ever_by_lane.

**Gate vs score distribution:**
- Gate config `[code]`: `MIN_SCORE=65` (config.py:112, quant floor at
  gate.py:37), `CONVICTION_THRESHOLD=75` (config.py:113), formula
  `quant*0.6 + narrative*0.4` (gate.py:49-71), gate flag OFF (config.py:125).
- Scorer structure `[code]`: 12 divisor signals, raw max 193 (scorer.py:25,80);
  divisor derived over active capability set (PR #450) → **168 in prod**
  (holder_growth's 25 excluded: no MORALIS key); normalized
  `min(100, pts*100/divisor)` then ×1.15 co-occurrence (scorer.py:345-351).
- Empirical distribution `[doc]` (06-21→06-28, n=1,606): bands 0-9: 648 ·
  10-24: 675 · 25-39: 169 · 40-54: 112 · 55-64: 2 · **65+: 0**. Max 59.
- **Confirmed: the gate sat ~6-19 points above anything the corpus could
  produce; post-#450 renormalization (~×1.15) still leaves the max ≈68-only-
  in-theory; measured winners scored ≤18.** 60-day refresh: `[NEEDS-DB]`
  P0.score_distribution_60d.

## 3. Phase 1 — funnel forensics (trailing-60d shape)

Committed-evidence funnel (exact 60d counts: `[NEEDS-DB]` P1.funnel_60d):

| Stage | Count / rate | Evidence |
|---|---|---|
| Ingested | ~289 tokens/cycle mean (max 367), 1,440 cycles/day | `[doc]` findings_cycle_change_audit_2026_05_13.md:69 |
| Scored (distinct contracts) | 1,606 / week | `[doc]` slow_burn backtest §2 |
| **Signaled** (score ≥ MIN_SCORE=65) | **0** | same, §2 |
| **Gated** (conviction evaluated) | **0** ("the gate never executed once all week") | same, §1 |
| **Alerted** (conviction lane) | **0 since 2026-06-21**; 33 lifetime rows; MiroFish 0 jobs since 06-01; 10 spurious "Conviction Score: N/A" sends (killed by #440) | `[doc]` slow_burn:70; outcome_ledger.py:3-6; config.py:118-120 |
| Paper-open TG lane (parallel path) | rejects ~99.99% of scored candidates upstream at dispatch | `[doc]` design_detection_time_alert_lane.md:16-23 |

**Where flow dies: the SCORING→GATE boundary.** Not at ingestion (289/cycle),
not at dispatch plumbing (the plumbing was never reached). Since #440 the
boundary is formalized: the gate no-ops.

**Suppression store:** three combos latched pre-#424 (`gainers_early` — the
best-performing lane, dark ~7.5 weeks behind TWO independent kills:
signal-level auto-suspend 2026-05-19 + combo suppression 2026-06-12 →
`parole_exhausted`; `losers_contrarian`; `chain_completed`). The auto-suspend
itself was **justified** (regime shift, −$18.40/trade) per
findings_gainers_early_autosuspend_attribution_2026_05_29.md:116. Current
frozen entries: `[NEEDS-DB]` P1.suppression_state.

## 4. Phase 2 — ledger backfill

Historical gate counterfactuals are **impossible from the alerts table** (33
rows, no usable price) — this is why `signal_outcome_ledger` was built
(2026-07-02) to self-label all FUTURE emissions. For the trailing-90d
paper-trade record (the signal-fire record that does have price paths:
checkpoints 1h/6h/24h/48h + peak), run
`investigation/ledger_backfill.py --db scout.db --days 90 --out ledger.csv`
`[NEEDS-DB]`. It emits per-signal outcome rows (max multiple, time-to-peak
best-effort, realized-vs-fixed-24h) plus an aggregate footer. Smoke-tested
against a synthetic DB in this session.

## 5. Obvious-but-unfixed defects (NOT fixed, per brief)

1. **`holder_growth` is a phantom signal** — 25 pts (13% of divisor) dead in
   prod: capability-gated on `MORALIS_API_KEY`, unset; 0/491 Solana rows had
   holder data. PR #450 removed it from the divisor rather than funding the
   key. `[code]` scorer.py:73-75,110-122 · `[doc]` spec_holder_growth:62-65.
2. **Gate retired instead of recalibrated** — with the measured winners
   scoring ≤18, recalibrating to p99 (~45-55) would STILL catch ~none of the
   winners; the detection-lane quality gate (#466, quant≥1, 8/10 recall on
   early catches) is the working replacement pattern. `[doc]` PR #466 body,
   findings_same_asset §3.
3. **`tasks/backlog_fable_analysis_2026_07_10.md` is uncommitted** yet cited
   as the authoritative tracker by backlog.md and config comments
   (config.py:581 region). Single-disk-failure risk + unverifiable claims
   (e.g. the "24 winners"). `[git]`
4. **CG Demo key quota exhaustion had zero alerting** until #465 (merged
   2026-07-17, activation still pending operator `cron/deploy.sh`). `[doc]`
5. **7-day retention destroys evidence** — `gainers_snapshots` prune makes
   ANSEM-class postmortems impossible retroactively; the recorder built for
   this (#459, DASH-05) is default-OFF, so evidence is evaporating daily.
   `[code]` moved_already.py:23-25, config.py:582.
6. **#424 deploy-to-prod unconfirmed** — merged 2026-07-10; runbook still
   phrases the nightly refresh as a precondition to verify. `[doc]` §3 above.

## 6. Current-state overlay (July)

Even a perfect gate would alert on nothing today: CG (the only source feeding
the trending/gainers lanes) is quota-dead since 2026-07-13, and the newer
detection lane — the one lane with validated early-catch recall (8/10) — is
default-OFF pending #466 + operator flag flip. Restoration sequence and
command packs: `tasks/ops_pack_priorities_2026_07_18.md`.

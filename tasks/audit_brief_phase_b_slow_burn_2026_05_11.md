# Audit setup brief — Phase B bootstrap-CI on slow_burn cohort

**Date:** 2026-05-11
**Status:** LOCKED. Audit technical setup against the locked brief; do NOT perform evaluation comparison before D+14 (2026-05-24).
**Reconstruction note:** Original brief (created 2026-05-11 earlier in same session) was lost during branch switches between sessions. This is a reconstruction from conversation history; locked criteria (§2, §3) are reproduced verbatim from the original lock. Cross-reference with `tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md` for implementation; cross-reference with `tasks/findings_silent_failure_audit_2026_05_11.md` for the parallel table-freshness watchdog workstream.

---

## §1 — Purpose

Phase B (slow_burn watcher) shipped 2026-05-03; first detection batch landed 2026-05-10T02:23Z. D+14 evaluation (2026-05-24) needs bootstrap-CI on per-detection forward outcomes to decide Phase C (paper-trade dispatch for the CG-markets-watcher corpus). Source data (`volume_history_cg`) is rolling-pruned at 7 days; without snapshot infrastructure, the data won't survive to D+14. This brief locks the audit operationalization so it runs deterministically at D+14 against the locked criteria below.

---

## §2 — Soak extension criteria + bootstrap-CI gate (locked 2026-05-11)

**Locked before evaluation rate is observable. Extension trigger is sample size, NOT result direction.**

```
D+14 evaluation (2026-05-24):
  IF Phase B has produced ≥35 detections with ≥48h post-detection outcome data:
    → evaluate against bootstrap-CI gate (see below)
  IF Phase B has produced <35 detections:
    → extend soak by 14d to D+28 (2026-06-07)
    → extension trigger is "insufficient sample," NOT "results look promising/disappointing"

D+28 evaluation (2026-06-07):
  IF still <35 detections:
    → escalate as separate finding ("slow_burn fire rate insufficient to validate
      within soak window") rather than further extension
    → soak window itself is in question; re-evaluate signal viability

Bootstrap-CI gate (applies at evaluation time regardless of which D+ window lands):
  Compute 95% bootstrap CI (10,000 resamples) on per-detection forward outcomes:
    - peak_achieved_pct (max % above entry within 14d post-detection)
    - hold_time_to_peak_hours
    - peak_to_exit_giveback_pct
  Promote to Phase C IF:
    - 90% lower bound of peak_achieved_pct distribution ≥ 30%
    - median hold_time_to_peak ≤ 168h (1 week)
    - 90% lower bound of (peak - giveback) ≥ 15%
  Otherwise: do NOT promote. Document as observation-only signal; revisit at next
  iteration.

Why 90% lower bound, not 80% or 95%:
  - 95% is overly conservative for n≈35; CI width swamps signal
  - 80% under-weights tail risk for paper-trade promotion
  - 90% is the standard middle ground for n=30-50 with bootstrap; locks now
```

**Discipline against post-hoc threshold relaxation:** if D+14 results land at boundary (close to but not over the 90% lower bound), the soak-extension rule informs *whether to extend soak vs revert*, NOT whether to retroactively change the threshold.

---

## §3 — D+14 result interpretation framework (locked 2026-05-11, before audit setup)

Phase B's actual cohort accumulation rate (56 detections in ~36h as of lock time) is materially higher than the design-time assumption ("slower accumulation"). The §2 bootstrap-CI gate operates correctly on per-detection forward outcomes regardless of rate, but rate is itself information about detector behavior. The D+14 result must be interpreted against both axes — gate outcome AND cohort rate — not gate outcome alone.

Four-quadrant interpretation matrix:

| Gate result | Cohort rate at D+14 | Interpretation | Phase C action |
|---|---|---|---|
| Pass | ≥100 detections / 14d | Real signal at meaningful volume | Phase C ships; capital allocation scales to rate |
| Pass | <100 detections / 14d | Real signal but scarce | Phase C ships; capital allocation reflects opportunity density |
| Fail | ≥100 detections / 14d | Detector too loose; many false positives carrying few real winners | Phase C does NOT ship as-is; re-scope detector tightness as separate finding |
| Fail | <100 detections / 14d | Scarce signal AND noisy | Phase C does NOT ship; signal itself in question; escalate as architectural finding |

The 100/14d threshold is set at ~2× the design-time expected accumulation. Functionally, given Phase B's observed first-36h accumulation rate (12-44/day → projected 168-616 by D+14), this threshold operates principally as a **tripwire for rate collapse mid-soak** rather than as a discrimination between "abundant" and "scarce" signal. If detector behavior changes during soak (regime shift, upstream throttling, ingest filter mutation), rate could drop below 100/14d and signal "something changed; treat the cohort as scarce" regardless of forward-outcome math. The threshold is intentionally conservative: above-100 is the expected operating regime; below-100 indicates the cohort is not what design assumed.

**Locked elements (do NOT relax at D+14 evaluation):**

- Gate pass/fail criterion stays as §2 bootstrap-CI lower bound (90% lower bound on per-detection peak_achieved_pct ≥ 30%)
- Cohort rate threshold stays at 100/14d
- Quadrant-to-action mapping above is final
- "Fail + high rate" does NOT auto-promote to "marginal pass; ship Phase C with caveats" under any framing
- "Pass + low rate" does NOT auto-promote to "scarce signal, withhold Phase C until rate increases"

The framework's purpose is to prevent post-hoc rationalization at D+14. Both axes are observable in the data; both axes are locked in interpretation.

**Boundary handling:**

- If gate result lands at the bootstrap-CI boundary (within ±10% of the §2 threshold), extend soak by 14 days to D+28 per §2 extension rule; do not interpret as either pass or fail
- If cohort rate lands within 90-110 detections/14d range, choose the lower-rate interpretation (treat as "scarce") rather than the higher-rate interpretation. The bias toward conservative interpretation at boundaries is intentional

**This framework is locked before audit technical setup begins.** Audit setup will naturally surface rate × outcome correlations as a side effect of correctness checks; locking the framework first prevents those observations from contaminating the interpretation logic.

---

## §4 — Data path correctness (§9c discipline)

- For each detection in `slow_burn_candidates` since 2026-05-10T02:23Z, verify that `audit_volume_snapshot_phase_b` has continuous coverage from `detected_at` through `detected_at + 48h`
- Report count of detections with complete coverage vs incomplete; bootstrap-CI runs on complete-coverage subset only
- If incomplete-coverage rate >10% of detections, escalate as data-path issue before running bootstrap (do not silently subset)

**Data-source note:** `audit_volume_snapshot_phase_b` is populated by the daily snapshot job per `tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md`. The job mirrors `volume_history_cg` rows for slow_burn-detected coin_ids in the soak window into a non-pruned table. The original brief specified `price_cache` as the data source; that was corrected post-§9c-discipline-check 2026-05-11 — `price_cache` is current-snapshot-only with no historical data.

---

## §5 — Bootstrap framework

- Use BCa (bias-corrected accelerated) bootstrap, `scipy.stats.bootstrap` with `method='BCa'`
- 10,000 resamples
- 90% confidence interval (matches §2 90% lower bound criterion)

Rationale for BCa: handles skewed distributions correctly (per-detection peak distributions are long-tail with mass near zero — exactly the shape BCa exists for). Percentile bootstrap is biased for skewed n≈35; basic bootstrap can produce CIs outside the observed range. BCa is the discipline standard for small-n bootstrap on financial-return-shaped data.

---

## §6 — Outcome metric operationalization

- Outcome window: `[detected_at, detected_at + 48h]` (closed-open interval)
- Peak: max of `price` field from `audit_volume_snapshot_phase_b` rows within window (flat-price, single sample per minute — see §8 tail validation for OHLC peak-fidelity check)
- Detection-time price: `price` of the `audit_volume_snapshot_phase_b` row at `recorded_at` immediately ≥ `detected_at` (or the closest available if no exact match)
- `peak_achieved_pct = (peak - detection_time_price) / detection_time_price × 100`
- No slippage/fees modeled in (paper-trade convention; matches §2)
- Sample cadence: `audit_volume_snapshot_phase_b` captures rows from `volume_history_cg` which is written every pipeline cycle (~60s). Granularity used per detection (1-minute typical; coarser if any gap) must be reported alongside peak_achieved_pct (do not silently mix granularities in the bootstrap input distribution)
- Data gaps: if any sample minute within window has no row for the detection's coin_id, detection counts as incomplete-coverage per §4. Audit must report gap-rate distribution (% of expected sample-minutes missing, per detection) as a separate output alongside coverage report

---

## §7 — Cross-token gap analysis (dropout vs ingest interruption)

`volume_history_cg` writer (`scout/spikes/detector.py:record_volume`) silently skips coins that aren't in `_raw_markets_combined` (top-N CG markets response). Per-token dropout (token exited top-500-by-vol) looks identical to system-wide ingest pause at single-token analysis. Audit must distinguish:

- For each detection's 48h window, compute per-token gap count (sample-minutes where no row exists for THIS coin_id but at least one row exists for some other coin_id in the cohort within the same minute-bucket)
- Tag gaps as `per_token_dropout` (other tokens have rows, this one doesn't) vs `system_wide_pause` (no tokens have rows in the minute-bucket)
- Report per-detection breakdown: `gap_minutes_total / per_token_dropout_minutes / system_wide_pause_minutes`

The gap-rate distribution (from §9 Outputs) is broken down by tag.

---

## §8 — Tail validation (B3 hybrid spec)

The primary CI runs on flat-price peak from `audit_volume_snapshot_phase_b` (60s cadence; intra-poll spikes bounded to ≤60s gaps). To validate the peak-fidelity assumption on the load-bearing subset:

- Identify the top-N detections that drive the bootstrap-CI lower bound on `peak_achieved_pct` (suggest N = 15 OR top 25% of bootstrap-CI-lower-bound-driving detections, whichever is smaller)
- For each of those N detections, query CG `/coins/{id}/ohlc?days=2` at audit time and recompute `peak_achieved_pct` from the OHLC `high` field
- Report divergence: `flat_price_peak_pct vs ohlc_peak_pct` per detection, plus summary stats (mean divergence, max divergence, count of detections where ohlc_peak > flat_peak by >10%)
- If divergence exceeds 10% for >30% of validated detections, escalate as a finding (flat-price peak materially under-reports OHLC peak for the load-bearing subset); primary CI lower bound is reported with the caveat

CG rate limit: 30 req/min free tier. N=15 fits comfortably in one minute.

---

## §9 — Outputs

- Per-detection peak_achieved_pct (full distribution)
- Bootstrap CI [lower, upper] at 90% confidence (BCa method)
- Bootstrap median
- Coverage report: N complete / N incomplete / N total
- Gap-rate distribution: histogram of % expected sample-minutes missing per detection, broken down by `per_token_dropout` vs `system_wide_pause` tag (per §7)
- Granularity breakdown: count of detections at 1m granularity vs fallback granularity
- Cohort rate report: detections per 7d (rolling) and detections per 14d cumulative
- Tail validation summary (per §8): N validated detections, mean/max OHLC-vs-flat divergence, escalation flag

---

## §10 — Scope limitations (locked; do NOT widen post-lock)

**Snapshot scope captures `[detected_at, detected_at + 48h]` window only.** Pre-detection ramp-up context (rows before each detection's `detected_at`) is **not snapshotted** and is subject to the 7-day rolling prune. Any future audit work requiring pre-detection trajectory data (e.g., characterizing volume-ramp shape before slow_burn fires) needs a separate snapshot job with widened scope; **do NOT widen this snapshot's scope post-lock.**

Rationale: the locked audit gate (§2) evaluates per-detection forward outcomes only; pre-detection context is not load-bearing for the Phase C ship/no-ship decision. Widening scope post-lock would violate the pre-registered spec. Future audits that need pre-detection context are separate workstreams with their own pre-registration.

---

## §11 — Data-source lock (pre-registered 2026-05-11)

Primary data source: `audit_volume_snapshot_phase_b` (populated by daily snapshot job per `tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md`). Tail validation source: CG `/coins/{id}/ohlc?days=2` at audit time.

**At D+14 evaluation, audit runs against available data with explicit reporting of what's missing.** Coverage gaps in `audit_volume_snapshot_phase_b` (snapshot job failures, missed days, etc.) become a finding about the audit infrastructure — NOT a reason to extend the soak or defer evaluation. Soak extension is triggered ONLY by sample size (<35 detections per §2), never by data-quality concerns at audit time.

This composes with the §2 extension rule: data quality is reported as a finding; sample size triggers extension; evaluation runs at D+14 against whatever data exists.

---

## §12 — What this brief does NOT do

- Compare results to §2 threshold (that happens at D+14, not before)
- Apply §3 interpretation framework (locked separately, applied at D+14)
- Make any Phase C ship/no-ship decision

The "what this brief does not do" section is the load-bearing part — it prevents the audit from drifting into evaluation prematurely.

---

## §13 — Engineer handoff notes

- Run audit setup at any time between now and 2026-05-24
- Audit setup is independent of evaluation comparison; evaluation runs strictly on D+14 against locked criteria (§2 + §3)
- If audit setup reveals data-path issues (>10% incomplete coverage, unexpected granularity mixing, unexpected gap-rate clusters), escalate as a data-path finding *before* running the bootstrap. Do not silently work around.
- If gap-rate distribution shows the strict-coverage rule eats >50% of the cohort, surface as separate finding for re-lock decision next session; do NOT relax the rule mid-audit.

---

## §14 — End-of-soak runbook

**2026-05-25 (D+15):** final scheduled run of `gecko-audit-snapshot.service` completes at 04:00 UTC. After confirming the final run captured the last possible forward-outcome data, operator action:

```bash
ssh root@89.167.116.187 'systemctl disable --now gecko-audit-snapshot.timer && systemctl disable --now gecko-audit-snapshot-watchdog.timer && systemctl list-timers gecko-audit-snapshot* --no-pager' > .ssh_disable_audit.txt 2>&1
```

Verify both timers show as `inactive` / `disabled`. Read `.ssh_disable_audit.txt` to confirm.

**Table preservation:** `audit_volume_snapshot_phase_b` is preserved indefinitely as the audit artifact. Estimated final size: 75-300 MB (per-detection × ~3K rows × 14 days × cohort size; see plan Task 7 disk pre-flight). Static after end-of-soak — no further writes. Consider archiving to a backup table if disk pressure rises.

**Reminder host:** this runbook step lives at the top of the audit brief (this doc) AND in the plan's Task 7 Step 12. The operator should see it at the natural D+14 evaluation moment when they read the brief to run the audit.

---

## §15 — Cross-references

- `tasks/plan_audit_volume_snapshot_phase_b_2026_05_11.md` — implementation plan for the snapshot infrastructure
- `tasks/findings_silent_failure_audit_2026_05_11.md` (same date) — parallel discipline workstream proposing a table-freshness watchdog daemon. When that ships, `audit_volume_snapshot_phase_b` should be on its monitored-tables list — provides defense-in-depth against stacked-failure mode (snapshot job dies + snapshot-job watchdog also dies).
- Global CLAUDE.md §9c (Post-hoc attribution discipline) — codified four-instance lever-vs-data-path pattern. §4 data-source correction in this brief (price_cache → audit_volume_snapshot_phase_b) is a fifth instance of the pattern, surfaced at audit-setup-verification time rather than at audit-run time.

**Historical-reference preservation note:** §4 and §15 retain `price_cache` references as historical context for the §9c lever-vs-data-path instance this brief is an artifact of. Do not scrub mechanically; these references are documentation of the discipline at work, not specifications of behavior. Specifications use only `audit_volume_snapshot_phase_b`.

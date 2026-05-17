**New primitives introduced:** NONE.

# losers_contrarian Revival-Criteria Tightening — Empirical Findings (2026-05-17)

**Source:** srilu-vps `/root/gecko-alpha/scout.db` snapshot pulled 2026-05-17.
**Companion:** `tasks/plan_lc_revival_criteria_tightening.md` (v3), `tasks/design_lc_revival_criteria_tightening.md`, `tasks/baselines_revival_criteria_2026_05_17.md`.

> **>>> WARNING <<<**
>
> The `gainers_early` evaluation in this doc is **FALSIFICATION-RISK ANALYSIS ONLY**. Do NOT use this output to drive `gainers_early` state changes without re-running the evaluator against current data AND obtaining explicit operator approval. Operator constraint: "do not change gainers_early behavior unless the evidence clearly supports it."

## TL;DR

The new evaluator correctly rejects two of the four signals it was run against (`gainers_early` FAIL, `losers_contrarian` STRATIFICATION_INFEASIBLE — **LC was suspended today, so post-cutover regime cannot yet be evaluated; re-run in ≥7d**) and produces clear "wait" verdicts for the other two (`chain_completed` and `volume_spike`, both BELOW_MIN_TRADES). The headline finding: the 2026-05-13 `keep_on_permanent` audit-id=24 verdict on `gainers_early` is **contradicted** by the new evaluator — both stratified windows show negative bootstrap-LB per-trade-pnl, and the ATTENTION block fires automatically printing the revoke SQL.

## losers_contrarian evaluation

```
=== Revival criteria evaluation: losers_contrarian ===
Total closed trades: 289
Cutover: 2026-05-17T01:02:46.345864+00:00 (0d ago) [source: signal_params_audit:auto_suspend:tg_alert_eligible]
>>> Cool-off status: CLEAR
Verdict: STRATIFICATION_INFEASIBLE

Failure reasons:
  - cutover at 2026-05-17T01:02:46.345864+00:00 cannot split into two >= 7d / >= 50-trade windows
```

**Read:** The most-recent regime cutover for LC is today's 2026-05-17T01:02Z auto_suspend (combined-gate hard_loss). Zero days of post-cutover data exist, so window B is empty. **The evaluator correctly refuses to evaluate.** Operator must wait for post-cutover data to accumulate (Task 11 fold D#6 helper not applicable here because cutover is current, not historical — the BELOW projection helper only fires when n is the gate).

If operator wants to evaluate against an earlier cutover (e.g., the 2026-05-06T02:13Z LC operator-revival), they can pass `--cutover-iso 2026-05-06T02:13:00Z`. But per design-review fold C#6, operator-revival rows are EXCLUDED from automatic cutover-detection because they're outcomes of regime decisions, not regime decisions themselves.

## gainers_early evaluation (FALSIFICATION-RISK ANALYSIS — DO NOT ACT)

> **>>> WARNING (re-stated next to the SQL below) <<<**
> The FAIL output for `gainers_early` below CONTAINS A `sqlite3 INSERT` revoke statement. Per operator constraint, **do NOT paste** it. The signal is heading the right direction (per-trade improved $-3.13 → $-0.70 across the cutover); auto-revocation would be premature. The evaluator provides evidence; the operator chooses whether to act.

```
=== Revival criteria evaluation: gainers_early ===
Total closed trades: 434
Cutover: 2026-05-04T01:01:02.736271+00:00 (13d ago) [source: signal_params_audit:auto_suspend:enabled]
>>> Cool-off status: CLEAR
Verdict: FAIL

Failure reasons:
  - window_a.per_trade_bootstrap_lb=$-11.59 <= 0
  - window_a.win_pct_wilson_lb=41.5% < 55.0%
  - window_b.per_trade_bootstrap_lb=$-8.88 <= 0
  - window_b.win_pct_wilson_lb=51.6% < 55.0%

>>> ATTENTION: existing soak_verdict='keep_on_permanent' at 2026-05-13T04:05:02.142Z is CONTRADICTED by current FAIL.
>>> To revoke, run:
sqlite3 <db> "INSERT INTO signal_params_audit(...) VALUES('gainers_early', 'soak_verdict', 'keep_on_permanent', 'revoked', ...);"

Window A: 2026-04-21 → 2026-05-03 (n=194)
  net=$-607.97  per_trade=$-3.13  win%=48.5
  win_pct_wilson_lb=41.5%  per_trade_bootstrap_lb=$-11.59
  no_breakout_and_loss_rate=0.31
  stop_loss_frequency=0.22
  expired_loss_frequency=0.27
  exit_machinery_contribution=0.74

Window B: 2026-05-04 → 2026-05-17 (n=240)
  net=$-168.36  per_trade=$-0.70  win%=57.9
  win_pct_wilson_lb=51.6%  per_trade_bootstrap_lb=$-8.88
  no_breakout_and_loss_rate=0.24
  stop_loss_frequency=0.15
  expired_loss_frequency=0.19
  exit_machinery_contribution=0.95
```

**Read:** Both stratified windows produce negative bootstrap-LB on per-trade pnl. The 2026-05-04 auto_suspend event is the cutover; window B (post-suspend) is the period that produced the 2026-05-13 `keep_on_permanent` verdict + the 4 days after. Net P&L is improving across the cutover ($-3.13 → $-0.70 per trade) but the bootstrap LB on post-cutover data still includes 0 — the signal is not statistically distinguishable from a noisy zero-expectancy process.

**Secondary diagnostics:**
- `no_breakout_and_loss_rate` improved (0.31 → 0.24, both within the healthy max of 0.40) — feedstock breakout rate is fine
- `stop_loss_frequency` dropped (0.22 → 0.15) — losses are less frequent in the new regime
- `exit_machinery_contribution` improved (0.74 → 0.95) — winners are exit-machinery-driven, not entry luck
- `win_pct` improved (48.5% → 57.9%) — visible improvement at point-estimate

The qualitative picture: gainers_early is **getting better** but not by enough margin for a `keep_on_provisional` verdict. The 2026-05-13 verdict was likely operator-eyeballed against the post-cutover data + soak rules that did not require Wilson/bootstrap LB. **The same falsification class the originating 2026-05-13 LC verdict belongs to.**

**Recommendation (operator decision):** Defer revocation pending explicit operator review. The signal is heading the right direction; auto-revocation would be premature. The new evaluator provides the evidence; the operator chooses whether to act.

## chain_completed evaluation

```
Total closed trades: 12
Verdict: BELOW_MIN_TRADES

Failure reasons:
  - n_trades=12 < REVIVAL_CRITERIA_MIN_TRADES=100

>>> Estimated re-evaluable in ~205.3 days (need 88 more trades; recent rate = 0.43/day)
>>> Note: PASS additionally requires >= 7d AND >= 50 trades on BOTH sides of cutover.
```

**Read:** Strong-EV signal (per the 2026-05-17 morning checkpoint Q2: 12 trades, +$1,296, +$108/trade, 83.3% win) but the trade-count floor of 100 means the evaluator cannot produce a verdict for another ~6.8 months at current fire rate. The Tier 1a chain dispatch architecture is working but produces too few high-conviction events for verdict-stamp eligibility. **Acceptable — the floor is doing its job.**

## volume_spike evaluation

```
Total closed trades: 36
Verdict: BELOW_MIN_TRADES

Failure reasons:
  - n_trades=36 < REVIVAL_CRITERIA_MIN_TRADES=100

>>> Estimated re-evaluable in ~56.0 days (need 64 more trades; recent rate = 1.14/day)
```

**Read:** Same shape as chain_completed but higher fire rate; projected ~8 weeks to eligibility.

## Cross-reference

- `backlog.md:1595` — originating scope. Item 5 ("post-verdict monitoring") implemented via `keep_on_provisional_until_<iso>` rename (active watchdog enforcement is follow-up `BL-NEW-REVIVAL-VERDICT-WATCHDOG`).
- Memory `feedback_lever_vs_data_path_pattern.md` — the gainers_early result IS the 6th instance of the §9c pattern: the visible lever (audit-id=24 `keep_on_permanent`) is what an operator might rely on, but the *actual* statistical truth (bootstrap LB still negative) only surfaces when you make the data path do the work via Wilson + bootstrap.
- Memory `feedback_pre_registered_hypothesis_anchoring.md` — the v2 plan's median-split was the satisfying-frame; Reviewer A pushed past it; the v3 cutover-stratified split is what survived the falsification check.
- CLAUDE.md §11b — bootstrap CI + Wilson LB are the project-standard existing-data battery, now first-class in the evaluator.

## Validation evidence (test suite)

- `tests/test_revival_criteria.py` — **48 tests, all passing** on srilu Linux Python 3.12.3 / pytest 8.4.2.
- Adjacent regression suite (`pytest -k "trading or signal_params or auto_suspend or revive or revival or calibrate or config"`) — **506 tests pass, 3 pre-existing env-coupled failures unrelated to revival_criteria** (`test_check_config_prints_resolved_values`, `test_live_mode_defaults_to_paper`, `test_coingecko_config_defaults` all fail because srilu .env overrides defaults).

## Follow-ups filed (Task 14 will add to backlog.md)

- `BL-NEW-REVIVAL-VERDICT-WATCHDOG` — active enforcement of `keep_on_provisional_until_<iso>` expiry (replace the manual revoke flow with automatic alert/revocation at expiry)
- `BL-NEW-REVIVAL-CRITERIA-QUARTERLY-RECALIBRATION` — periodic re-derivation of healthy-signal baselines (per Reviewer D #10)
- `BL-NEW-EVALUATION-HISTORY-PERSISTENCE` — persist evaluator runs to DB (not just structlog) per Reviewer C #16
- `BL-NEW-REVIVAL-CRITERIA-PER-SIGNAL-TUNING` — per-signal Settings overrides if global gates prove too strict/lenient for specific signal classes

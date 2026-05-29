# TG Alert Qualification — Baseline Findings (2026-05-29)

**Purpose:** Capture the measurements required by PR #296 to gate the build PR for `BL-NEW-TG-ALERT-QUALIFICATION-DESIGN`. Read-only diagnostic on srilu prod. No runtime changes.

**Soak gate context:** Tracker-promotion soak gate (PR #281) cleared. The data exists to qualify alerts; the question now is whether the additional PR #296 inputs are measured enough to gate a build.

## 1. Soak Gate — CLEARED

PR #281 gate criterion: `≥5 unique tracker-promoted coin_ids/day for ≥3 mature UTC days` OR 14-day calendar backstop (2026-06-09).

14-day distribution from `gainers_comparisons` excluding open paper_trades:

| utc_day | unique_tracker_promoted |
|---|---|
| 2026-05-29 (today, partial) | 22 |
| 2026-05-28 (mature) | 30 |
| 2026-05-27 (mature) | 28 |
| 2026-05-26 (mature) | 20 |
| 2026-05-25 (mature) | 30 |
| 2026-05-24 (mature) | 21 |
| 2026-05-23 (mature) | 9 |
| 2026-05-22 (mature) | 22 |
| 2026-05-21 (mature) | 18 |
| 2026-05-20 (mature) | 23 |
| 2026-05-19 (mature) | 20 |
| 2026-05-18 (mature) | 9 |
| 2026-05-17 (mature) | 15 |
| 2026-05-16 (mature) | 13 |
| 2026-05-15 (mature) | 22 |

**Mature days observed: 14. Minimum: 9. Median: ~22. All ≥9, far above the 5/day floor.** Gate is structurally cleared with significant headroom.

**Critical caveat (operator-pinned):** soak-clear is **eligibility only**, NOT approval to send alerts. The remaining PR #296 inputs gate the build decision.

## 2. Current TG Alert Volume Baseline (PR #296 input 1 — MEASURED)

`tg_alert_log` per-day, outcome ∈ {sent, announcement_sent, m1_5c_announcement_sent}, last 14d:

| utc_day | sent |
|---|---|
| 2026-05-29 (partial) | 3 |
| 2026-05-28 | 13 |
| 2026-05-27 | 11 |
| 2026-05-26 | 13 |
| 2026-05-25 | 15 |
| 2026-05-24 | 9 |
| 2026-05-23 | 18 |
| 2026-05-22 | 24 |
| 2026-05-21 | 10 |
| 2026-05-20 | 19 |
| 2026-05-19 | 12 |
| 2026-05-18 | 22 |
| 2026-05-17 | 33 |
| 2026-05-16 | 34 |
| 2026-05-15 | 37 |

- **14-day mature-day average: 19.3/day**
- **Recent 7-day average: 14.7/day** (declining trend)
- **Earlier 7-day average: 23.9/day**

Distribution by `signal_type` (last 14d, outcome=sent):

| signal_type | sent count |
|---|---|
| narrative_prediction | 113 |
| gainers_early | 76 |
| volume_spike | 54 |
| losers_contrarian | 30 |
| **Total** | **273** |

Blocked-not-sent (informational — these alerts were prevented):

| signal_type | outcome | count |
|---|---|---|
| chain_completed | blocked_eligibility | 115 |
| tg_social | blocked_eligibility | 11 |
| gainers_early | blocked_cooldown | 1 |
| volume_spike | blocked_cooldown | 1 |

Upstream narrative inbound (informational, not alerts): 868 inbound tweets in 14d via `narrative_alerts_inbound`. Most do not trigger alerts; downstream classifier filters.

**Implication for scarcity target:** Operator-pinned target is **3-5/day max**. Current sent volume is **14-37/day** depending on the day, with mature 14-day average of **19.3/day** — roughly **4-6× over the scarcity target**. Build PR must compress sent volume by that ratio.

## 3. Per-Corpus Volume Split (PR #296 input 3 — PARTIALLY MEASURED)

Today's Focus surfaces two corpora: `paper` (existing paper_trades) and `tracker` (promoted via PR #281). The `tg_alert_log` table indexes by `signal_type`, NOT by `source_corpus`. Reconciliation:

| signal_type | mapped corpus | sent (14d) |
|---|---|---|
| narrative_prediction | paper-side (signal scored against scorer corpus) | 113 |
| gainers_early | paper-side | 76 |
| volume_spike | paper-side | 54 |
| losers_contrarian | paper-side | 30 |
| **paper-corpus total** | | **273** |
| tracker-promoted | **NO ALERTS** — promotion path is dashboard-only per PR #281 anti-scope | 0 |

**Finding:** all 273 sent alerts in last 14d are paper-corpus. The tracker corpus (which generated 9-30 unique coin_ids/day per the soak data) does NOT currently produce alerts. The build PR would need to either (a) extend alert qualification to tracker corpus or (b) explicitly defer tracker-alert work.

## 4. Operator-Action / Noise Baseline (PR #296 input 2 — UNKNOWN)

**Required by PR #296:** the proportion of currently-sent alerts the operator finds useful vs ignores ("operator-acted vs ignored").

**Available data:** none. The `tg_alert_log` records dispatch outcome but does NOT record operator response. There is no read-receipt telemetry; no thumbs-up/down; no manual labeling.

**Possible proxies:**
- Time-to-paper-trade-open on the same `token_id` after alert (would suggest operator-acted) — but the paper-trade path is automated, not operator-driven, so this is not a clean proxy.
- Telegram delete/edit signal — Telegram API exposes operator dismissal/forwarding, but gecko-alpha does not poll this.
- Hand-labeled sample over last 14 days — operator could mark each of the 273 alerts as acted/ignored, but at 19/day operator-survey would take meaningful time and is subjective.

**Status: UNKNOWN. Not measurable without new instrumentation or operator self-report.**

## 5. Scarcity Target (PR #296 input 4 — PINNED)

Operator-pinned **3-5 alerts/day max** (analyst exchange 2026-05-28). The build PR must demonstrate this compression on the current 19.3/day baseline (≈4-6× reduction) using only inputs available at decision time (no future-runner labels).

## 6. PR #296 Input Summary

| Input | Status | Rationale |
|---|---|---|
| Soak gate clearance | **CLEARED** | 14 mature days, all ≥9, median ~22 |
| Current TG alert volume baseline | **MEASURED** | 19.3/day mature 14d avg; per-signal breakdown above |
| Operator-action/noise baseline | **UNKNOWN** | No telemetry; cannot measure without new instrumentation or operator self-report |
| Per-corpus volume split | **PARTIALLY MEASURED** | All 273 are paper-corpus; tracker corpus = 0 alerts today |
| Scarcity target | **PINNED** | 3-5/day max |

## 7. Gating Decision

Per PR #296 item 5: **"If inputs are missing, alert launch stays blocked or dashboard-only."**

Operator-action/noise baseline is UNKNOWN. By PR #296's anti-rationalization fence, this means:

- The build PR for live TG alert qualification **CANNOT** proceed to alert-dispatch shipping today.
- A design PR scoping the alert-intent surface CAN proceed (this PR — design only, no dispatch).
- A separate instrumentation PR (operator-action telemetry) would be required before the build PR for live dispatch is approved.

The design doc (`tasks/design_tg_alert_qualification_2026_05_29.md`) describes the alert-intent surface and explicitly defers dispatch behavior to a separate gated PR.

## Notes

- Decline in alert volume (37→34→33 on 2026-05-15/16/17 down to 13-15/day in 2026-05-24+) suggests recent tuning (cooldown, eligibility checks) is already compressing volume. Operator-action telemetry would clarify whether this compression matches operator preference.
- `chain_completed: 115 blocked_eligibility` indicates the eligibility gate is doing significant work already — useful baseline for the build PR's "default-blocked" framing.
- Backlog: file `BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY` as the explicit instrumentation prerequisite for the build PR.

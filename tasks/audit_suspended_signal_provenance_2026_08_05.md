# Suspended-signal provenance audit

**Date:** 2026-08-05
**Source of truth:** `scout.db` on the testbed VPS (`/root/gecko-alpha/scout.db`)
**Signal states changed by this audit:** none. Every suspended lane remains suspended.

Nine lanes examined. Each ruling below records the suspension's original
justification, the evidence available today, and whether that evidence supports
revival. It does not.

---

## 1. `volume_spike` — VALID SUSPENSION

| Field | Value |
|---|---|
| Suspended at | `2026-07-18T01:00:46.942505+00:00` |
| Suspended by | `auto_suspend` (automated) |
| Reason | `hard_loss` |
| Evaluation window | `drawdown_baseline_at = 2026-06-22T18:21:51.951640+00:00` → suspension |
| Cohort | **n = 31** |
| Net P&L | **−$514.47** |
| Ruling | **VALID** — suspension stands |

The cohort is the 31 `paper_trades` rows that both opened and closed inside the
final pre-suspension window. IDs are contiguous, `2624`–`2654`, first close
`2026-07-10T14:07:53Z`, last close `2026-07-17T18:26:29Z`. All 31 listed in full —
the point of a provenance record is that the cohort can be re-derived, not taken
on trust.

| # | ID | Symbol | Exit | P&L | % |
|---:|---:|---|---|---:|---:|
| 1 | 2624 | QCOMB | stale_onset | −6.03 | −2.01 |
| 2 | 2625 | SPYB | expired | −3.31 | −1.10 |
| 3 | 2626 | DRAMB | expired | −25.98 | −8.66 |
| 4 | 2627 | WDCB | stale_onset | −13.30 | −4.43 |
| 5 | 2628 | EGLD | expired | +12.65 | +4.22 |
| 6 | 2629 | RVN | expired | −37.29 | −12.43 |
| 7 | 2630 | GOOGLB | expired | −5.59 | −1.86 |
| 8 | 2631 | NBISB | expired | −8.99 | −3.00 |
| 9 | 2632 | CBRSB | expired | +4.42 | +1.47 |
| 10 | 2633 | KAT | expired | −43.31 | −14.44 |
| 11 | 2634 | HOODIE | moonshot_trail | +68.93 | +22.98 |
| 12 | 2635 | QQQB | expired | −6.23 | −2.08 |
| 13 | 2636 | GROVE | time_death | −59.06 | −19.69 |
| 14 | 2637 | TREE | expired_stale_price | +0.98 | +0.33 |
| 15 | 2638 | OPN | trailing_stop | +17.72 | +5.91 |
| 16 | 2639 | OWL | stop_loss | −86.29 | −28.76 |
| 17 | 2640 | TRUST | stale_onset | −41.51 | −13.84 |
| 18 | 2641 | SLX | time_death | −46.04 | −15.35 |
| 19 | 2642 | B3 | time_death | −65.43 | −21.81 |
| 20 | 2643 | PYR | stop_loss | −76.30 | −25.43 |
| 21 | 2644 | B | moonshot_trail | +23.15 | +7.72 |
| 22 | 2645 | T | stale_onset | +74.25 | +24.75 |
| 23 | 2646 | ANKR | time_death | −34.83 | −11.61 |
| 24 | 2647 | HOODCAT | stop_loss | −82.84 | −27.61 |
| 25 | 2648 | DRAMON | time_death | −11.77 | −3.92 |
| 26 | 2649 | BLAST | time_death | −13.49 | −4.50 |
| 27 | 2650 | XEC | peak_fade | +42.79 | +14.26 |
| 28 | 2651 | CASHDOG | stop_loss | −82.84 | −27.61 |
| 29 | 2652 | SN15 | time_death | −2.99 | −0.99 |
| 30 | 2653 | CLUDE | time_death | −2.99 | −0.99 |
| 31 | 2654 | PALU | time_death | −2.99 | −0.99 |
| | | | **Total** | **−514.47** | |

Composition: 8 winners, 23 losers. The losses are dominated by exits, not by
entries — 4 stop-losses total −$328.27 and 9 `time_death` closes total −$239.57,
together 110% of the net loss (the two categories exceed the net because the
8 winners offset part of it). Consistent with the standing finding that
suspensions are an exit-mechanics problem rather than a signal-quality one; that
finding is **not** grounds for revival here, and no exit change is made by this
delivery.

---

## 2. `tg_social` — SECURITY_OR_DATA_QUARANTINE_VALID

| Field | Value |
|---|---|
| Suspended at | `2026-07-03 00:24:47` |
| Suspended by | `operator` (not automated) |
| Reason | `ga01_containment_operator` |
| Ruling | **VALID** — owner hold, remains suspended |

24 `paper_trades` rows exist. **14 are fabricated and excluded**: entry-price
bookkeeping closes where no price source ever served the token, each with
`pnl_usd` exactly `$0.00`. The remaining 10 real rows total **−$474.55**.

Counting the fabricated rows would dilute net P&L and drawdown toward zero and
make a bleeding lane look healthier — i.e. un-suspendable for its real losses.
The exclusion keys on `exit_provenance = 'entry_fallback'`, with
`exit_reason = 'expired_stale_no_price'` retained as an OR-fallback for rows
closed before the provenance column existed.

This is an **owner hold under GA-01 containment**, not an automated
performance suspension. It is not this session's to lift, and no evidence
gathered here bears on it.

---

## 3. `narrative_prediction` — CONFIGURED ENABLED, EFFECTIVELY QUARANTINED

| Field | Value |
|---|---|
| `signal_params.enabled` | **1** |
| `suspended_at` | NULL |
| Dispatch | **blocked** |
| Ruling | configured-enabled ≠ reachable |

325 historical `paper_trades` rows. The row says enabled; dispatch does not reach
it. This is the exact shape the lever-vs-data-path discipline exists to catch: the
`enabled` flag looks like the control and is not the operative gate.

Recorded as a **named data gap**, not resolved. The flag and the effective state
disagree, and reconciling them means changing either dispatch or the flag — both
outside this delivery's scope. Anyone reading `enabled = 1` for this lane and
concluding it is live would be wrong.

---

## 4. `long_hold` — NOT_APPLICABLE_AS_SIGNAL / HISTORICAL_AND_UNREACHABLE

| Field | Value |
|---|---|
| `signal_params` rows | **0** |
| `paper_trades` rows | **14** |
| Date range | 2026-04-21 → 2026-04-25 |
| Ruling | **NOT_APPLICABLE_AS_SIGNAL** |

`long_hold` has no `signal_params` row, so it is not a dispatchable signal type
and cannot be suspended or revived — there is no lane to act on. The 14 rows are
historical artifacts of an exit-holding behaviour recorded in the signal column,
all from a five-day window in April 2026 (IDs 675, 738, 773, 800, 822, 839, 846,
931, 932, 953, 959, 984, 1070, 1198).

**HISTORICAL_AND_UNREACHABLE.** No action possible; none taken. Do not read its
absence from `signal_params` as a suspension.

---

## 5. Six zero-window lanes — SUSPENSION VALID, REVIVAL EVIDENCE INSUFFICIENT

All six were auto-suspended on the `hard_loss` gate. All six remain suspended.

| Lane | Suspended at | Reason | Trades opened since |
|---|---|---|---:|
| `trending_catch` | 2026-05-11T01:00:26Z | hard_loss | **0** |
| `losers_contrarian` | 2026-05-17T01:02:46Z | hard_loss | **0** |
| `chain_completed` | 2026-06-06T01:02:08Z | hard_loss | **0** |
| `slow_burn` | 2026-06-17T01:01:50Z | hard_loss | **0** |
| `first_signal` | 2026-06-29T01:01:48Z | hard_loss | **0** |
| `volume_spike` | 2026-07-18T01:00:46Z | hard_loss | **0** |

**Zero observation window.** Not "weak evidence for revival" — *no* evidence. A
suspended lane opens no positions, so it generates no forward data, so it can
never accumulate the evidence that would justify reviving it. The suspension is
self-sealing.

The post-suspension closes that do exist (`chain_completed` 35, `losers_contrarian`
56, `slow_burn` 7, `trending_catch` 5, `first_signal` 2) are **tail closures of
pre-suspension entries**, not new activity. Every one of those cohorts is
negative. Counting them as revival evidence would be a §9c attribution error — the
rows postdate the suspension but the decisions that produced them predate it.

**Ruling for all six: original suspension valid on its own window; current revival
evidence insufficient; remain suspended.** Breaking the self-sealing loop requires
observation-only collection — running the lane's decisions without taking
positions — which is explicitly **deferred and out of scope here**. It is named as
the blocker so it is not rediscovered.

---

## 6. Summary

| Lane | Ruling | State after audit |
|---|---|---|
| `volume_spike` | valid suspension, n=31, −$514.47 | suspended |
| `tg_social` | SECURITY_OR_DATA_QUARANTINE_VALID (owner hold) | suspended |
| `narrative_prediction` | enabled but dispatch-blocked | unchanged (named gap) |
| `long_hold` | NOT_APPLICABLE_AS_SIGNAL / HISTORICAL_AND_UNREACHABLE | n/a |
| `trending_catch` | valid; zero-window | suspended |
| `losers_contrarian` | valid; zero-window | suspended |
| `chain_completed` | valid; zero-window | suspended |
| `slow_burn` | valid; zero-window | suspended |
| `first_signal` | valid; zero-window | suspended |

### Named data gaps

1. **Zero observation window on all six auto-suspended lanes.** No forward
   evidence can accumulate while suspended. Blocks any evidence-based revival.
2. **`narrative_prediction` flag/dispatch disagreement.** `enabled = 1` does not
   mean reachable. Reading the flag alone gives the wrong answer.
3. **14 fabricated `tg_social` records.** Excluded here; the writer that produced
   $0.00 entry-fallback closes is not fixed by this delivery.
4. **`long_hold` has no lane.** Present in trade history, absent from
   `signal_params`; not suspendable and not revivable.

### Anti-scope

No signal enabled, disabled, suspended or revived. No parameter value or lock
state changed. No observation-only revival infrastructure built. No exit
mechanics touched.

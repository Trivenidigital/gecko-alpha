# D2b — What population is `combo_refresh` intended to measure?

**Status: GOVERNANCE RULING REQUIRED. No code. No status-registry change.**

This document presents evidence for a decision; it does not make one. Per
operator direction 2026-08-11: *do not decide status inclusion from observed
profitability.* The profitability table appears here only as impact analysis,
after the design-history argument, and is not the basis of any recommendation.

---

## 1. The question

`combo_refresh` computes each combo's 30d trade/win/loss rollup and from it
decides **suppression, parole re-arm, and clearance**. It counts only
`CLOSED_COUNTABLE_STATUSES` (`scout/trading/paper.py:50`):

```
closed_tp · closed_sl · closed_expired · closed_trailing_stop
closed_moonshot_trail · closed_stale_onset
```

Four terminal statuses exist in production and are **not** counted:
`closed_floor`, `closed_peak_fade`, `closed_manual`, `closed_time_death`.

The question is not "should `closed_time_death` be added". It is: **which
terminal outcomes is this feedback metric supposed to be computed over?**
Until that is answered, adding statuses on the evidence of their observed
returns would redefine the suppression metric after seeing the outcomes.

## 2. Design history

| status | introduced | PR | in countable set? |
|---|---|---|---|
| `closed_expired` | 2026-04-14 | `709fc22d` PaperTrader | ✅ yes |
| `closed_trailing_stop` | 2026-04-21 | #39 `51f974a2` | ✅ yes — **set created in this PR** |
| `closed_floor` | 2026-04-23 | #48 `3fd98da4` | ❌ no |
| `closed_peak_fade` | 2026-04-25 | #50 `044d2f6a` | ❌ no |
| `closed_moonshot_trail` | 2026-04-27 | #51 `4b58ca49` | ✅ yes — added deliberately |
| `closed_stale_onset` | 2026-07-03 | #408 `b23ef0a2` | ✅ yes — added deliberately |
| `closed_time_death` | 2026-07-10 | #457 `79bb8a29` | ❌ no |

`CLOSED_COUNTABLE_STATUSES` was created in #39 containing the statuses that
existed *at that moment*. It carries **no stated rationale for the set** — the
commit body describes the trailing-stop mechanic, not a measurement policy.

Two later statuses were consciously added (#51, #408). Three were not.

**The `models.py` comment is post-hoc description, not a design record.** The
line reading *"adds the runtime-only `open` and `closed_manual` /
`closed_floor` / `closed_peak_fade` variants that aren't counted in standard
rollups"* was written in **#51 (2026-04-27)** — *after* floor (04-23) and
peak_fade (04-25) already existed, by the same commit that added
`closed_moonshot_trail` to the countable set. It documents the state that had
accumulated. Neither #48 nor #50 mentions rollups, `combo_performance`, or
suppression at all. Whether the omission was considered or incidental is **not
recoverable from the record.**

## 3. A principled distinction does emerge — and it is not about returns

Reading the exit mechanics themselves (`scout/trading/evaluator.py`):

- **`closed_floor`** fires only when `floor_armed and current_price <= entry_price`.
  The floor arms **after leg 1**. The position is partially realized; its
  recorded `pnl_usd` is a **residual on the runner slice**, not the trade's
  full outcome.
- **`closed_peak_fade`** is guarded on `remaining_qty is not None and
  remaining_qty > 0` — likewise a **remaining-slice** exit.
- **`closed_time_death`** is gated on **`leg_1_filled_at IS NULL`** plus
  `max(cp_1h, cp_6h, cp_24h) < PAPER_LADDER_LEG_1_PCT`. It fires on a
  **whole, never-partially-realized position** — deliberately targeting "the
  cohort peak_fade never sees" (#457).

That yields a candidate design rule with no reference to profitability:

> **Countable = exits of a full, unrealized position.
> Excluded = exits of a partially-realized (post-leg-1) position, whose
> recorded PnL is a slice residual and is not comparable to a whole-trade
> outcome.**

Under that rule `closed_time_death` **belongs in the countable set** — it is
structurally the same class as `closed_expired`, which is already counted:
both are whole-position exits on a trade that never reached its first ladder
leg.

**Wrinkle, stated rather than smoothed over:** the rule is not perfectly clean.
`closed_trailing_stop` and `closed_moonshot_trail` can also fire on a
post-leg-1 remainder, yet both are countable. So the countable set is not
today a strict "full-position only" set. The rule above is a *candidate
reconstruction* of intent that fits 8 of 10 statuses, not a recovered
specification.

## 4. `closed_time_death` is live, and that was never reconciled

#457 shipped the lane **dry-run by default**, and says so explicitly:

> *"In DRY_RUN mode (default) it never closes… The real-close path
> (`close_reason='time_death'`, `'closed_time_death'`) is present but
> **unreachable until a future flip PR** sets `PAPER_TIME_DEATH_DRY_RUN=False`."*

Production `.env` today:

```
PAPER_TIME_DEATH_ENABLED=true
PAPER_TIME_DEATH_DRY_RUN=false
```

Code defaults remain `False` / `True`. **The lane was flipped live via `.env`,
so no code commit records the transition** — and the two code-level
checkpoints that would have prompted registration (`TradeStatus`,
`CLOSED_COUNTABLE_STATUSES`) were never revisited. 106 real closes since
2026-07-17.

This is the same root cause as the D2a registry gap: an `.env` flip that
bypassed the code-review surface where status registration would be noticed.

## 5. Impact analysis — what changes under each candidate population

Read-only replay, 30d window to 2026-08-11. Held constant: the documented rule
(`trades >= FEEDBACK_SUPPRESSION_MIN_TRADES=20` **and**
`wr < FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT=30.0`), the provenance filters, and
each combo's persisted `suppressed` / `parole_trades_remaining`.

### P0 — status quo (no change)

| combo | n | wr | branch |
|---|---|---|---|
| `gainers_early` | 117 | 75.2% | stay clear |
| `volume_spike` | 15 | 26.7% | stay clear (below min_trades) |
| `losers_contrarian` | 0 | — | preserve (LOCKED — cannot ever clear) |

### P1 — add `closed_time_death` only (the "full-position" rule of §3)

| combo | n | wr | branch | change |
|---|---|---|---|---|
| `gainers_early` | 212 | 41.5% | stay clear | stats move 33.7pt; branch same |
| `volume_spike` | 24 | 16.7% | **NEWLY SUPPRESS** | ⚠ **active lane auto-suppressed** |
| `losers_contrarian` | 2 | 0.0% | **RE-SUPPRESS + fresh parole 5** | lock released |

### P2 — all terminal outcomes (also `floor` + `peak_fade`)

Adds 61 further closes (34 `peak_fade`, 27 `floor`). Not replayed per-combo
here because it requires a prior ruling on whether a partial-slice PnL is
comparable to a whole-trade PnL (§3) — if it is not, P2 is incoherent
regardless of its numbers.

### Outcome character of the excluded statuses (30d)

Presented **last and deliberately**, as impact data only:

| status | n | wins | avg % | 0-price rows |
|---|---|---|---|---|
| `closed_time_death` | 106 | 0 | −11.75 | 0 |
| `closed_peak_fade` | 34 | 34 | +22.25 | 0 |
| `closed_floor` | 27 | 27 | +4.53 | 0 |
| `closed_expired` *(countable)* | 12 | 3 | −4.25 | 0 |

These are outcome-segregated by construction — `time_death` fires on a trade
that died flat, `floor`/`peak_fade` fire only after a gain. That is a
**mechanical consequence of when each rule can trigger**, and it means the
current population has a selection axis. It does **not** by itself establish
that the exclusions violate the metric's intended definition. That is the
ruling being requested.

## 6. What a ruling must settle

1. Is the feedback population **all terminal outcomes**, or **whole-position
   outcomes only**? §3 offers a reconstruction, not a specification.
2. If whole-position only: `closed_time_death` is in, and the
   `closed_trailing_stop` / `closed_moonshot_trail` post-leg-1 cases need
   reconciling.
3. **`volume_spike` auto-suppression is a required decision, not a side
   effect.** Any population that includes `time_death` suppresses a currently
   active lane on its first nightly refresh. Authorize or exclude it
   explicitly.
4. `closed_manual` — operator-initiated closes. Almost certainly out of any
   automated feedback population, but it is currently excluded by the same
   silence as the others.

## 7. Related

- D1 (parole slot burned on non-committing admission) — PR #522, independent.
- D2a (register `closed_time_death` in `TradeStatus` only) — proven
  runtime-neutral: 468 passed / 1 skipped identically with and without.
  `TradeStatus` is referenced once, at `models.py:83`, on a model constructed
  with four fields as a price-source boundary where `status` never leaves its
  `"open"` default. Safe as a standalone typing/documentation PR, and it does
  **not** pre-judge this ruling.
- `tasks/plan_losers_contrarian_bounded_pilot_2026_08_08.md` — the pilot whose
  invalidation surfaced all of this.

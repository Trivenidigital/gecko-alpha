# D2b — What population is `combo_refresh` intended to measure?

**Status 2026-08-12: RULED. Implementation BLOCKED on explicit transition
authorization. No code. No status-registry change.**

**Operator ruling (2026-08-12):** the authoritative population is **all
legitimate closed trades carrying usable realized outcome data**, with
exclusions based on **data validity/provenance** — not on which exit mechanism
fired. The present `CLOSED_COUNTABLE_STATUSES` whitelist is implementation
drift from the original locked contract, absent a later approved spec that
deliberately superseded it. None was found.

**Basis — the original locked spec, not the outcome table.**
`docs/superpowers/specs/2026-04-18-paper-trading-feedback-loop-design.md`:

> line 359 — *"Only closed trades count (`status != 'open'`). Single statement
> per window:"*
> line 370 — `AND status != 'open'`

Decisions `D4` (*suppression rule `trades>=20 AND 30d_wr<30%`*) and `D5`
(*parole: 14 days locked, then 5-trade re-test*) match the implementation
exactly, which corroborates that this spec is the governing contract for this
subsystem. PR #29 applied its D1–D21 decisions.

§3 below (the "whole position vs residual slice" reconstruction) is retained as
research only. It is **not** the governing rule and its own counterexample
(countable `trailing_stop`/`moonshot_trail` can also fire post-leg-1) already
showed it was unstable.

---

## 1. The question (as originally posed)

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

## 5. Full terminal-status audit + transition matrix (post-ruling)

Read-only, prod, 2026-08-12. **The three known omissions did not exhaust the
drift.** Twelve distinct non-`open` statuses exist; two are not closes at all.

### 5.1 Complete status universe

| status | n ever | not a close | invalid prov | stale mark | in `TradeStatus` | in whitelist |
|---|---|---|---|---|---|---|
| `closed_expired` | 896 | 0 | 14 | 129 | ✅ | ✅ |
| `closed_peak_fade` | 391 | 0 | 0 | 0 | ✅ | ❌ |
| `closed_sl` | 355 | 0 | 0 | 0 | ✅ | ✅ |
| `closed_trailing_stop` | 352 | 0 | 0 | 0 | ✅ | ✅ |
| `closed_time_death` | 106 | 0 | 0 | 0 | ❌ | ❌ |
| `closed_floor` | 71 | 0 | 0 | 0 | ✅ | ❌ |
| `closed_moonshot_trail` | 42 | 0 | 0 | 0 | ✅ | ✅ |
| `closed_tp` | 14 | 0 | 0 | 0 | ✅ | ✅ |
| `closed_stale_onset` | 4 | 0 | 0 | 4 | ✅ | ✅ |
| `closed_manual` | 1 | 0 | 0 | 0 | ✅ | ❌ |
| `kraken_pilot_anchor` | 1 | **1** | 0 | 0 | ❌ | ❌ |
| `solana_lane_anchor` | 1 | **1** | 0 | 0 | ❌ | ❌ |

**`kraken_pilot_anchor` / `solana_lane_anchor` are sentinel rows** — NULL
`closed_at`, NULL `exit_price`, NULL `pnl_usd` — that nonetheless satisfy
`status != 'open'`. A literal reading of the spec sweeps them into the
population, where they add to `COUNT(*)` while contributing neither a win nor a
loss, depressing WR. This is exactly why the ruling's *"carrying usable
realized outcome data"* qualifier is load-bearing and the literal SQL is not
directly implementable.

Invalid-provenance classes (already excluded today): 14 rows with
`exit_provenance='entry_fallback'` / `exit_reason='expired_stale_no_price'`,
all on `closed_expired`. `stale_snapshot` (133) is valid-but-discountable per
#408.

### 5.2 Replay — `CURRENT_WHITELIST` vs `ALL_VALID_TERMINAL_CLOSES`

`ALL_VALID` = `status != 'open'` AND `closed_at IS NOT NULL` AND
`pnl_usd IS NOT NULL` AND not `entry_fallback` AND not
`expired_stale_no_price`. Rule held constant at D4/D5. Only the 30d window
drives transitions.

**30d**

| combo | whitelist n / wr | all-valid n / wr | supp | transition |
|---|---|---|---|---|
| `gainers_early` | 117 / 75.2% | **271 / 54.2%** | 0 | stay clear — no transition |
| `volume_spike` | 15 / 26.7% | **25 / 20.0%** | 0 | ⚠ **NEWLY SUPPRESS** |
| `losers_contrarian` | 0 / — | **3 / 33.3%** | 1, rem=0 | ⚠ **CLEAR** |

**7d** (reported; drives nothing): `gainers_early` 46/78.3% → 102/55.9%;
`losers_contrarian` 0 → 3/33.3%.

### 5.3 The losers transition is defect-driven, and reveals a separate bug

An earlier replay that held `floor`/`peak_fade` out of both arms showed
`losers_contrarian` → RE-SUPPRESS + parole 5. **Under the correct population it
becomes CLEAR**: the `closed_peak_fade` winner joins the two `closed_time_death`
losers → 3 trades, 1 win, 33.3% ≥ 30% → the clear branch fires and lifts a
suppression earned on **103 trades**.

That is possible only because of an asymmetry in `combo_refresh`:

```
suppress:  if w30["trades"] >= min_trades (20) and w30["wr"] < wr_thresh
clear:     if not zero_trade (n >= 1)        and w30["wr"] >= wr_thresh
```

**Suppression requires n≥20 (spec D3/D4). Clearance requires n≥1.** A single
lucky retest trade can clear a suppression earned on a hundred. This is a
latent defect independent of the population question — but the population
correction is what makes it fire. The `losers_contrarian` CLEAR should
therefore be read as a **defect-driven transition, not a legitimate one**, and
likely wants its own ruling alongside `volume_spike`.

### 5.4 Outcome character of the excluded statuses (30d)

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

## 6. Open decisions — what still requires explicit authorization

The population question is RULED (§ header). These remain:

1. **`volume_spike` → NEWLY SUPPRESS.** Restoring the population definition and
   letting a nightly job silently mutate an active lane's state are separate
   decisions. Authorize or exclude explicitly before deployment.
2. **`losers_contrarian` → CLEAR.** Per §5.3 this is driven by the
   clear-branch min-trades asymmetry, not by a genuine recovery. Recommend
   ruling on the asymmetry first; clearing a 103-trade suppression on n=3
   should probably not be allowed to happen as a side effect of the population
   fix.
3. **The clear-branch asymmetry itself** — fix (add a min-trades guard to
   clearance, mirroring D3/D4) or accept. Independent of D2, but it changes
   what the corrected population does on first run.
4. **Sentinel-row exclusion** (`kraken_pilot_anchor`, `solana_lane_anchor`) —
   confirm they are excluded by a validity predicate (`closed_at`/`pnl_usd`
   NOT NULL) rather than by an ever-growing status blacklist, so future anchor
   rows cannot silently rejoin the population.
5. **`closed_manual`** — operator-initiated closes. Likely out of an automated
   feedback population, but currently excluded by the same silence as the rest.

**Deployment constraint:** do not ship the corrected population and let the
nightly refresh apply transitions automatically. Transitions are authorized
individually, in advance.

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

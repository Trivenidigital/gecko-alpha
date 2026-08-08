# losers_contrarian — bounded revival pilot (PREPARED, NOT EXECUTED)

**New primitives introduced:** NONE

**Date:** 2026-08-08
**Status:** plan only. No DB row changed, no flag flipped, no trade opened.
**Author's headline:** the pilot is cheap and safe to run, but its purpose is
**not** "see if the signal makes money." Its purpose is to collect the one
measurement every stop-width decision depends on — **`pre_leg1_mae_pct`**, the
adverse excursion over the window in which the initial stop is actually eligible
to fire. That column does not exist yet (PR #516), and the whole-life `mae_pct`
this plan originally proposed to use **cannot answer the question** — see the
retraction in §4.

**Blocked on:** #516 merged **and deployed** before activation.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trading-signal backtesting / counterfactual replay | none found | build from scratch — n/a here; this plan proposes no new analysis code, only a pre-registered read of existing columns |
| Max-adverse-excursion / stop-width analysis | none found | n/a — built in-tree; `mae_pct` shipped earlier, `pre_leg1_mae_pct` in PR #516 |
| SQLite cohort analysis | none found | use existing `sqlite3` + in-tree query patterns |
| Statistical bootstrap / confidence intervals | none found | standard proportion CI, no dependency needed |

**Ecosystem check.** `hermes-agent.nousresearch.com/docs/skills` served a shell
page ("Fetching 88k+ skills… 0 Built-in, 0 Optional, 0 Community") with no
catalog rendered; `agentskills.io` returned only the format overview and
`agentskills.io/search` 404s. **Verdict:** the hub could not be queried for
these four domains, so this is *"check attempted, catalog unavailable"* — not
*"checked and found nothing."* It is recorded as inconclusive rather than
negative. No external dependency is proposed regardless, since every primitive
this plan needs already exists in-tree.

---

## 1. Recommendation

**Run the pilot — but not yet, and not as a profitability test.** It is blocked
on #516 merging and deploying, and it must not touch exit geometry first.

Four things changed my framing, each load-bearing. The fourth came from review
and is the reason this plan is not executable as first written:

1. The standing thesis — *"suspensions are an exit problem, not a signal
   problem"* — **cannot be supported by the decomposition that produced it.**
   See §3. The thesis may still be true; the evidence offered for it does not
   discriminate.
2. The exits were **never actually fixed**. PR #504 was docs-only (266 lines,
   one file, zero code). `signal_params_audit` contains **zero** geometry
   changes for any signal since 2026-05-17. The precondition "fix exits before
   reviving" was never met by anything.
3. The decisive counterfactual is **structurally unanswerable on existing
   data**, and the codebase already says so in a comment (§4). A first revision
   of this plan proposed answering it with `mae_pct`; that was wrong, because
   `mae_pct` summarises the wrong window. Retracted in §4 and replaced by
   `pre_leg1_mae_pct` (#516).
4. **The measurement this pilot exists to collect was itself mis-specified.**
   The first revision would have spent ~16 days accumulating 200 trades of a
   metric that cannot answer the stop-width question, because `mae_pct` keeps
   deepening after the initial stop stops being eligible to fire. Caught in
   review before any data was collected. §4 carries the retraction; #516 carries
   the fix.

---

## 2. Runtime state verified (§9a)

Every assumption checked against prod before proposing anything.

| # | Assumption | Verified value | Status |
|---|---|---|---|
| 1 | Signal is suspended | `signal_params.enabled=0`, `suspended_at=2026-05-17`, `suspended_reason=hard_loss`, `updated_by=auto_suspend` | ✅ |
| 2 | Env gate must be flipped too | `PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=true` **already** | ⚠️ **inverted** |
| 3 | Candidate supply exists | `losers_snapshots` ~10,000 rows/day, fresh through 2026-08-08 | ✅ |
| 4 | Tracker running | `LOSERS_TRACKER_ENABLED=true` | ✅ |
| 5 | Exit geometry unchanged since the loss | zero geometry rows in `signal_params_audit` since 2026-05-17; last write to the row was the suspension itself | ✅ |
| 6 | Position size on revival | **$150** — absent from trust registry → `experimental` → 0.5 × `PAPER_TRADE_AMOUNT_USD=300` | ⚠️ **accidental** |
| 7 | Revival primitive exists | `Database.revive_signal_with_baseline` — atomic `enabled=1` + `drawdown_baseline_at` + audit row, 7-day cool-off | ✅ |
| 8 | Whole-life MAE available | **0 rows before 2026-08**; 70/70 in August | ❌ |
| 9 | Whole-life MAE *sufficient* for the stop-width question | **No** — it keeps deepening after the SL stops being eligible; 7/7 armed winners misread as damage | ❌ **blocking, see §4** |
| 10 | Eligibility-window MAE exists | `pre_leg1_mae_pct` — **PR #516, not yet merged/deployed** | ❌ **blocking** |

**Assumption 2 is a stale-comment trap.** `scout/main.py:985-986` states the
lane is *"disabled by default in prod via
PAPER_SIGNAL_LOSERS_CONTRARIAN_ENABLED=False."* Prod has it `true`. Anyone
planning this revival from the code comment would have flipped an env var that
was already flipped and concluded the lane was live. The **only** effective
blocker is the single DB row.

---

## 3. The confound that invalidates the standing thesis

The finding on record splits closed trades by exit path:

| Group | n | P&L | avg |
|---|---|---|---|
| MANAGED (`tp`, `floor`, `peak_fade`, `trailing_stop`, `moonshot_trail`) | 130 | **+$5,368** | +14.15% |
| UNMANAGED (`sl`, `expired`) | 200 | **−$6,501** | −10.84% |

This looks like proof that exits are the problem. **It is not a treatment
contrast.** `peak_fade`, `trailing_stop`, `tp` and `moonshot_trail` can only
fire when price *rises*. A trade cannot be assigned to the managed arm — the
price path assigns it. "Trades that went up exited profitably" is close to
tautological, and the same arithmetic would appear for a signal with no edge at
all.

The discriminating query — *what was actually available to capture?*:

| exit path | n | avg peak | avg realized |
|---|---|---|---|
| `closed_sl` | 58 | **+3.0%** | −26.3% |
| `closed_expired` | 142 | +6.4% | −4.5% |

**39 of the 58 stop-outs peaked below +5%; exactly one exceeded +10%.** These
positions never went meaningfully green. No exit rule rescues a trade that
peaked at +3% and fell 26%. The perfect-foresight ceiling — selling all 173
peak-bearing unmanaged trades at their exact top — is +$2,835 against −$5,141
actual, and requires unattainable precision on an average peak of **+5.46%**.

### What the signal actually looks like

| Band | n | P&L |
|---|---|---|
| reached +10% (first exit rung) | 152 | **+$5,551** |
| peaked above entry, never +10% | 151 | −$5,323 |
| never traded above entry (`peak_pct` NULL) | 27 | −$1,361 |
| **total** | **330** | **−$1,133** |

`peak_pct` NULL is **not** a data defect — `evaluator.py:594` only writes it
when `current_price > reference`, so NULL means "never exceeded entry." All 27
have valid entry/exit prices and every one is negative. Confirmed in the writer
before claiming it.

Against peers, `losers_contrarian` is mid-pack, not an outlier: 50.2% of trades
reach +10% (`gainers_early` 62.1%, `narrative_prediction` 34.6%) on an average
peak of 13.2%. **The honest read is a near-coinflip signal (48.8% win) whose
geometry is calibrated for moves it produces about half the time** — not a
signal that is being destroyed by its exits.

---

## 4. Why the obvious fix cannot be evaluated today

> ### ⚠️ RETRACTION 2026-08-08 — `mae_pct` alone cannot do this either
>
> **An earlier revision of this section claimed that `n = 200` MAE-bearing
> closes would settle the stop-width question. That claim is withdrawn.** It
> was wrong for a reason that would have destroyed the pilot's entire output.
>
> `mae_pct` is a **whole-life** low-water mark — the evaluator updates it on
> every valid tick until close, unconditionally
> (`evaluator.py:623-637`, no guard). The initial stop is **not** whole-life; it
> is gated `if not floor_armed and sl_price > 0 and current_price <= sl_price`
> (`evaluator.py:780`). Once leg 1 arms the floor, the SL is structurally out of
> the picture and the downside rule becomes a breakeven floor at entry
> (`evaluator.py:812`).
>
> So `entry → +10% (leg 1 arms) → −15% → closes profitable` records
> `mae_pct = −15%`, and the proposed counterfactual would read that as *"a −12%
> stop would have killed this winner."* **It would not have** — the stop was no
> longer eligible when the dip occurred.
>
> Worse, the contamination lands **exactly on the population the pilot needed to
> measure**. Arming requires `change_pct >= leg_1_pct` (+10%), so every winner
> arms the floor, and every post-arm dip pollutes its `mae_pct`. On the only
> cohort carrying the column (2026-08):
>
> | population | n | dipped past −12% | of those, **won** |
> |---|---|---|---|
> | floor **ARMED** (SL ineligible) | 47 | 7 | **7** |
> | not armed (SL eligible) | 23 | 19 | **0** |
>
> All 7 would have been miscounted as damage from tightening — a **14.9%
> false-damage rate** on the armed population, biased in the direction that
> makes a tighter stop look worse than it is.
>
> **Correct measurement, now required before any revival:**
> `pre_leg1_mae_pct` — the low-water mark restricted to the window in which the
> initial stop is eligible, frozen when leg 1 arms. Shipped in **PR #516** with
> the discriminating test pair (dip before leg 1 → counted; dip only after leg 1
> → not counted) and mutation-tested: removing the eligibility guard kills two
> tests, changing the trough seeding kills a third.
>
> **The pilot is blocked on #516 merging and deploying.** Rows opened before
> that deploy carry `pre_leg1_mae_pct IS NULL` and are **not** eligible for the
> n-gate — same forward-only discipline as `mae_pct`, no backfill, and NULL must
> never be read as 0.
>
> This is the same failure class the project keeps hitting: **having the right
> value without its relevant timing/state axis is not enough.** The value was
> real; the window it summarised was the wrong one.



The tempting change is a tighter stop: `sl_pct` 25 → ~12. On available data it
looks strictly beneficial. It is not evaluable, and the codebase already
documents exactly why (`scout/trading/evaluator.py:603-609`):

> *"Without it, stop-width cannot be evaluated: for trades that already stopped
> out we know they reached the stop, but for every other close we cannot know
> whether it dipped past a tighter stop before recovering. **That asymmetry
> makes a tighter stop look strictly beneficial on the available data**, which
> is why this is measured rather than inferred."*

A tighter stop helps the 58 stop-outs and **damages an unknown number of the
161 winners** that dipped and recovered. Measuring the second effect requires
`mae_pct`. Coverage:

| month | closed trades | with `mae_pct` |
|---|---|---|
| 2026-04 | 836 | **0** |
| 2026-05 | 975 | **0** |
| 2026-06 | 109 | **0** |
| 2026-07 | 190 | **0** |
| 2026-08 | 70 | 70 |

The entire `losers_contrarian` cohort (2026-04-21 → 05-17) has zero. This is
the same shape as the P0b finding: the study is not hard, it is *structurally
unanswerable* on the existing cohort, and no amount of re-slicing fixes it.

**That is the pilot's actual justification.** Not "does it make money" — we have
330 trades saying roughly break-even-minus. The pilot exists to produce
MAE-bearing trades on a signal with a known prior.

---

## 5. The paper-sizing contract is intentional; the registry's self-description and coverage have drifted

> **RECLASSIFIED 2026-08-08.** An earlier revision titled this *"blocking
> finding — the sizing registry contradicts itself, in production"* and
> presented registry-derived paper sizing as an undiscovered contract violation.
> **That framing is wrong and is withdrawn.**
>
> Merged **PR #455** (`01daa849`, 2026-07-11, *"trust-weighted paper sizing,
> flag-gated (SIG-10) — carries a contract decision"*) surfaced this exact
> decision for operator approval in its own body:
>
> > *"The registry is stamped `not_for_sizing: true` … This PR consumes its
> > maturity_state as DATA for a paper-only, default-OFF knob and relaxes no
> > gate … **Approve as-is** if that reading is acceptable (my recommendation: it
> > is — the stamp guards live/production sizing automation, and this knob
> > generates the evidence for that future decision), **or say 'redirect'**…"*
>
> It was approved and merged. `trust_sizing.py` documents the same reading. The
> consumer is deliberate, flag-gated (`PAPER_TRUST_SIZING_ENABLED`), paper-only,
> and defaults unknown signals to `experimental` on purpose. There is no hidden
> production consumer and no undiscovered violation.

What survives — and still matters:

- The registry's **self-description is stale and misleading**. `"not_for_sizing":
  true` and *"not consumed by production logic"* now read as false to anyone who
  greps the file without finding #455. The stamp means *live* sizing; the text
  does not say so.
- **Coverage has drifted.** `gainers_early` — the only signal currently trading —
  is absent, so it takes the `experimental` default. The registry also carries
  entries for `tg` and `x`, which are not real `signal_type` values (the real one
  is `tg_social`); those never match.
- The **$150-vs-$300 cohort confounding is real** and is the operationally
  important part.

Independent of this pilot, and larger than it.

`docs/superpowers/registries/signal_trust_registry.v1.json` declares in its own
header:

```json
"visibility_only": true,
"not_for_sizing": true,
"notes": "Read-only registry for operator visibility. This file is not consumed
          by production logic and must not be used for pruning, suppression,
          auto-disable, sizing, or live execution decisions."
```

`scout/trading/trust_sizing.py` consumes it for **paper** sizing, `engine.py:436`
calls it at open time, and **`PAPER_TRUST_SIZING_ENABLED=true` in prod** — all of
which was authorized by #455 (above). The text is what is stale, not the
behaviour. What the flag being on *does* mean is that sizing now varies by
registry membership, verified empirically rather than inferred:

| signal | registry entry | tier | size | n |
|---|---|---|---|---|
| `gainers_early` | **absent** | experimental (default) | **$150** | 239 |
| `volume_spike` | `trusted_experimental` | trusted | $300 | 31 |

All 239 `gainers_early` trades are sized at exactly $150 and all 239 carry
`trust_tier` in `signal_data`. The registry also holds entries for `tg` and `x`,
which are not real `signal_type` values (the real one is `tg_social`) — dead
entries that never match.

Three consequences that touch conclusions already drawn:

1. **The only signal currently trading is half-sized** because it is *absent*
   from the registry and therefore takes the `experimental` default. That
   default is intentional (#455); the surprise is the coverage gap, not the
   mechanism.
2. **Cross-signal USD comparisons are 2× confounded.** `gainers_early` at $150
   against `volume_spike` at $300 is not a like-for-like P&L comparison.
3. **The auto-suspend gates are USD-denominated** (`hard_loss −$500`,
   `pnl_threshold −$200`). A half-sized signal needs twice the percentage loss
   to trip them. `gainers_early`'s 2026-07-27 `hard_loss` suspension fired on
   $150 trades — **the underlying percentage loss was twice as bad as the dollar
   figure suggests.** Effective safety-gate severity currently varies 2× across
   signals for reasons unrelated to signal quality.

Clean cohort boundary for anyone re-reading `gainers_early` history:

| cohort | size | n |
|---|---|---|
| 2026-04-21 → 05-18 | $300 | 518 |
| 2026-07-19 → 08-07 | **$150** | 239 |

**Recommended handling — do NOT "fix" the sizing now.** The in-flight
`gainers_early` cohort is at 69/100 toward its own n-gate; doubling its size
mid-cohort would rebase the comparison exactly the way the retention rebase did
in #512. Correct the registry's *self-description* and make the dependency
explicit; decide actual sizing at the next cohort boundary. Filed as a separate
item, not bundled into this pilot.

---

## 6. Pilot design

Deliberately minimal — every primitive already exists (§7a drift check found
`revive_signal_with_baseline`, trust sizing, `drawdown_baseline_at`, MAE
capture, the decision-event ledger). **No new primitives.**

**The single action:** one call to
`revive_signal_with_baseline("losers_contrarian", reason=…, operator="operator",
restore_tg_alert_eligible=False)`.

That atomically sets `enabled=1`, stamps `drawdown_baseline_at=NOW()` so
pre-revival drawdown is excluded from the rolling window, and writes the audit
row. Nothing else changes:

- `live_eligible` stays **0** — paper only, no real money.
- `tg_alert_eligible` stays **0** — explicitly passed, not defaulted. A
  paper-only revival must not silently re-enable alerting (#513).
- Exit geometry **unchanged** — changing it now would confound the very
  measurement the pilot exists to produce.
- `.env` untouched — both env gates are already `true`.

### Why the size is already safe — and how much margin there is

At the `experimental` 0.5× default, trades open at $150. Replaying the rolling
window that actually triggered the 2026-05-17 suspension (`net −$208`,
`drawdown −$1,412` at $300/trade) at half size:

| gate | condition | at $150 | fires? |
|---|---|---|---|
| (a) catastrophic bleed | `net ≤ −$500` | −$104 | no |
| (b) pump-then-crash | `drawdown ≤ −$500` **AND** `net < −$200` | −$706 **AND** −$104 | **no** — net leg fails |

The historical cohort would not have auto-suspended at $150. **State the margin
honestly: gate (b)'s drawdown leg is already breached (−$706 vs −$500); only the
net leg holds it off, with roughly 2× headroom.** If the pilot performs
materially worse than history, gate (b) fires — and that is the safety system
working, not a pilot failure. The pilot is designed to survive *historical*
performance, not arbitrary performance.

This safe size is currently **accidental** — it follows from the signal being
absent from the registry (§5). That fragility is the reason §5 is a blocker to
document rather than a nice-to-have.

---

## 7. Pre-registered gates (§11a — data-bound, not calendar-bound)

Written before execution. **Not to be revised after seeing results.**

### 7.1 Cohort boundary

The pilot cohort is defined by a persisted timestamp, not by "trades that look
recent". At activation, record `PILOT_T0` = the `applied_at` of the
`revive_signal_with_baseline` audit row — an immutable row, not a recomputed
`MIN()`.

A row is **eligible** only if all four hold:

1. `signal_type = 'losers_contrarian'`
2. `opened_at >= PILOT_T0`
3. `status LIKE 'closed%'`
4. `pre_leg1_mae_pct IS NOT NULL`

Condition 4 excludes every row opened before #516 deploys. NULL is *never* read
as 0. Conditions 2 and 4 are separate on purpose: a row can satisfy the cohort
boundary and still be uninstrumented if the deploy lands after activation, which
is why **#516 must deploy before activation, not alongside it**.

### 7.2 Exposure bounds (explicit, not implied)

| Bound | Value | Enforced by |
|---|---|---|
| Position size | **$150** (`PAPER_TRADE_AMOUNT_USD=300` × experimental 0.5) | `resolve_paper_trust_size` |
| Max simultaneous open pilot positions | **60** | monitored; breach = K5 |
| Max aggregate pilot notional at any instant | **$9,000** (60 × $150) | derived from the above |
| Max cumulative pilot notional deployed | **$30,000** (200 × $150) | n-gate terminates the pilot |

The concurrency figure is derived, not invented: at the historical ~12.7
trades/day and a 168h (7-day) max duration, steady-state open positions are
≈ 89 — so **60 is a real constraint** and the pilot is expected to press against
it. It is set deliberately below the natural steady state to cap instantaneous
notional; the cost is a slower fill rate, which the n-gate absorbs.

Paper only. `PAPER_MAX_EXPOSURE_USD=200,000` is the global backstop; the pilot's
$9,000 ceiling is 4.5% of it.

### 7.3 Completion gate

**Primary:** `n = 200` **eligible** rows (per §7.1).

At the historical 48.8% win rate that yields ~98 profitable trades — enough to
estimate *P(dipped below candidate stop while the stop was eligible | ended
profitable)* to about ±10pp at 95%. That conditional is the quantity the
stop-width decision turns on, and §4's retraction is why it must be computed
from `pre_leg1_mae_pct`, never `mae_pct`.

**Interim read at n = 100** (±15pp) is **descriptive only** and may not trigger
any change.

**Calendar estimate, not a target:** ~16 days at the historical rate, longer
under the 60-position cap. Per §11c, halt at n=200 whenever it arrives; if the
rate collapses, extend rather than concluding on a short sample.

### 7.4 Kill criteria

| # | Condition | Action |
|---|---|---|
| K1 | auto-suspend fires | let it. Do **not** re-revive. Record `n` reached |
| K2 | net ≤ −$400 at n<200 | halt + rollback; below the −$500 gate by design |
| K3 | `pre_leg1_mae_pct` NULL rate > 5% on new eligible closes | halt — instrumentation broken, the only deliverable is void |
| K4 | fill rate < 3/day for 5 consecutive days | halt — n=200 unreachable in a sane window |
| K5 | simultaneous open pilot positions > 60 | halt + rollback — exposure bound breached |

**Explicitly NOT a kill criterion:** negative P&L within the gates.
Break-even-minus is the *prior*, not a surprise. Killing the pilot for
confirming the prior would destroy the measurement it exists to collect.

### 7.5 Mandatory rollback — the pilot disables itself

**At n=200, or on ANY kill criterion, the DB row returns to `enabled = 0`.**

This is not optional and not deferred to analysis. A "bounded pilot" that
reaches its gate but leaves the row enabled while someone reads the numbers is
not bounded — it is an unbounded revival with a report attached.

Default sequence: **collect → disable → analyze → separately authorize anything
further.** No automatic continuation. No geometry change merely because the
sample completed. `live_eligible` and `tg_alert_eligible` stay `0` throughout and
across the rollback.

Rollback is an audited write (`applied_by='operator'`, reason referencing this
plan), not a silent flip.

### 7.6 Decision rules — pre-registered mapping

Computed on the eligible cohort only. Let **D** = share of *profitable* eligible
trades whose `pre_leg1_mae_pct` ≤ the candidate stop (the true damage rate), and
**S** = P&L saved on trades that closed `closed_sl`.

| Outcome | Condition | Verdict |
|---|---|---|
| **REVISE** | net stop-width change is positive with D ≤ 15%, **and** the sign survives candidate stops of −10/−12/−15% | propose a `sl_pct` change as its own audited PR with its own approval |
| **KEEP** | net change is positive but D > 15%, or the sign flips across candidate stops | leave geometry unchanged; record the measurement; signal stays disabled |
| **RE-SUSPEND** | net change is negative, or the cohort's own P&L is worse than the −$1,133 / 330-trade historical rate on a size-normalised basis | signal stays disabled; write the result up as a retirement argument |

All three verdicts end with the signal **disabled** (§7.5). REVISE authorizes a
*proposal*, never an application — the same proposal-only discipline the
deterministic learner runs under.

**What the pilot can and cannot conclude:**

- ✅ *Can*: whether a tighter **initial** stop is net-beneficial, using a window
  that matches the stop's actual eligibility.
- ✅ *Can*: whether the 2026-04/05 peak distribution is stable three months on.
- ❌ *Cannot*: whether the signal is profitable at $300 — USD results do not
  transfer across a 2× size change (§5).
- ❌ *Cannot*: anything about the *post-arm* breakeven floor or the trailing
  stop. `pre_leg1_mae_pct` is silent past arm time by construction; those are a
  separate question needing a separate measurement.
- ❌ *Cannot*: anything about live execution. Paper only.

## 8. Sequencing

Ordering is load-bearing: steps 1–3 must complete **before** step 4, or the
cohort accumulates rows that can never satisfy §7.1.

1. **This plan reviewed and approved** ← current state
2. **#516 merged** (`pre_leg1_mae_pct` + the discriminating test pair)
3. **#516 deployed and verified in prod** — confirm a freshly-opened trade gets a
   non-NULL `pre_leg1_mae_pct`. Merge is not deploy; a plan gated on a column
   that exists only on master would collect 200 unusable rows.
4. Record `PILOT_T0`, then the single `revive_signal_with_baseline` call —
   `restore_tg_alert_eligible=False`, logged in the approvals table
5. Verify within 24h: trades opening with `signal_type='losers_contrarian'`,
   `amount_usd = 150`, `pre_leg1_mae_pct` non-NULL on first close,
   `tg_alert_eligible` still `0`, open positions ≤ 60
6. Read at n=100 (descriptive only), decide at n=200 per §7.6
7. **Rollback to `enabled=0` at n=200 or any kill criterion (§7.5)** — before
   analysis, not after
8. Only then consider a geometry change — as its own audited proposal, with its
   own approval

Filed separately, **not** blocking the pilot: the registry self-description and
coverage drift (§5).

---

## 9. Anti-scope

Not done, and deliberately: no parameter value changed · no lock state changed ·
no exit geometry touched · no `.env` edit · no real-money execution · no order,
swap, approval or transaction · no revival executed · no sizing change to any
signal · no registry edit.

---

## 10. Approvals log

| Action | Class | Approval record | Timestamp |
|---|---|---|---|
| Read-only prod DB / config queries | read-only | covered by standing test-bed calibration (2026-07-19) | 2026-08-08 |
| Author this plan | doc | operator: "prepare, do not execute" | 2026-08-08 |
| Revise this plan per review ruling | doc | operator: `CHANGES_REQUIRED_BEFORE_MERGE` (7-item list) | 2026-08-08 |
| Ship `pre_leg1_mae_pct` (PR #516) | code + migration | operator item 3: "establish a temporally valid measurement… prove with tests" | 2026-08-08 |
| Merge #516 | merge | **NOT YET REQUESTED** — per-PR only | — |
| Deploy #516 to testbed | deploy | **NOT YET REQUESTED** | — |
| **Revive `losers_contrarian` (paper)** | **runtime state change** | **NOT YET REQUESTED** — operator: `LOSERS_CONTRARIAN_REVIVAL_NOT_AUTHORIZED` | — |
| Registry self-description / coverage correction | code + config | NOT YET REQUESTED — separate item | — |

No action in the last four rows has been taken. #516 is authored and pushed but
**not merged**; the revival is **not authorized** and nothing in prod has
changed.

### Review-ruling conformance

| # | Required correction | Where satisfied |
|---|---|---|
| 1 | Keep the managed/unmanaged falsification | §3, unchanged |
| 2 | Retract the whole-life `mae_pct` claim | §4 retraction block |
| 3 | Temporally valid measurement + discriminating tests | PR #516; `tests/test_pre_leg1_adverse_excursion.py`, mutation-tested |
| 4 | n=200 gate uses the valid measurement | §7.1 condition 4, §7.3 |
| 5 | Cohort timestamp / max opens / max notional / rollback / KEEP-REVISE-RE-SUSPEND | §7.1, §7.2, §7.5, §7.6 |
| 6 | Reclassify §5 per #455 | §5 reclassification block |
| 7 | No production revival | §9 anti-scope; approvals log above |

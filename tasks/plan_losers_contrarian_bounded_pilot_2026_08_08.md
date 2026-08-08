# losers_contrarian — bounded revival pilot (PREPARED, NOT EXECUTED)

**New primitives introduced:** NONE

**Date:** 2026-08-08
**Status:** plan only. No DB row changed, no flag flipped, no trade opened.
**Author's headline:** the pilot is cheap and safe to run, but it is **not** a
profitability test and **not** a net-P&L counterfactual. It is a **stop-damage
incidence screen**: it measures how often each candidate stop threshold would
have been crossed while the initial stop was actually eligible to fire, using
**`pre_leg1_mae_pct`** (PR #516). It cannot price those crossings — a low-water
*mark* records no fill price — so its strongest possible verdict is "authorize a
separate stop-width experiment," never "the geometry is proven better." See the
retraction in §4 and the scope correction in §7.6.

**Blocked on:** #516 (`398c32a7`) and #517 (`e6b8f741`) are **merged**. Both must
still be **deployed** before activation — #516 for provenance-safe measurement,
#517 for the entry cap that makes this pilot's stated bounds real rather than
descriptive. Merge is not deploy (§8 step 3).

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
5. **Even with the right column, the verdict had to shrink.** `pre_leg1_mae_pct`
   is a *mark*: it proves a threshold was crossed while the stop was live, but
   records no fill price, so no net-P&L counterfactual follows from it. §7.6 is
   now an incidence screen. Claiming otherwise would have invented fill precision
   the data does not contain — the same error as (4), one level deeper.

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
| 10 | Eligibility-window MAE exists | `pre_leg1_mae_pct` — #516 **merged** `398c32a7`; **not yet deployed** | ⚠️ **blocking until deployed** |
| 11 | Entry cap enforceable | `PAPER_LOSERS_PILOT_MAX_ENTRIES` — #517 **merged** `e6b8f741`; **not yet deployed**, and unset in prod `.env` (default 0 = no cap) | ⚠️ **blocking until deployed + configured** |

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
> Worse, the contamination concentrates in the population the pilot needs to
> measure. Arming requires `change_pct >= leg_1_pct` (+10%), so **every armed
> trade is one that ran at least +10% at some point** — and its post-arm dips
> pollute its `mae_pct`. On the only cohort carrying the column (2026-08):
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
> **Stated precisely, because the looser version is false.** An earlier revision
> wrote *"arming requires +10%, so every winner arms the floor."* That does not
> follow: a trade can close profitably without ever reaching +10% — 151 of the
> 330 historical closes peaked above entry but never hit the first rung (§3), and
> some of those closed green. Arming implies a +10% run; being a winner does not
> imply arming.
>
> The supported claim is narrower and is what the table shows: **in the observed
> August sample, all 7 floor-armed trades that later dipped past −12% were
> winners, against 0 of 19 in the unarmed threshold-crossing group.** n=70 across
> all signals, not `losers_contrarian`-specific. That is enough to establish the
> contamination is real and directional; it is not enough to quantify its size on
> this signal, which is one of the things the pilot is for.
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

### 7.2 Exposure bounds — the cohort is capped on ENTRIES, not closes

> **CORRECTED 2026-08-08.** An earlier revision paired a completion gate of
> "200 **closed** trades" with a cumulative cap of "$30,000 = 200 × $150."
> **Those two are arithmetically incompatible.** At the instant the 200th trade
> *closes*, positions opened later are still open — at the 60-concurrency figure
> that is 240–260 entries already deployed, i.e. **$36,000–$39,000**, before
> counting rows excluded by §7.1. The stated cap was not a cap.

**The cohort is bounded by entries, enforced in code — under one admission
writer.**
`PAPER_LOSERS_PILOT_MAX_ENTRIES = 200` (**PR #517**). `trade_losers` refuses the
entry that would exceed it, counting the cohort from
`signal_params.drawdown_baseline_at` — the same anchor `auto_suspend` uses, so
the two cannot disagree about which trades are "the pilot". Refusals emit a
`pilot_entry_cap_reached` decision row.

> **Why this needed code.** A previous revision called 200 a hard cap while
> relying on an operator noticing "entry 200 landed" and then disabling the row.
> That is a **monitored cutoff, not a cap**: `trade_losers` iterates the whole
> fresh-losers batch, so further entries can be admitted before the disable write
> lands. Calling a monitored cutoff a bound is the same species of error as
> calling a mark a fill price.

Already-open positions resolve normally once admission closes. No position is
force-closed — that would truncate the very excursions being measured.

| Bound | Value | Nature |
|---|---|---|
| Position size | **$150** (`PAPER_TRADE_AMOUNT_USD=300` × experimental 0.5) | enforced by `resolve_paper_trust_size` |
| **Max cohort entries** | **200** | **enforced pre-open** (#517) — exact under a single admission writer; see the concurrency scope below |
| Max cumulative pilot notional | **$30,000** (200 × $150) | follows from the enforced entry cap |
| Simultaneous open pilot positions | **60** | **monitored abort threshold (K5), not an admission cap** |
| Peak instantaneous notional | ~$9,000 at 60 open | consequence of the above, not a guarantee |

**The 60 figure is labelled honestly.** There is no pre-open enforcement point
for it today. `trade_losers` (`scout/trading/signals.py:519`) selects candidates
excluding tokens with an open position, but applies no count cap; adding one is a
code change this plan does not propose. So 60 is a **tripwire**: if concurrency
exceeds it, K5 halts the pilot. It is not a promise that concurrency cannot
exceed it.

Sizing this honestly matters because the natural steady state runs *above* the
tripwire: at the historical ~12.7 entries/day against a 168h max duration,
concurrency tends to ≈ 89. **K5 is therefore likely to fire before n=200 unless
the entry rate is lower than history.** That is an accepted and pre-registered
outcome, not a surprise — a halted pilot with a recorded `n` is a valid result,
and it is strictly preferable to silently exceeding a bound the plan claimed to
enforce.

If the operator wants 60 to be a real ceiling, that requires a pre-open count
check in the admission path — a separate, testable change, and a precondition
this plan deliberately does not smuggle in.

Paper only. `PAPER_MAX_EXPOSURE_USD=200,000` is the global backstop.

### 7.3 Completion gate

Two distinct events, previously conflated:

1. **Admission closes** at the **200th cohort entry** (§7.2). This is the hard
   bound and the only one that caps notional.
2. **The pilot completes** when every one of those ≤200 entries has resolved —
   `status LIKE 'closed%'`. Bounded above by the 168h `max_duration_hours`, so
   completion trails admission-close by at most 7 days.

**Analysis set** = the eligible subset of those entries (§7.1): closed **and**
`pre_leg1_mae_pct IS NOT NULL`. Call its size `n_eff`. Because #516 deploys
before activation (§8), `n_eff` should approach 200; it cannot exceed it.

**Precision `n_eff` buys.** At the historical 48.8% win rate, `n_eff = 200`
yields ~98 profitable trades — enough to estimate `D(X)`, the damage-incidence
rate of §7.6, to about ±10pp at 95%. `n_eff = 100` gives roughly ±15pp.

**Minimum for a verdict: `n_eff ≥ 120`** (~59 winners, ~±13pp). Below that,
§7.6 returns **`NO_VERDICT`** — record the incidence table as descriptive and stop.
Pre-registering this floor matters because the entry cap makes `n_eff` an upper
bound rather than something reachable by waiting longer: **if `n_eff` is short,
more calendar time cannot fix it**, unlike a closes-based gate.

**Interim read at `n_eff = 100`** is **descriptive only** and may not trigger any
change.

**Calendar estimate, not a target:** ~16 days to 200 entries at the historical
rate, plus up to 7 days for the tail to resolve. Per §11c the gate is the data,
not the date.

### 7.4 Kill criteria

| # | Condition | Action |
|---|---|---|
| K1 | auto-suspend fires | let it. Do **not** re-revive. Record `n` reached |
| K2 | net ≤ −$400 before admission closes | halt + rollback; below the −$500 gate by design |
| K3 | `pre_leg1_mae_pct` NULL rate > 5% on new eligible closes | halt — instrumentation broken, the only deliverable is void |
| K4 | entry rate < 3/day for 5 consecutive days | halt — 200 entries unreachable in a sane window |
| K5 | simultaneous open pilot positions > 60 | halt + rollback — **monitored tripwire, not a hard cap** (§7.2); firing is a pre-registered outcome, not a failure |
| K6 | `pilot_entry_cap_exceeded` logged, **or** overlapping pipeline processes observed during the pilot | halt + rollback — the entry cap is exact only under one admission writer (§7.2); a raced cohort is not the pre-registered `n` |

**Explicitly NOT a kill criterion:** negative P&L within the gates.
Break-even-minus is the *prior*, not a surprise. Killing the pilot for
confirming the prior would destroy the measurement it exists to collect.

### 7.5 Mandatory rollback — the pilot disables itself

**At the 200th cohort entry, or on ANY kill criterion, the DB row returns to
`enabled = 0`.**

This is not optional and not deferred to analysis. A "bounded pilot" that
reaches its gate but leaves the row enabled while someone reads the numbers is
not bounded — it is an unbounded revival with a report attached.

Note this fires at **admission close**, not at analysis time: the signal is
disabled while the final positions are still resolving. That is deliberate — the
open tail needs to finish for its excursions to be measured, but nothing new
should enter while it does.

Default sequence: **cap entries → disable admission → let the tail resolve →
analyze → separately authorize anything further.** No automatic continuation.
No geometry change merely because the sample completed. `live_eligible` and `tg_alert_eligible` stay `0` throughout and
across the rollback.

Rollback is an audited write (`applied_by='operator'`, reason referencing this
plan), not a silent flip.

### 7.6 Decision rules — an incidence screen, NOT a net-P&L counterfactual

> **SCOPE CORRECTION 2026-08-08.** An earlier revision defined REVISE as *"net
> stop-width change is positive."* **That is not computable from
> `pre_leg1_mae_pct` and the condition is withdrawn.**
>
> The column is a low-water **mark**. It records that a threshold was crossed
> while the stop was eligible; it does **not** record the price on the first tick
> that crossed it. Real stop execution books `current_price` — or
> `max(current_price, gap_floor)` when the gap-fill model is on — so a trade
> ending at `pre_leg1_mae_pct = −25%` is consistent with a hypothetical −12% stop
> having first observed −13%, −18%, or −25%. Those imply materially different
> realized P&L, and nothing stored distinguishes them.
>
> Claiming a net-P&L counterfactual from a mark would be inventing fill precision
> the column does not contain — the same species of error as the retraction in
> §4, one level further in. **The cheapest correct fix is not another column: it
> is to scope the claim to what a mark can support.**

This pilot is a **stop-damage incidence screen**. It answers *how often* each
candidate threshold would have been crossed while the initial stop was eligible,
separately for trades that ended profitable and unprofitable. It does not answer
*how much* P&L a tighter stop would have produced.

> ### ⚠️ CORRECTION 2026-08-08 — `C(X)` was a pseudo-discriminator
>
> A previous revision made REVISE depend on `C(X)`, the share of `closed_sl`
> trades crossing each candidate threshold, and required the rule to hold at all
> three of −10/−12/−15%. **`C(X)` cannot discriminate anything, and the
> three-threshold requirement collapses.**
>
> `closed_sl` fires only when `not floor_armed and current_price <= sl_price`,
> and `sl_pct` is **25**. `pre_leg1_mae_pct` tracks the low-water mark over
> exactly that same pre-floor window, and #516 updates it *before* the SL branch
> on the same tick — so at the moment a trade closes at stop, its recorded
> trough already includes the stop-crossing price. Therefore, for every properly
> instrumented `closed_sl` row:
>
> ```
> pre_leg1_mae_pct ≤ −25%   ⟹   C(−10) = C(−12) = C(−15) = 100%
> ```
>
> And `D` is nested by construction — crossing −15% implies crossing −12% and
> −10% — so `D(−10) ≥ D(−12) ≥ D(−15)`. If `D(−10) ≤ 15%` the looser caps pass
> automatically, and with `C = 100%` both `C − D ≥ 20 pp` and `C ≥ 2 × D` pass
> automatically too.
>
> The elaborate rule was therefore **already** just `D(−10) ≤ 15%`, dressed up as
> four conditions across three thresholds. Stating it plainly is strictly more
> honest and removes a gate that could never fail.

Computed on the eligible cohort (§7.1) only. For each candidate stop
`X ∈ {−10%, −12%, −15%}`:

- **D(X)** = share of *profitable* eligible trades with `pre_leg1_mae_pct ≤ X`
  — the damage-incidence rate: winners a stop at X would have cut short.
  **This is the only discriminating quantity the pilot produces.**
- **C(X)** = share of *`closed_sl`* eligible trades with `pre_leg1_mae_pct ≤ X`
  — **repurposed as a measurement sanity check, not a decision input.**

### C(X) as a sanity check

`C(X)` is expected to be **100% at all three thresholds**. It is a check on the
pilot's own assumptions, and it is valuable precisely because its expected value
is known in advance:

| Observed | Meaning |
|---|---|
| `C(X) = 100%` at all three | instrumentation, cohort and geometry all behaving as modelled |
| `C(X)` materially below 100% | **at least one modelling assumption is false** — the SL geometry changed mid-pilot, the eligible cohort admitted rows it should not have (§7.1), or `pre_leg1_mae_pct` is not tracking the window it claims to |

**If `C(X) < 95%` at any threshold, the pilot returns `NO_VERDICT`.** Report the
incidence table descriptively and investigate the assumption breach; do not read
`D(X)` as meaningful, because whatever broke `C` plausibly broke `D` too.

### Verdict, with explicit precedence

Evaluated in this order. Precedence is required because a cohort can satisfy
RE-SUSPEND *and* REVISE simultaneously — poor realized return with low winner
damage is an entirely reachable combination, and without an ordering the verdict
would depend on which row of a table someone read first.

```
if C(-10) < 95% or C(-12) < 95% or C(-15) < 95%:
    NO_VERDICT      # assumption breach — see the sanity-check table
elif n_eff < 120:
    NO_VERDICT      # under-powered (§7.3)
elif R_pilot <= -2.00%:
    RE-SUSPEND      # the signal's own economics fail, independent of stop width
elif D(-10) <= 15%:
    REVISE          # authorize a separate stop-width experiment
else:
    KEEP
```

| Outcome | Meaning |
|---|---|
| **NO_VERDICT** | sanity check failed or under-powered; record descriptively, change nothing |
| **RE-SUSPEND** | `R_pilot ≤ −2.00%`; signal stays disabled, write up as a retirement argument |
| **REVISE** | `D(−10) ≤ 15%`; **authorize a separate, bounded stop-width experiment** with a pre-registered fill model, as its own proposal and approval. **Not** "the geometry is proven better." |
| **KEEP** | `D(−10) > 15%`; leave geometry unchanged, record the incidence table, signal stays disabled |

**RE-SUSPEND outranks REVISE deliberately.** If the signal's own realized return
is materially negative, tuning its stop is the wrong conversation — the question
becomes whether to run it at all.

`D(−12)` and `D(−15)` are still **reported**, because the shape of the nesting is
informative for designing the follow-on experiment. They are not gates.

**Size-normalisation, stated as a formula.** Compare mean *percentage* return per
trade, never USD — the pilot runs at $150 and the historical cohort at $300, so
USD totals are not comparable (§5).

```
R_pilot      = mean(pnl_pct) over the eligible cohort (§7.1)
R_historical = mean(pnl_pct) over the 330 closed losers_contrarian trades
               opened 2026-04-21 .. 2026-05-17  =  −0.99%
```

`pnl_pct` is already per-trade percentage return, so it is size-invariant by
construction and no rescaling is applied. The RE-SUSPEND threshold is the
**absolute** value `R_pilot ≤ −2.00%`, not a comparison to `R_historical`:
anchoring to a moving historical figure would let the criterion drift as the
reference cohort is re-filtered. −2.00% is set roughly 1pp below the historical
−0.99% so ordinary variation around the prior does not trigger retirement, while
a materially worse cohort does.

`R_historical` is retained as context in the write-up, not as a gate.

All three verdicts end with the signal **disabled** (§7.5). REVISE authorizes a
*further experiment*, never an application and never a geometry change — the
same proposal-only discipline the deterministic learner runs under.

**A fill model is out of scope here and is a precondition of that later
experiment**, not of this one. Defining it means specifying, at minimum: the
execution price for a threshold crossing between evaluator ticks, whether
`PAPER_STOP_GAP_FILL` semantics apply, and how a gap through the stop is booked.
None of that is answerable from stored columns today.

**What the pilot can and cannot conclude:**

- ✅ *Can*: measure **threshold-crossing incidence** while the initial stop is
  eligible — how often each candidate stop would have been touched, split by
  eventual outcome, using a window that matches the stop's real eligibility.
- ❌ *Cannot*: establish that a tighter initial stop is **net-beneficial**. That
  needs a fill model this pilot does not define (§7.6). The strongest available
  verdict is "authorize a separate experiment".
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
2. **#516 + #517 merged** (`pre_leg1_mae_pct` with its cutover guard; the
   enforced entry cap)
3. **Both deployed and verified in prod** — confirm a freshly-opened trade gets
   a non-NULL `pre_leg1_mae_pct`, and that `PAPER_LOSERS_PILOT_MAX_ENTRIES=200`
   is set in `.env`. Merge is not deploy; a plan gated on a column that exists
   only on master would collect 200 unusable rows, and an unset cap silently
   reverts the bound to descriptive.
4. Record `PILOT_T0`, then the single `revive_signal_with_baseline` call —
   `restore_tg_alert_eligible=False`, logged in the approvals table. The
   baseline it stamps is also the cap's cohort anchor, so activation order
   matters: cap configured BEFORE revival.
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
| Ship the entry cap (PR #517) | code + config | operator: "I prefer the real guard if this is going to be called a bounded pilot" | 2026-08-08 |
| Merge #516 | merge | operator: `#516 APPROVED_TO_MERGE` | 2026-08-08 — merged `398c32a7` |
| Merge #517 | merge | operator: `CONTENT APPROVED — rebase after #516 + fresh exact-head CI` | 2026-08-08 — rebased, CI green, merged `e6b8f741` |
| Merge #515 | merge | operator: `APPROVED_TO_MERGE` conditional on truth-maintenance edits + fresh exact-head CI | pending this revision |
| Deploy #516 + #517 to testbed (single combined deploy) | deploy | operator: "I also authorize the single combined deployment of the already-merged #516 + #517" | pending #515 merge |
| **Revive `losers_contrarian` (paper)** | **runtime state change** | **NOT YET REQUESTED** — operator: `LOSERS_CONTRARIAN_REVIVAL_NOT_AUTHORIZED` | — |
| Registry self-description / coverage correction | code + config | NOT YET REQUESTED — separate item | — |

#516 and #517 are **merged**; nothing is **deployed** yet. The revival is
explicitly **not authorized** — the deployment authorization above does not
extend to it. No `revive_signal_with_baseline` call has been made, no `PILOT_T0`
stamped, and no pilot trade opened.

### Review-ruling conformance

| # | Required correction | Where satisfied |
|---|---|---|
| 1 | Keep the managed/unmanaged falsification | §3, unchanged |
| 2 | Retract the whole-life `mae_pct` claim | §4 retraction block |
| 3 | Temporally valid measurement + discriminating tests | PR #516; `tests/test_pre_leg1_adverse_excursion.py`, mutation-tested |
| 4 | n gate uses the valid measurement | §7.1 condition 4, §7.3 |
| 5 | Cohort timestamp / max opens / max notional / rollback / KEEP-REVISE-RE-SUSPEND | §7.1, §7.2, §7.5, §7.6 |
| R2-1 | Remove stale plan file from #516 | rebased onto master; #516 is 5 files, no plan |
| R2-2 | No exact net-P&L claim from a mark | §7.6 scope correction — incidence screen |
| R2-3 | Entry-capped cohort; 60 labelled as tripwire | §7.2 rewrite; §7.3 admission-vs-completion split |
| R2-4 | "every winner arms" corrected; vacuous assert removed | §4 precision note; #516 `row[33]` disjunct deleted |
| R3-1 | Provenance across migration cutover | #516 — pre-cutover rows stay NULL, fail-closed, 3 tests |
| R3-2 | Real admission cap, not a monitored cutoff | **#517** — pre-open guard, 8 tests, 3 mutants killed |
| R3-3 | Remove remaining "net-beneficial" claim | §7.6 verdict wording; "can/cannot" list |
| R3-4 | Quantify "materially exceeds" | ~~§7.6 — `C−D ≥ 20pp` and `C ≥ 2×D`~~ **superseded by R4-2**: those criteria were removable because `C(X)` is structurally 100%, so they could never bind. The surviving quantified gate is `D(−10) ≤ 15%`. |
| R3-5 | Define size-normalisation formula | §7.6 — `R_pilot = mean(pnl_pct)`, absolute gate ≤ −2.00% |
| R4-1 | Cap guarantee scoped to one admission writer | §7.2 concurrency block; K6 |
| R4-2 | `C(X)` is structurally 100% — not a discriminator | §7.6 correction; repurposed as a sanity check with a `NO_VERDICT` floor |
| R4-3 | Define verdict precedence | §7.6 — explicit ordered branch; RE-SUSPEND outranks REVISE |
| 6 | Reclassify §5 per #455 | §5 reclassification block |
| 7 | No production revival | §9 anti-scope; approvals log above |

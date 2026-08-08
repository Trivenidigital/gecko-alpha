# losers_contrarian — bounded revival pilot (PREPARED, NOT EXECUTED)

**New primitives introduced:** NONE

**Date:** 2026-08-08
**Status:** plan only. No DB row changed, no flag flipped, no trade opened.
**Author's headline:** the pilot is cheap and safe to run, but its purpose is
**not** "see if the signal makes money." Its purpose is to collect the one
measurement (`mae_pct`) that every exit-geometry decision depends on and that
**does not exist for any trade before 2026-08**.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trading-signal backtesting / counterfactual replay | none found | build from scratch — n/a here; this plan proposes no new analysis code, only a pre-registered read of existing columns |
| Max-adverse-excursion / stop-width analysis | none found | n/a — `mae_pct` / `trough_price` already shipped in-tree (`scout/trading/evaluator.py:603-625`) |
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

**Run the pilot. Do not run it as a profitability test, and do not touch exit
geometry first.**

Three things changed my initial framing while gathering evidence, and each is
load-bearing:

1. The standing thesis — *"suspensions are an exit problem, not a signal
   problem"* — **cannot be supported by the decomposition that produced it.**
   See §3. The thesis may still be true; the evidence offered for it does not
   discriminate.
2. The exits were **never actually fixed**. PR #504 was docs-only (266 lines,
   one file, zero code). `signal_params_audit` contains **zero** geometry
   changes for any signal since 2026-05-17. The precondition "fix exits before
   reviving" was never met by anything.
3. The decisive counterfactual is **structurally unanswerable on existing
   data**, and the codebase already says so in a comment (§4).

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
| 8 | MAE available for the decision | **0 rows before 2026-08**; 70/70 in August | ❌ **blocking** |

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

## 5. Blocking finding — the sizing registry contradicts itself, in production

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

`scout/trading/trust_sizing.py` consumes it for sizing, `engine.py:436` calls it
at open time, and **`PAPER_TRUST_SIZING_ENABLED=true` in prod.** Verified
empirically, not inferred:

| signal | registry entry | tier | size | n |
|---|---|---|---|---|
| `gainers_early` | **absent** | experimental (default) | **$150** | 239 |
| `volume_spike` | `trusted_experimental` | trusted | $300 | 31 |

All 239 `gainers_early` trades are sized at exactly $150 and all 239 carry
`trust_tier` in `signal_data`. The registry also holds entries for `tg` and `x`,
which are not real `signal_type` values (the real one is `tg_social`) — dead
entries that never match.

Three consequences that touch conclusions already drawn:

1. **The only signal currently trading is silently half-sized** because it is
   *absent* from a file that says it is not used for sizing.
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

Written before execution. Not to be revised after seeing results.

**Primary completion gate:** `n = 200` closed `losers_contrarian` trades with
non-NULL `mae_pct`.

Rationale: at the historical 48.8% win rate that yields ~98 profitable trades,
enough to estimate *P(dipped below candidate stop | ended profitable)* to about
±10pp at 95% — the quantity that decides the stop-width question. Interim
read at `n = 100` (±15pp) is **descriptive only** and may not trigger a change.

**Calendar estimate, not a target:** historical rate was 330 trades / 26 days
≈ 12.7/day → ~16 days. Per §11c, halt at n=200 whenever it arrives; if the rate
collapses, extend rather than concluding on a short sample.

**Kill criteria (any one ends the pilot early):**

| # | Condition | Action |
|---|---|---|
| K1 | auto-suspend fires | let it. Do **not** re-revive; record `n` reached |
| K2 | net ≤ −$400 at n<200 | halt, report; below the −$500 gate by design |
| K3 | `mae_pct` NULL rate > 5% on new closes | halt — instrumentation is broken, and the pilot's only deliverable is void |
| K4 | fill rate < 3/day for 5 consecutive days | halt — supply insufficient; n=200 unreachable in a sane window |

**Explicitly NOT a kill criterion:** negative P&L within the gates. A
break-even-minus result is the *prior*, not a surprise. Killing the pilot for
confirming the prior would destroy the measurement it exists to collect.

**What the pilot can and cannot conclude:**

- ✅ *Can*: whether a tighter stop is net-beneficial, by measuring the winner
  damage that is currently unmeasurable.
- ✅ *Can*: whether the 2026-04/05 peak distribution is stable three months on.
- ❌ *Cannot*: whether the signal is profitable at $300 — sizing differs, and
  USD results do not transfer across a 2× size change (§5).
- ❌ *Cannot*: anything about live execution. Paper only.

---

## 8. Sequencing

1. **This plan reviewed and approved** ← current state
2. §5 registry contradiction filed as its own item (does **not** block the pilot)
3. Single `revive_signal_with_baseline` call, recorded in the approvals log
4. Verify within 24h: trades opening, `signal_type='losers_contrarian'`,
   `amount_usd=150`, `mae_pct` non-NULL on first close, `tg_alert_eligible` still 0
5. Read at n=100 (descriptive), decide at n=200
6. Only then consider a geometry change — as its own audited proposal

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
| **Revive `losers_contrarian` (paper)** | **runtime state change** | **NOT YET REQUESTED** | — |
| Registry / sizing correction | code + config | NOT YET REQUESTED — separate item | — |

No action in the third or fourth row has been taken. This document is the
approval ask for the third.

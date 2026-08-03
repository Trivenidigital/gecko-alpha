# Instrumentation revival — `gainers_early`, paper-only

**New primitives introduced:** NONE. Uses the deployed
`Database.revive_signal_with_baseline` and the `trough_price` / `mae_pct` columns
shipped in #505 (`8aa998df`).

**Date:** 2026-08-03
**Status:** PREPARED — **not executed.** Awaiting operator go.
**Class:** prod-state change (signal enable), paper-only, reversible.

---

## What this is, and what it is not

**This is an instrumentation run to make the system evaluable again. It is NOT a
bet that `gainers_early` is profitable.** It is not, on the evidence: −$3,198
lifetime over n=673 at 50.7% win. Nobody should expect this run to make money.

The purpose is to restart data collection. See §2 for why nothing else can.

## 1. The paper pipeline has been dead since 2026-07-27

| date | paper trades opened |
|---|---|
| 2026-07-20 … 07-26 | 13–26 / day |
| **2026-07-27** | **1** |
| 07-28 … 07-30 | 0 |
| 07-31, 08-01 | 1 each (anchor rows for the live lanes, not trades) |
| 08-02, 08-03 | 0 |

2026-07-27T01:04:14 is exactly when `gainers_early` — the last signal producing
volume — was auto-suspended. `signal_params.enabled=0` blocks **paper** trades
too (`scout/trading/engine.py:401`), so the evidence pipeline stopped with it.

Consequence: **no evidence of any kind is accumulating.** Every open question
from the exit-mechanics analysis (PR #504) and the MAE instrumentation (#505) is
unanswerable until something generates trades. #505 currently records zero rows.

## 2. The revival criteria cannot be satisfied from a standstill

Ran the project's own gate (`scout/trading/revival_criteria.py`) against all four
plausible candidates. **All four return `STRATIFICATION_INFEASIBLE`** — not a
rejection, an inability to evaluate:

| signal | n | cutover age | verdict |
|---|---|---|---|
| gainers_early | 673 | 7d | `STRATIFICATION_INFEASIBLE` |
| losers_contrarian | 330 | 78d | `STRATIFICATION_INFEASIBLE` |
| first_signal | 277 | 35d | `STRATIFICATION_INFEASIBLE` |
| chain_completed | 185 | 58d | `STRATIFICATION_INFEASIBLE` |

Failure reason in every case: *"cutover … cannot split into two >= 7d / >=
50-trade windows."*

**This is a catch-22, and it is structural.** The criteria compare a pre-cutover
window against a post-cutover window. Suspension is what creates the cutover —
and it simultaneously guarantees no post-cutover trades will ever exist. A
suspended signal therefore can never produce a revival verdict, no matter how
long it waits or how much the mechanics improve.

So this run is not circumventing the gate. **The gate is unsatisfiable while
suspended, and this run is what makes it evaluable.** Worth filing as a defect
in its own right — the revival machinery has no path from `suspended` to any
verdict other than `INFEASIBLE`.

## 3. Why `gainers_early`

- **Highest volume** — 673 closed trades lifetime; produced 13–26 opens/day, so
  it reaches a usable n fastest. Every other candidate is slower.
- **Most recently suspended** (7d) so its behaviour is closest to current
  mechanics; the others are 35–78d stale.
- **Cool-off satisfied.** `SIGNAL_REVIVAL_MIN_SOAK_DAYS=7`; last *operator*
  revival was 2026-07-17T12:28:52 (17d ago). The 07-27 suspension was
  `auto_suspend`, and the cool-off only counts `applied_by='operator'`. **No
  `force=True` needed** — do not pass it.
- Its loss profile is the system-typical one, so the MAE it produces generalises.

## 4. The exact change

```python
# Run on srilu-vps, /root/gecko-alpha
import asyncio
from scout.db import Database
from scout.config import Settings

async def main():
    db = Database("/root/gecko-alpha/scout.db")
    await db.initialize()
    await db.revive_signal_with_baseline(
        "gainers_early",
        reason=(
            "INSTRUMENTATION RUN (not a profitability bet). Paper-only, "
            "live_eligible=0. Restarts the evidence pipeline, dead since "
            "2026-07-27, and populates trough_price/mae_pct (#505) so stop "
            "width becomes evaluable. Revival criteria return "
            "STRATIFICATION_INFEASIBLE for every suspended signal — the gate "
            "needs post-cutover data that suspension prevents existing, so "
            "this run is what makes a verdict possible. Stop rule "
            "pre-registered in tasks/plan_gainers_early_instrumentation_"
            "revival_2026_08_03.md."
        ),
        operator="operator",
        settings=Settings(),
    )
    await db.close()

asyncio.run(main())
```

`revive_signal_with_baseline` sets `enabled=1`, stamps
`drawdown_baseline_at=NOW()` so pre-revival drawdown does not immediately
re-trip auto-suspend, and writes the audit row — atomically.

**Guard rails that must remain 0 and are NOT touched by this call:**
`live_eligible=0` (no real money), `tg_alert_eligible=0` (no operator alerts).
Verify both after the call.

## 5. Pre-registered stop rule (§11a — data-bound, not calendar-bound)

**Primary target: n ≥ 100 closed trades with `mae_pct IS NOT NULL`.** That is the
dataset the stop-width question needs. Not "wait N days".

Expected duration — and it is longer than the open rate suggests.
`gainers_early.max_duration_hours = 168` (7 days, **not** the global
`PAPER_MAX_DURATION_HOURS=48`), so trades opened on day 1 may not close until
day 8. At 13–26 opens/day, expect **~2 weeks to n=100 closed**, not one.

**This run is self-limiting.** `SIGNAL_SUSPEND_HARD_LOSS_USD=-500` bypasses the
50-trade floor. At the historical −$4.75/trade, that trips at roughly **105
closed trades** — which lands near the data target. If auto-suspend fires first,
**let it.** Do not re-revive to chase the target; take the n you have.

**Halt early and analyse if** n=100 is reached sooner (§11c — do not run to
calendar completion once the data threshold is met).

**Abandon if** after 14 days fewer than 30 closed trades have `mae_pct` set —
that would mean the column is not being populated and the instrumentation is
broken, which is a bug to fix, not data to wait for.

## 6. What this run answers

1. **Stop width.** With `mae_pct`, the cost side becomes computable: how many
   *winners* dipped past a tighter stop before recovering. Today only the saving
   side is measurable, which makes tightening look strictly beneficial.
2. **The <10%-peak band.** 941 historical trades, −$32,029 — the entire system
   loss. MAE tells us whether they go straight down (cut earlier) or chop around
   entry before dying (a different fix).
3. **A revival verdict.** Post-cutover trades are exactly the window-B data the
   criteria need, so this run converts `STRATIFICATION_INFEASIBLE` into an
   actual verdict for the first time.

## 7. Rollback

Immediate, one statement, no data loss:

```sql
UPDATE signal_params SET enabled = 0 WHERE signal_type = 'gainers_early';
```

Paper trades already open continue to be evaluated and closed normally; MAE rows
already collected are kept. Nothing about this run touches real money — the
`live_eligible=0` flag is not modified, and the live mandate is independently
`DISABLED`.

## 8. Approvals

| Action | Class | Status |
|---|---|---|
| Deploy #505 (MAE columns) | deploy, additive | DONE — `8aa998df`, verified on prod |
| Enable `gainers_early` paper-only | prod-state, reversible | **PENDING operator go** |
| Any change to `live_eligible` | live-money | NOT REQUESTED, NOT AUTHORIZED |
| Any change to stop width | trade behaviour | NOT REQUESTED — blocked on this run's data |

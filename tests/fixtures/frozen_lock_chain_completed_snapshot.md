# Regression fixture — frozen-suppression lock (transition + steady-state)

Named regression fixture for `fix/frozen-suppression-lock` (funnel investigation
part iv; forensic diagnosis 2026-07-04, report appendix Q). Documents the
real-world case the fix prevents: a suppressed combo that stops trading, falls
out of the 30-day refresh selection, is never refreshed again, and latches at
`parole_exhausted` FOREVER with no operator notification — a §12b-class
permanent state change driven by a refresh-window artifact, not a decision.

Referenced by `tests/test_trading_combo_refresh.py::
test_chain_completed_frozen_lock_regression`.

## The selection predicate + the boundary artifact (corrected 2026-07-04)

Deployed `refresh_all` (b23ef0a2) selects combos to refresh with:

```sql
SELECT DISTINCT signal_combo FROM paper_trades
WHERE signal_combo IS NOT NULL AND opened_at >= datetime('now', '-30 days')
```

Intent is a strict timestamp `>=`, but it is a SQLite STRING comparison of two
MISMATCHED datetime formats: `opened_at` is stored ISO with a `T` separator and
`+00:00` timezone (`"2026-06-04T02:31:52.497424+00:00"`), while
`datetime('now','-30 days')` produces a space-separated, tz-less string
(`"2026-06-04 03:04:08"`). At character 10, `'T'` (0x54) > `' '` (0x20), so on
the boundary DAY (equal date portions) `opened_at` compares `>=` the cutoff
REGARDLESS of the time-of-day. The predicate behaves as `DATE() >=` by accident.
Consequence: a combo whose newest open is on day D stays in-window for ALL of
day D+30, and exits at the D+31 refresh (when the cutoff date rolls forward).
This is off-by-one #4 (report appendix Q); the general-selection fix is tracked
as `BL-DATETIME-NORMALIZATION`.

## Transition fixture — chain_completed

Newest `signal_combo='chain_completed'` open = **2026-06-04T02:31:52Z**. By the
artifact above it stayed in-window through all of 2026-07-04 and **exits at the
2026-07-05 ~03:00Z refresh** (cutoff date rolls to 2026-06-05).

### Before-snapshot (2026-07-04 post-refresh — still in-window & cycling)

`combo_performance`, `window='30d'`, `combo_key='chain_completed'`:

| field                    | value                       |
|--------------------------|-----------------------------|
| suppressed               | 1                           |
| suppressed_at            | 2026-07-04T03:04:08Z        |
| parole_at                | 2026-07-18T03:04:08Z        |
| parole_trades_remaining  | 5                           |
| last_refreshed           | 2026-07-04T03:04:08Z        |
| trades (30d)             | 51                          |
| wins                     | 2                           |
| win_rate_pct             | 3.92 %                      |

State = **cycling, not latched**: in-window, exhausted parole then re-suppressed
(remaining reset 5, parole +14d). At 3.92 % WR it will never un-suppress
(< 30 % threshold) — a perpetual-but-correct re-test of a genuine loser.

### After-snapshot (post-2026-07-05 ~03:00Z exit)

_Appended when the 2026-07-05 03:00Z refresh runs. Expected under the OLD
deployed code: `chain_completed` drops out of the selection, `last_refreshed`
stops advancing (stays 2026-07-04), parole frozen — the latch. Under the FIXED
code (#424 deployed): still refreshed via the suppressed-widened selection,
state preserved, one §12b alert._

<!-- capture: paste the post-2026-07-05-03:00Z combo_performance row here -->

## Steady-state fixtures — already latched (confirmed 2026-07-04)

Two combos crossed the boundary weeks ago and are frozen NOW — `remaining=0`,
`parole_at` in the past, un-refreshed since mid-June, so `should_open` returns
`parole_exhausted` forever with no re-evaluation. Genuine losers (earned bans);
the MECHANISM (window-artifact latch) is the defect the fix ends.

| combo              | newest combo-open | frozen since (last_refreshed) | remaining | parole_at (past)  | 30d WR |
|--------------------|-------------------|-------------------------------|-----------|-------------------|--------|
| gainers_early      | 2026-05-18        | 2026-06-17                    | 0         | 2026-06-26        | 15.3 % |
| losers_contrarian  | 2026-05-17        | 2026-06-16                    | 0         | 2026-06-24        | 12.2 % |

## What the fix guarantees — and why constraint (a) is load-bearing

1. `refresh_all` refreshes a `suppressed=1` combo even with zero trades in the
   window (widened selection), keeping it live/refreshable — no silent latch.
2. **Constraint (a) — the zero-trade preserve guard.** A naive force-refresh of
   a frozen zero-trade combo would hit the `remaining<=0 → re-suppress` branch
   (WR on 0 trades reads < threshold), RESETTING `parole_at` to now+14d and
   `remaining` to 5 — an accidental SOFT-REVIVAL that re-arms parole-retest
   trades. The guard PRESERVES the state verbatim (suppressed=1, remaining and
   parole_at unchanged) so no revival occurs. Validated by the 2026-07-04 soak:
   the obvious fix would have caused a worse bug.
3. A §12b operator Telegram alert fires ONCE on entry into permanent-suppression
   ("revival requires explicit operator action via revive_signal_with_baseline"),
   deduped via `combo_performance.perm_suppression_alerted_at`.

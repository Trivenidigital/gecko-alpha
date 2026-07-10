# Regression fixture — chain_completed frozen-suppression lock

Named regression fixture for `fix/frozen-suppression-lock` (funnel
investigation part iv). This documents the real-world case the fix prevents:
a suppressed combo that stops trading, falls out of the trade-only 30-day
refresh window, is never refreshed again, and latches at `parole_exhausted`
FOREVER with no operator notification — a §12b-class permanent state change
driven by a refresh-window artifact, not a decision.

Referenced by `tests/test_trading_combo_refresh.py::
test_chain_completed_frozen_lock_regression`.

## Before-snapshot (captured 2026-07-03, pre-latch)

`combo_performance` row, `window = '30d'`, `combo_key = 'chain_completed'`:

| field                       | value                    |
|-----------------------------|--------------------------|
| suppressed                  | 1                        |
| suppressed_at               | 2026-06-19               |
| last_open (max opened_at)   | 2026-06-04               |
| parole_at                   | 2026-07-03T03:00:00Z     |
| parole_trades_remaining     | 5                        |
| last_refreshed              | 2026-07-02T03:00:00Z     |
| trades                      | 63                       |
| wins                        | 4                        |
| win_rate_pct                | 6.35 %                   |

Mechanics: as of 2026-07-03 the last trade (2026-06-04) is still inside the
30-day window, so the OLD `refresh_all` query still selects `chain_completed`.
At the 2026-07-04 03:00Z nightly refresh, 2026-06-04 drops OUTSIDE the 30-day
window. Under the old trade-only selection the combo would no longer be
refreshed, freezing its parole state permanently. Two sibling combos
(`gainers_early`, `losers_contrarian`, last trades mid-May) were ALREADY
permanently locked this way.

## What the fix guarantees on this row

1. `refresh_all` still refreshes `chain_completed` after 2026-07-04 because it
   is `suppressed = 1` (widened selection), keeping the row live/refreshable.
2. `refresh_combo` PRESERVES the suppression state verbatim for a zero-trade
   suppressed combo — `suppressed` stays `1`, `parole_trades_remaining` stays
   `5`, `parole_at` unchanged. No auto-revival (constraint a).
3. A §12b operator Telegram alert fires ONCE on entry into the state
   ("permanent-suppression state ... revival requires explicit operator action
   via revive_signal_with_baseline"), deduped via
   `combo_performance.perm_suppression_alerted_at`.

## After-snapshot (post-2026-07-04 03:00Z latch)

_To be appended by the operator when captured on/after 2026-07-04._

<!-- operator: paste the post-latch combo_performance row here -->

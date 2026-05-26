# Trader Surface Path Trace: TOES-Style Winners

Date: 2026-05-26

## Purpose

Diagnose why Top Gainers Tracker wins such as TOES, BSB, BILL, UB, TROLL, and
ASTEROID were visible in research output but did not become a concentrated
trader-facing queue.

This is a diagnostic artifact only. It does not propose or ship trading-policy,
ranking, alerting, or dashboard changes.

## Pinned Universe

- **Motivating tokens:** TOES, BSB, BILL, UB, TROLL, ASTEROID from
  `C:\Testing\11.png`.
- **Actionable window:** first `gainers_comparisons.appeared_on_gainers_at`.
- **Successful surface:** token appears in Now Tradable, Trade Inbox, or the
  Action Queue with a reviewable/watch state within +/-24h of the actionable
  window.
- **Runner truth:** not used for live ranking or diagnosis. Later-runner outcome
  is offline-only and immature for rows after about 2026-05-21 because the prior
  stop-loss audit found ~131h average time-to-runner-board.

## Pre-Registered Hypothesis

Before running the focused path trace, the highest-prior hypothesis was:

> TOES-style tokens are in the Top Gainers / markets-watcher corpus but not the
> scorer/paper-trade corpus, so they never reach the paper-trade-backed cockpit.

That hypothesis was only partly correct. The stronger terminating lever in this
sample is: **the gainers_early paper-trade path attempted to open, but the
signal was disabled in `signal_params`, so no paper row existed for the
paper-backed trader surfaces to display.**

## Existing Primitives Drift-Check

| Gate | Existing primitive | Evidence | Residual gap |
|---|---|---|---|
| Tradability firewall | Actionability Gate v1 | `scout/trading/actionability.py` | Only applies after a paper-trade open. |
| Trader cockpit | `/api/live_candidates` | `dashboard/db.py:get_live_candidates` scans `paper_trades.status='open'` | Tracker-only winners without paper rows are invisible. |
| Trade Inbox | `/api/trade_inbox` + `TradeInboxTab.jsx` | `dashboard/db.py:get_trade_inbox` also scans `paper_trades.status='open'`; PR #273 added grouping/score UI | Solves broad grouping over open paper rows, but not tracker-only rows. |
| Cockpit buckets | `candidate_review`, `watch`, `blocked`, `data_insufficient` | `dashboard/db.py` verdict logic | Buckets are downstream of paper rows. |
| Trader Action Queue | client-side bucket cards over open positions | `dashboard/frontend/components/traderQueue.js` | Also downstream of open paper positions. |
| Top Gainers linkage | sources + most recent paper outcome | `dashboard/api.py` `/api/gainers/comparisons` enrichment | Research table, not a scarce trader queue. |

Conclusion: do not build a parallel trader surface. The existing surfaces are
real, and PR #273 already shipped the grouped Trade Inbox. The remaining gap is
the input corpus: both Now Tradable and Trade Inbox are downstream of open
paper rows, so tracker-only wins remain invisible there.

## Path-Trace Evidence

Runtime config/code facts:

- Scorer corpus: `MIN_MARKET_CAP=10_000`, `MAX_MARKET_CAP=500_000`
  (`scout/config.py`).
- Top Gainers watcher corpus: `GAINERS_MAX_MCAP=500_000_000`.
- Paper-trade admission corpus: `PAPER_MIN_MCAP=5_000_000`,
  `PAPER_MAX_MCAP=500_000_000`, `PAPER_GAINERS_MAX_24H_PCT=50.0`.
- `gainers_early` signal params on prod: `enabled=0`,
  `suspended_reason=hard_loss`, `updated_at=2026-05-19T01:02:14.744149+00:00`.
- Paper-trade engine short-circuits disabled signals before price, exposure, or
  live-eligibility checks (`trade_skipped_signal_disabled` in
  `scout/trading/engine.py`).

Per-token trace at first Top Gainers comparison:

| Token | Tracker entry | Mcap at tracker | 24h % near tracker | Score max | Gate events | Paper rows +/-24h | Journal terminating evidence | Terminating lever |
|---|---:|---:|---:|---:|---:|---:|---|---|
| BSB | 2026-05-24 08:24Z | $313.2M | +22.1% | 19 | 0 | 0 | `trade_skipped_signal_disabled`, `signal_type=gainers_early` at 08:24Z | disabled paper signal |
| TROLL | 2026-05-24 09:02Z | $120.0M | +22.5% | 16 | 0 | 0 near tracker; old `gainers_early` row still open from 2026-05-17 | existing open row + disabled paper signal | duplicate/open-state plus disabled signal |
| UB | 2026-05-25 01:18Z | $403.3M | +20.0% | 9 | 0 | 0 | repeated `trade_skipped_signal_disabled`, `signal_type=gainers_early` before/around window | disabled paper signal |
| BILL | 2026-05-25 06:07Z | $275.3M | +20.6% | 16 | 0 | 0 | repeated `trade_skipped_signal_disabled`, `signal_type=gainers_early`; also `trending_catch` disabled | disabled paper signal |
| TOES | 2026-05-25 08:10Z | $16.5M | +29.4% | 9 | 0 | 0 | `trade_skipped_signal_disabled`, `signal_type=gainers_early` at 08:10Z and repeats | disabled paper signal |
| ASTEROID | 2026-05-25 12:41Z | $16.1M | +53.2% | 0 | 0 | 0 | paper gainers late-pump filter would reject >50% 24h if signal were enabled | late-pump filter + zero scorer |

Aggregate terminating levers:

| Terminating lever | Count | Notes |
|---|---:|---|
| Disabled `gainers_early` paper signal | 5/6 direct; 1/6 mixed | Confirmed by journal for BSB/UB/BILL/TOES and repeated around the window. |
| Existing open duplicate state | 1/6 | TROLL had an old open `gainers_early` row from 2026-05-17. |
| Late-pump filter | 1/6 | ASTEROID was already above `PAPER_GAINERS_MAX_24H_PCT=50`. |
| FCFS/live-slot full | 0/6 observed | No fresh paper rows existed, so `would_be_live` could not be the terminating lever. |
| Conviction gate blocked | 0/6 observed for tracker window | No `conviction_gated` events for the relevant token ids in `signal_events`. |
| Scorer-corpus mismatch | partial contributor | Max scores are low, but the paper-trade path did attempt gainers entries; the immediate blocker was disabled signal params. |

## Interpretation

The current cockpit/inbox is not "illegible"; it is downstream of
`paper_trades`. For this sample, the system detected the tokens in Top Gainers,
then the paper-trade entry path declined to create rows because `gainers_early`
was disabled after a hard-loss auto-suspension. Since `/api/live_candidates` and
`/api/trade_inbox` both scan open paper rows, those tracker wins could not enter
the trader queue.

This means an urgency-state classifier is not the next build. PR #273 already
added grouped inbox semantics over the available corpus. A classifier over the
same missing corpus would not have surfaced TOES.

## Branch Decision

Smallest build, if we choose to build:

1. **Tracker-to-Trade-Inbox promotion path, read-only / watch-only.**
   Add Top Gainers comparison rows as a second source corpus for the existing
   Trade Inbox even when paper trading is disabled or no paper row exists. Label
   them separately from paper-backed candidates.
2. **Persist gate-decision events.**
   For each promoted/blocked tracker candidate, record why it is present or
   absent: disabled signal, stale price, late-pump filter, missing price, no
   source confidence, duplicate open position, or not enough metadata.
3. **Only after promotion produces enough rows, scope urgency.**
   Gate urgency-state design on a measured queue fire rate, for example
   `>= N promoted tracker candidates/day` or `>= N concurrent watch candidates`
   for multiple days.

Do not re-enable `gainers_early` or loosen paper-trade policy from this finding
alone. The point is trader visibility of detector wins, not automatic entry.

## Follow-Up Questions Before Build

- Should tracker-promoted rows live directly in `/api/trade_inbox` as a second
  source corpus, or should they have a separate `/api/tracker_candidates`
  endpoint that the existing Trade Inbox UI composes?
- What is the watch-only retention window: 6h, 24h, or until the token leaves
  Top Gainers?
- Should Telegram alert on tracker promotion, or should the first version be
  dashboard-only to measure noise?

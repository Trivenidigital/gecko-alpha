**New primitives introduced:** ONE — the *detection-time alert lane*
(`scout/trading/detection_alert.py`), a flag-gated, default-OFF operator
alert path that fires on the SCORING pass (before the paper dispatch gate)
rather than on paper-trade OPEN. No new DB table, no new schema_version, no
new CHECK value — it reuses `tg_alert_log` with `signal_type='detection_lane'`
and `detail='detection_lane[:reason]'`. Three config knobs
(`DETECTION_ALERT_LANE_ENABLED`, `DETECTION_ALERT_MAX_PER_DAY`,
`DETECTION_ALERT_MAX_AGE_MIN`).

# Design — Detection-Time Alert Lane (ALR-02)

**Status:** design + flag-gated skeleton (default OFF). Backlog item ALR-02,
`tasks/backlog_fable_analysis_2026_07_10.md`.

## Problem (the review's core reframe)

Every existing trader-facing Telegram alert (`tg_alert_dispatch.py`
`notify_paper_trade_opened`) fires on **paper-trade OPEN**. That path sits
*downstream* of the dispatch gate, which rejects ~99.99% of scored
candidates. Two consequences:

1. The operator only ever hears about tokens the robot already decided to
   trade — inheriting the same near-total rejection.
2. The message semantics are "the robot acted", not "an early candidate is
   here". The product's central promise — *beat CoinGecko Highlights by
   minutes* — is unserved on the alert surface, even though the earliness
   data (`_compute_lead_time_vs_trending`) already exists.

A trader wants **"early candidate detected"** surfaced *before* the robot
decides. That is a fundamentally different lane, not a tweak to the paper
alert body (which ALR-01 already handles).

## Hermes-first analysis

Checked against the Hermes skill hub (`hermes-agent.nousresearch.com/docs/skills`)
+ awesome-hermes-agent ecosystem.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram message dispatch | none applicable — delivery already goes through the project's own `scout.alerter.send_telegram_message` (pacing, chat routing, parse-mode hygiene all in-tree) | reuse in-tree `alerter` |
| Per-token dedup / rate limiting | none — this is project-specific SQLite state (`tg_alert_log`), not a generic capability | reuse in-tree `tg_alert_log` + a COUNT query |
| Earliness vs trending reference | none — bespoke to this project's `trending_snapshots` table and `_compute_lead_time_vs_trending` sign convention | reuse in-tree `engine._compute_lead_time_vs_trending` |
| Universe / junk filtering | none — reuses the operator universe filter shipped for the paper lane | reuse in-tree `tg_alert_dispatch._check_universe` |

**awesome-hermes-agent ecosystem check + verdict:** the lane is entirely a
composition of *existing project primitives* (alerter, `tg_alert_log`,
`trending_snapshots`, the lead-time helper, the universe filter). No external
Hermes skill or library covers "fire a bounded, deduped early-detection alert
against our own scored-candidate + trending state" — that is inherently
internal plumbing. Build from in-tree reuse; no external dependency.

## Trigger definition (decision log)

Fires for a candidate iff **all** of:

1. **CG-sourced** — `chain == 'coingecko'`. The coingecko.com link, the
   `price_cache` key, and the trending reference are all keyed on the CG
   slug; DEX-address candidates would (a) always read `no_reference` and (b)
   render a broken link. So the lane is CG-only by construction (mirrors
   `trade_first_signals`, which also restricts to `chain == 'coingecko'`).
2. **Fresh** — the candidate's *authoritative* `candidates.first_seen_at`
   (the upsert preserves the earliest sighting, db.py:7153) is within
   `DETECTION_ALERT_MAX_AGE_MIN` of now. This operationalizes "candidate
   *first seen*": the lane surfaces genuinely-new detections, not stale
   not-yet-trending coins re-scored every cycle. The in-memory
   `CandidateToken.first_seen_at` is NOT used (it defaults to construction
   time and would read ~0 for a token seen days ago).
3. **Early vs CG trending** — the trending-earliness predicate
   `_detection_trigger(lead_time_min, status)` returns True:
   - `status == 'no_reference'` — the coin has **never** appeared on CG
     trending (we are entirely ahead of the crossover), **OR**
   - `status == 'ok' AND lead_time_min < 0` — a trending crossover exists but
     it is **later** than the detection instant (still early).

   **Sign convention (load-bearing, matches `engine._compute_lead_time_vs_trending`):**
   NEGATIVE `lead_time` = detected BEFORE the coin trended (early / good);
   POSITIVE = detected AFTER (late). `status == 'ok'` with `lead_time >= 0`
   (already trending / late) and `status == 'error'` do **not** fire. The
   spec's phrase "lead-time positive-early" is read *semantically* — "the
   lead-time affirms earliness" — which under this convention is
   `lead_time_min < 0`. This interpretation is called out explicitly here
   because the codebase has a documented history of sign-flip bugs on this
   exact column (moonshot floor, low-peak lock).

In the live lane `now = datetime.now(utc)`, so a fresh candidate is almost
always `no_reference` (not trending yet) → fire. The `ok + negative` branch
matters mostly for the backtest, where `now` is replayed as the candidate's
own `first_seen_at`.

## Noise budget (decision log)

A new alert surface is a new way to erode operator trust. Three stacked
bounds, all reusing existing state:

- **Daily cap** `DETECTION_ALERT_MAX_PER_DAY` (default **5**). Counted as
  `tg_alert_log` rows with `outcome='sent' AND detail='detection_lane'`
  since UTC midnight. When the cap is hit, further candidates are audited
  `outcome='blocked_cooldown' detail='detection_lane:rate_limit'` and NOT
  sent. Within a cycle the remaining budget is spent **freshest-first**
  (ascending age) so the newest detections win the scarce slots.
- **Per-token 24h dedup** — reuses `TG_ALERT_DEDUP_WINDOW_HOURS` (default 24;
  `0` disables). A token that already produced a `sent` detection-lane row in
  the window is audited `outcome='blocked_cooldown'
  detail='detection_lane:dedup_24h'` and NOT re-sent. The dedup is scoped to
  `detail='detection_lane'` so it is **independent** of the paper-open alert
  lane (a paper alert never suppresses a detection alert and vice-versa).
- **Freshness ceiling** `DETECTION_ALERT_MAX_AGE_MIN` (default **180**) —
  see trigger (2). Stops the lane from re-surfacing week-old not-yet-trending
  coins.

Only `outcome='sent'` rows consume the daily budget and set the dedup — a
`dispatch_failed` send neither burns budget nor claims dedup (parity with the
paper lane's demote-on-failure semantics), so a transient Telegram error does
not silently swallow the next legitimate fire.

## Dedup / audit reuse (decision log)

No schema change. Every decision writes ONE `tg_alert_log` row for audit:

| Situation | `outcome` | `detail` |
|---|---|---|
| sent | `sent` | `detection_lane` |
| universe-filtered | `blocked_eligibility` | `detection_lane:universe_filter:<pattern>` |
| 24h dedup hit | `blocked_cooldown` | `detection_lane:dedup_24h` |
| daily cap hit | `blocked_cooldown` | `detection_lane:rate_limit` |
| Telegram send failed | `dispatch_failed` | `detection_lane` |

All rows carry `signal_type='detection_lane'`, `paper_trade_id=NULL` (there is
no trade yet — the whole point). Every value used is already in the
`tg_alert_log` CHECK constraint (`sent`, `blocked_eligibility`,
`blocked_cooldown`, `dispatch_failed`), so there is **no CHECK change and no
migration**. A candidate that simply isn't *early* (fails the trigger) writes
**no** row — it is not a detection event, and logging every non-event would
swamp the table.

## Universe filter reuse (decision log)

The operator universe filter (`ALERT_UNIVERSE_FILTER_ENABLED` +
`ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS`, currently `-tokenized-`) is reused
verbatim via `tg_alert_dispatch._check_universe`. When the universe flag is
ON, a tokenized-equity/ETF slug is suppressed on the detection lane exactly as
on the paper lane. When the universe flag is OFF, `_check_universe` returns
`None` and the lane is unaffected. (The universe flag is orthogonal to the
detection-lane flag; the detection lane simply calls the same guard.)

## Kill switch (decision log)

`DETECTION_ALERT_LANE_ENABLED` (default **False**). When OFF:

- The `run_cycle` hook does not even construct the fire-and-forget task
  (`if settings.DETECTION_ALERT_LANE_ENABLED and all_scored_tokens:`), so the
  lane is byte-for-byte inert and costs nothing.
- `notify_early_detections` re-checks the flag as defense-in-depth and returns
  immediately, so a direct caller (test / future surface) can't bypass it.

Setting `DETECTION_ALERT_MAX_PER_DAY=0` is a second, softer off-switch (cap
reached before the first send). Flipping the flag OFF is a pure revert.

## Data path / hook point

`scout/main.py::run_cycle`, immediately after the Stage-3 scoring loop and the
paper-trade block (candidates already `upsert_candidate`-ed, so
`candidates.first_seen_at` is authoritative). Fire-and-forget, mirroring
`notify_paper_trade_opened`'s task pattern (module-level ref set
`_detection_alert_tasks` to prevent mid-flight GC; a done-callback surfaces
exceptions). The orchestrator itself never raises — a bug in the lane can
never break the pipeline cycle.

```
scoring loop (upsert_candidate) → paper-trade block
    → [flag] notify_early_detections(candidates=all_scored_tokens)
          per candidate (freshest-first, budget-bounded):
            CG-only? → fresh? → universe? → trigger(lead_time)? → dedup? →
            budget? → send (parse_mode=None) → tg_alert_log row
```

`parse_mode=None` on the send (the body is plain text; the header contains no
Markdown, but this is the standing rule for system/trader alerts — global
CLAUDE.md §12b). `_alert_dispatched` / `_alert_delivered` structured logs
bracket the send so every fire is traceable in journalctl regardless of
delivery outcome.

## Alert body

```
🔎 EARLY DETECT · WIF · $0.0234 · $45.0M
first seen 8 min ago · not yet on CG trending
coingecko.com/en/coins/dogwifhat
Dashboard: http://89.167.116.187:8000/#/token/dogwifhat
```

- Header reuses `_fmt_price` / `_fmt_mcap` from `tg_alert_dispatch`
  (card-v2 formatting parity).
- Freshness line renders `not yet on CG trending` for `no_reference`, or
  `<N> min ahead of CG trending` for the `ok + negative` branch.
- The dashboard link uses the ALR-09 **token** route `/#/token/{coin_id}`
  (there is no trade row). Omitted when `DASHBOARD_BASE_URL` is empty.

## VALIDATE — backtest (`scripts/detection_lane_backtest.py`)

Read-only, synchronous `sqlite3` (runs anywhere — no aiohttp/async deps).
Replays the trigger over the last 7 days of `candidates`, using each
candidate's own `first_seen_at` as the historical `now`, and reports:

- how many CG candidates **would have fired** (split: `no_reference` vs
  `ahead-of-crossover`),
- for the fired set, how many *later* appeared on CG trending (i.e. the run
  we were early to) and the median minutes of lead,
- how many were excluded by the universe filter.

**Hard limitation, stated up front:** `trending_snapshots` and the
gainers/price history have only **~7-day retention** (backlog DASH-05). So
the "would it have fired pre-run on an ANSEM-class monster?" question is only
answerable for monsters whose full first-seen → trending-crossover arc falls
inside the 7-day window. For older monsters the crossover reference has been
pruned, so the backtest under-counts early catches and **cannot** reconstruct
peak-gain attribution. This is a coverage floor, not a ceiling: a positive
result is real; a null result may be retention-blinded. A forward soak (lane
enabled, `MAX_PER_DAY` small) is the honest way to measure catch quality on
the next monster.

## Test plan (TDD)

`tests/test_detection_alert.py`:

- `_detection_trigger` predicate: `no_reference`→fire; `ok`+negative→fire;
  `ok`+zero→no; `ok`+positive→no; `error`→no; `ok`+None→no.
- `format_detection_alert` golden-file (exact body string).
- flag-OFF → inert (no send, no rows).
- universe reuse (flag ON) → `blocked_eligibility`
  `detection_lane:universe_filter:-tokenized-`, no send.
- daily rate-limit → `MAX_PER_DAY` respected; overflow →
  `blocked_cooldown detection_lane:rate_limit`, no send.
- 24h dedup → prior `sent` row suppresses re-send →
  `blocked_cooldown detection_lane:dedup_24h`, no send.
- happy path → fresh CG candidate, no trending → `sent` row + body sent.
- already-trending (positive lead) → no fire.

# Findings — implicit state machines in gecko-alpha (2026-11-19)

Surveyed at master `0f7fa3af` (PR #600). Scope: `scout/`, `dashboard/`
(backend and frontend), `scripts/`, `cron/`, `systemd/` — about 77k lines of
Python plus the React frontend.

A state machine here is an entity that moves through a finite set of named
states where only some moves are legitimate. It is *implicit* when the states
or the legal moves are not declared in one place: status strings spelled at
the write sites, a state that only exists as a combination of nullable
columns, or an UPDATE that never checks what the state was before.

About 70 machines were found. 22 are now declared in
`scout/statemachines/registry.py` (reference: `docs/state_machines.md`); the
rest are catalogued in section 3. The registry entries were written from the
code and then corrected after two independent reviews; how much of each entry
a test holds in place is stated per machine in the reference ("Checked by
tests").

## 1. What the survey showed

- **Two machines were already explicit.** `solana_executions.state` has a
  transition table and a guard that raises (`scout/live/solana_lane.py:481`,
  `:522`); the Robinhood/Pons lifecycle has an ordering map and a forward-only
  function (`scout/ingestion/rh_pons.py`). Everything else was implicit.
- **The most important machine had no declaration at all.**
  `paper_trades.status` has no CHECK constraint; its 13 values are spelled in
  roughly ten evaluator branches plus a dict in `scout/trading/paper.py`, and
  the one type that lists them (`TradeStatus`) is missing three.
- **The same state is re-derived differently by each reader.** "Closed" is
  `status != 'open'`, `LIKE 'closed_%'`, `LIKE 'closed%'`, an explicit tuple,
  or a provenance predicate, depending on the module. "Suspended" is three
  states in the dashboard backend and one in a frontend tab.
- **Several states exist only as column combinations**, so contradictory rows
  are storable: a lane enabled yet carrying a suspension timestamp, a combo
  suppressed with no parole date, a kill pointer left on a cleared event.
- **Vocabulary is duplicated across layers.** The alert-outcome list is
  spelled in the migration, the dashboard backend and a frontend panel; the
  lane states in the backend and in `signalLaneStatus.js`. They agree today,
  and nothing kept them agreeing until now.

## 2. Declared in the registry (22)

| Machine | Held in | Was implicit because |
|---|---|---|
| `paper_trade` | `paper_trades.status` | no CHECK; literals at ~10 write sites; "closed" re-derived 6 ways |
| `exit_ladder` | `paper_trades` leg / floor columns | state is a combination of NULL / non-NULL columns |
| `live_trade` | `live_trades.status` | CHECK lists values, but ~30 unguarded write sites across 3 venues |
| `shadow_trade` | `shadow_trades.status` | no guards; `needs_manual_review` means something else than on the live ledger |
| `solana_execution` | `solana_executions.state` | already explicit — mirrored and pinned to the lane's own table |
| `evm_execution` | `EvmReceipt.state` | Enum without a transition table |
| `kill_switch` | `live_control` + `kill_events` | three-way combination of pointer, `cleared_at`, `killed_until`; "expired but not cleared" had no name |
| `signal_lane` | `signal_params.enabled` + `suspended_at` | derived; re-derived by dashboard and frontend |
| `combo_suppression` | `combo_performance` (30-day row) | combination of three columns; 7-day rows share them |
| `tg_alert_outcome` | `tg_alert_log.outcome` | `sent` means claimed or delivered depending on the lane |
| `operator_action` | `tg_alert_operator_actions.action` | four copies of the vocabulary |
| `outcome_label` | `signal_outcome_ledger.label_status` | seven copies of the vocabulary |
| `source_call_outcome` | `source_calls.outcome_status` | recomputed every run; no terminal state |
| `source_call_resolution` | `source_calls.resolved_state` | no CHECK; three vocabularies in one column |
| `curve_launch` | `curve_launch_discoveries.lifecycle_status` | two writers disagree; two states never written |
| `tg_resolution` | `tg_social_signals.resolution_state` | Enum in Python, raw string in SQL, insert-only rows |
| `tg_listener` | `tg_social_health.listener_state` | two conflicting type declarations; column shared by other components |
| `tg_channel` | `tg_social_channels.removed_at` | derived from one nullable column |
| `prediction_outcome` | `predictions.outcome_class` | NULL is a state; no CHECK |
| `chain_match_outcome` | `chain_matches.outcome_class` | NULL is a state; "expired" not representable |
| `narrative_resolution` | `narrative_alerts_inbound.resolution_status` | transitions run inside a startup migration |
| `ingest_source` | `scout.heartbeat` in-memory + `ingest_watchdog_state` | states unnamed; one table column with three meanings |

## 3. Found, catalogued, not yet declared (~47)

These are real but either live only in memory or in files, are small, or need
a design decision before they can be stated cleanly. Each line gives where
the state lives.

**Trading (`scout/trading/`)**
- One-shot exit latches: `moonshot_armed_at`, `conviction_locked_at`,
  `peak_fade_fired_at` on `paper_trades`; dry-run latches stored as
  `trade_decision_events` rows (`evaluator.py`).
- Revival cool-off and soak verdict, derived purely from
  `signal_params_audit` rows (`revival_criteria.py`).
- Combo alert markers: `perm_suppression_alerted_at`,
  `retest_incomplete_alerted_at`, `reversal_alert_pending_json`.
- Parole reservation token, in memory (`suppression.py`).
- Calibration epoch (`signal_params.last_calibration_at`).
- Cohort digest singleton (`cohort_digest_state`).
- Detection receipt archive: `detection_receipt_archive_manifest.status` +
  `offhost_confirmed`; `detection_receipt_cohorts.status` (no writer in code).
- In-memory: disk-pressure paging, suppression fail-open alert cooldown,
  params cache.

**Live execution (`scout/live/`)**
- Kraken pilot exit intent, derived from which step records exist in JSONL
  evidence files.
- Venue health and dormancy (`venue_health`, latest row per venue).
- `live_operator_overrides` (time-derived active / expired) and the approval
  request.
- `venue_listings` listed / delisted.
- `OrderConfirmation.status` (`filled` / `partial` / `rejected` / `timeout`):
  the vocabulary exists only as a code comment. It is the answer to one poll
  of the venue, not something that moves between states, so it is left out
  of the registry.
- Operating modes: `SOLANA_MODE`, `LIVE_EXECUTION_MANDATE_MODE`, `LIVE_MODE`
  and the flags that combine with them.
- Lane lock files (Kraken pilot, Solana lane).

**Signals, social, narrative, chains**
- Pons reorg fold per log (`curve_launch_events` markers).
- Pons collector loop: pass status vocabulary (declared only in a comment),
  backoff, pacer.
- Ledger forward-poll enrollment (`ledger_enrollments` + evictions).
- Snapshot batch visibility (`source_call_snapshot_batches`).
- Telegram channel silence-alert backoff (overloads
  `tg_social_health.detail`), DLQ `retried_at`, shadow generation arming.
- Narrative learn cadence clocks (`agent_strategy` keys), strategy key lock,
  `ProviderHealthState`.
- Chain instance lifecycle (`active_chains`; expiry is encoded as deletion),
  chain pattern lifecycle (`chain_patterns.is_active`, `alert_priority`).
- Entry-mcap finalisation (`entry_mcap_snapshots.captured_at`),
  contract-to-coin resolution (`contract_coin_map`, with sentinel rows).
- Instrumentation alert dedup, MiroFish daily budget, Telegram per-chat
  pacing, CoinGecko budget pace latch, rate limiter backoff.

**Core, scripts, dashboard**
- Process lifecycle and pipeline cycle phases in `scout/main.py` (log event
  names only).
- Feedback-scheduler day sentinels and the combo-refresh failure streak.
- Database connection lifecycle; migration applied / unapplied (five
  different "already applied" checks).
- `alert_events` legal sequences and its free-text `transition` column.
- Detection decision receipts hot / pruned / archived.
- Chain-identity semantics marker (`legacy_prefix` → `canonical_v1`).
- Watchdog state files under `scripts/`: cooldown, breach, heartbeat and
  acknowledgement files, one convention per script.
- Dashboard derivations computed per request: trade-inbox `window_state`,
  `verdict`, `entry_quality`, `group`; TG trade linkage (two vocabularies);
  TG shadow lane state; subsystem health.
- Frontend: request lifecycle controllers (each with its own shape), the app
  WebSocket, localStorage review / dismissed flags.

## 4. Defects noticed along the way

The registry change itself alters no behavior. Six of these were fixed
afterwards on the same branch, each in its own commit with a failing test
first; they are marked **FIXED** with the commit. The rest are
recorded so they can be triaged separately.

### 4a. Confirmed by reading the code in this session

1. **FIXED (`e7cecd0f`).** **Solana forward swap adopted after a confirmation timeout cannot reach
   `reconciled`.** This is the confirm-timeout branch of
   `_await_and_reconcile`: `_handle_ambiguity` returns on adoption without
   recording a state (`scout/live/solana_lane.py:4071-4077`); the caller only
   patches the in-memory confirmation (`:4355-4361`); a passing reconciliation
   then writes `reconciled` from `submission_attempted`, which the lane's own
   table forbids, so the guard raises. The reverse path records the catch-up
   state first and its comment explains why (`:6095-6108`). Reproduced by
   `test_confirm_timeout_then_finalized_adoption_reconciles` before the fix.
2. **FIXED (`1424ba68`, `b8d6ed86`).** **`KrakenPilot.cancel` writes `rejected` without reading the row's
   status** (`scout/live/kraken_pilot.py:5024-5118`).
3. **FIXED (`27c39f68`).** **`record_contract_coin_map` overwrites `coin_id` unconditionally on
   conflict** (`scout/db.py:1771-1774`) and the new-pools lane writes
   `coin_id=None` for every new pool (`scout/ingestion/gt_new_pools.py`), so a
   resolved mapping can be reset to NULL when the chain names match.
4. **FIXED (`524fbbf7`: now logged at ERROR; the close still happens).** **An unrecognised close reason silently becomes `closed_manual`**
   (`scout/trading/paper.py:748-759`), which the combo statistics exclude.
5. **FIXED (`524fbbf7`).** **`TradeStatus` omits three live statuses** (`scout/trading/models.py:16-27`).
6. **Ladder leg fills are not guarded on `status = 'open'`**
   (`scout/trading/paper.py:627-634`).
7. **Kill-switch `clear` is an unconditional UPDATE** on whichever event is
   active when it runs (`scout/live/kill_switch.py:306-320`).
8. **Combo gate: an unparsable `parole_at` admits the trade**
   (`scout/trading/suppression.py:108-112`), while `parole_at` NULL on a
   suppressed row denies it permanently (`:105-106`).
9. **The detection lane records its 24h dedup as `blocked_cooldown`**
   (`scout/trading/detection_alert.py:888-896`); other lanes use
   `blocked_dedup_24h`.
10. **The one-time announcement markers are written even when Telegram
    rejects the message** (`scout/main.py:2140-2150`; `raise_on_failure`
    defaults to False at `scout/alerter.py:149`).
11. **The paper-lane alert row is committed as `sent` before the send**
    (`scout/trading/tg_alert_dispatch.py:505-516`), and its later demotion is
    `WHERE id=?` with no `outcome='sent'` guard (`:65-68`).
12. **`ListenerState` is declared twice and the copies differ**
    (`scout/social/telegram/listener.py:80-82` has `crashed`;
    `scout/social/telegram/models.py:152` does not).
13. **`cmd_add` uses `INSERT OR REPLACE`** on `tg_social_channels`
    (`scout/social/telegram/cli.py:141-146`), resetting unspecified columns.
14. **Chain outcome hydration maps a `NEUTRAL` prediction to `miss`**
    (`scout/chains/tracker.py:761-763`), and the cooldown check counts expired
    chains as well as completed ones (`:476-489`).
15. **The Binance adapter returns final `partial` on `PARTIALLY_FILLED`**
    (`scout/live/binance_adapter.py:728-737`).
16. **Ingest starvation marks the source alerted before the page is sent**
    (`scout/heartbeat.py:199-212`).
17. **`SignalTrustTab.jsx:24` shows SUSPENDED for disabled lanes too.**
18. **`needs_manual_review` has two meanings on `live_trades`**: stop counting
    as exposure (`scout/live/engine.py:718-731`) versus blocking the lane
    (`scout/live/solana_lane.py:377`, `scout/live/kraken_pilot.py:287`).

Raised by the independent reviews of the registry (each cites the code; none
was reproduced by a test):

19. **FIXED (`bbd3af58`, `cbb215bf`).** **An expired kill that has not been cleared blocks the next kill.**
    `is_active()` stops reporting the kill once `killed_until` passes
    (`scout/live/kill_switch.py:197-209`), so orders are allowed again, but
    the pointer is still set and `trigger` is a compare-and-swap on the
    pointer being NULL. Until the automatic clear runs, a new loss-cap breach
    cannot engage the switch. The registry names this state
    `expired_uncleared` and the auditor counts it.
20. **The Telegram listener row stays `running` after a normal shutdown.**
    Shutdown cancels the task without writing, and `stopped` is written only
    when the connection ends on its own
    (`scout/social/telegram/listener.py:1544-1564`); nothing ages the row
    out, so `running` cannot be read as "alive now".
21. **Every start-up connect failure of the listener is recorded as
    `auth_lost`** (`scout/social/telegram/listener.py:1303-1345`), including
    network failures that have nothing to do with authorisation.
22. **Operator revive rewrites any suppressed base-combo row**
    (`scout/db.py::revive_signal_with_baseline`): it restarts parole from now
    for a combo that still had slots left and gives a parole date to a row
    that had none.
23. **`UNRESOLVED` can be written on a narrative prediction before 48 hours.**
    `eval_retry_count` is cumulative and is also incremented when the price is
    missing because of the call budget, a rate limit or a network error
    (`scout/narrative/evaluator.py:303-319`).
24. **A shadow trade lands in `needs_manual_review` after three evaluation
    failures over its whole life**, not three in a row: `review_retries` is
    never reset (`scout/live/shadow_evaluator.py::_bump_review`).
25. **The surface-alert lane's provisional `sent` row can become durable
    before the send finishes.** The row is inserted uncommitted and the
    Telegram call is awaited holding only `db._txn_lock`
    (`scout/trading/trade_surface_alerts.py:266-333`); other writers on the
    shared connection commit without that lock (for example
    `scout/social/telegram/listener.py:1100`), and a commit on a shared
    SQLite connection commits everything pending on it.

Raised by the independent review of the fixes (not fixed):

26. **A forward Solana run that stops at `landed` has no command to move it
    on.** A finalize timeout, or an adoption that is not yet finalized, ends
    the run at `landed` with a review verdict. Recovery then blocks the lane,
    and the forward `resolve` command targets `landed` from `landed`, which
    the transition table refuses (`scout/live/solana_lane.py:2431-2443`,
    `:4823-4825`). The reverse resolve can target `finalized`
    (`:6907-6911`); the forward one cannot, so the run needs a hand edit.
    True before the fixes as well.
27. **A kill released by a new trigger skips the "latched over an hour"
    alert** that `auto_clear_if_expired` would have sent for it
    (`scout/live/kill_switch.py`). The release is logged as
    `live_kill_dead_pointer_released` and the new kill's own alert follows.

### 4b. Reported by the survey, not independently confirmed here

Treat as leads. Each came with file and line from a read-only search agent.

- Legacy partial take-profit can leave an open trade with reduced quantity if
  the follow-up sell fails (`scout/trading/evaluator.py:1395-1407`).
- A revive's `tg_alert_eligible` audit row is read back as the regime cutover
  (`scout/trading/revival_criteria.py:332-338`), and revive does not clear
  `retest_incomplete_alerted_at`, which can silence a later stall page.
- Cohort digest stamps advance even when the Telegram send fails
  (`scout/trading/cohort_digest.py:433-443`).
- `reconcile_open_live_trades` selects every open row with no venue filter
  (`scout/live/reconciliation.py:354-358`); the `cross_venue_exposure` view
  labels every open row as Binance (`scout/db.py:5088-5094`).
- `auto_clear_if_expired` reads kill state outside the lock and then clears
  whatever is active (`scout/live/kill_switch.py:337-371`).
- Venue health probe, dormancy job and operator overrides have no production
  caller; if wired, the probe would overwrite the dormancy flag.
- Four scheduler day-sentinels start empty on every boot, so a restart inside
  the run hour re-fires them (`scout/main.py:163-169`, `:2806-2807`).
- Several watchdog cooldown files are never cleared on recovery
  (`scripts/alert_channel_watchdog.py`, `scripts/cg_ingestion_watchdog.py`,
  `scripts/dex_discovery_watchdog.py`,
  `scripts/audit_stop_loss_false_negatives.sh`).
- Detection-alert and counter follow-up tasks are not drained at shutdown
  (`scout/main.py:127`, `:138`, `:3229-3231`).
- Dashboard manual close answers `ok` even when the sell returned False
  (`dashboard/api.py:1007-1015`); `/health` derives `pipeline_running` from
  `candidates.first_seen_at`, which only moves earlier.
- The instrumentation watchdog's `_last_alert_reason` is never cleared on
  recovery (`scout/instrumentation/watchdog.py`).
- Frontend `reasonBadge` lacks labels for several exit reasons the backend
  writes (`dashboard/frontend/components/TradingTab.jsx:162-171`);
  `isHardBlocked` lacks `entry_basis_unavailable`
  (`dashboard/frontend/components/tradeDecisionBoard.js:36-44`).

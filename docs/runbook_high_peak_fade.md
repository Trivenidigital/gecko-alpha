# High-Peak Fade Exit Gate — Operator Runbook

## What this gate does

Fires when a trade has reached `peak_pct >= PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT`
(default 60%) AND price has retraced `>= PAPER_HIGH_PEAK_FADE_RETRACE_PCT`
(default 15%) from peak. Fires AFTER existing trailing_stop, BEFORE BL-062
peak_fade. Defers to BL-067 conviction-lock when armed (skipped on
`conviction_locked_at IS NOT NULL`).

Backtest evidence: `tasks/findings_high_peak_giveback.md` §14.

## Safety model (explicit)

**There is no automated circuit breaker in this MVP.** A regime shift that makes the gate net-negative will only be caught by:
1. Operator running the weekly `Per-signal effectiveness` query (below)
2. Master kill-switch flip (`PAPER_HIGH_PEAK_FADE_ENABLED=False`) — 10-second .env edit + restart

Auto-suspend circuit breaker is a deferred follow-up plan. Until then, monitoring discipline is operator-driven. Do not flip Phase 3 if you cannot commit to the weekly query review.

## Default state on deploy

- `PAPER_HIGH_PEAK_FADE_ENABLED=False` (master off)
- All `signal_params.high_peak_fade_enabled=0` (no signals opted in)

This means: zero behavior change on deploy. Safe.

## Activation sequence

## Prerequisites — verify BEFORE starting Phase 1 clock

Run this query to confirm signals are eligible to fire:

```sql
SELECT signal_type, enabled, suspended_at, suspended_reason
FROM signal_params
WHERE signal_type IN ('gainers_early', 'losers_contrarian');
```

**Decision rule:**
- If both signals show `enabled=1` → Phase 1 can start
- If `gainers_early.enabled=0` (suspended): Phase 1 dry-run will produce zero audit rows. **DO NOT start the 7-day Phase 1 clock until at least one `high_peak_fade_would_fire` event is logged.** The auto_suspend mechanism may revert the signal automatically; check `signal_params.suspended_at` over time.

The 7-day Phase 1 window measures forward dry-run telemetry, not calendar time. The clock starts on the first would-fire event, not on deploy.

### Phase 1 — dry-run telemetry (7 days)

1. **Opt in `gainers_early`:**
   ```sql
   UPDATE signal_params SET high_peak_fade_enabled = 1
   WHERE signal_type = 'gainers_early';
   ```

2. **Flip master ON in dry-run mode:** edit `/root/gecko-alpha/.env`:
   ```
   PAPER_HIGH_PEAK_FADE_ENABLED=True
   PAPER_HIGH_PEAK_FADE_DRY_RUN=True
   ```

3. **Restart pipeline:** `systemctl restart gecko-pipeline`

4. **Verify:** `journalctl -u gecko-pipeline -f | grep high_peak_fade_would_fire`

   Expect 0-2 events per day at observed fire rate (~1.5/week).

### Phase 2 — review dry-run (after Phase 1 clock)

**Required minimums BEFORE flipping Phase 3:**
- ≥ 4 `high_peak_fade_would_fire` events in audit table
- ≥ 2 of those trades subsequently CLOSED with identifiable exit reason
- Bootstrap re-run of `scripts/backtest_high_peak_existing_data_battery.py` shows p5 mean Δ still > $20

If ANY of these is unmet → extend dry-run; do NOT flip live.

```sql
SELECT
  trade_id, signal_type, peak_pct, peak_price, current_price,
  retrace_pct,
  ROUND((1 - current_price/peak_price)*100, 2) AS retrace_pp,
  fired_at
FROM high_peak_fade_audit
WHERE dry_run = 1
ORDER BY fired_at DESC;
```

Cross-reference against the actual closes for those `trade_id`s in
`paper_trades`:

```sql
SELECT pt.id, pt.exit_reason, pt.pnl_pct, pt.pnl_usd, hpf.fired_at AS would_fire_at, pt.closed_at
FROM paper_trades pt
JOIN high_peak_fade_audit hpf ON pt.id = hpf.trade_id
WHERE pt.status LIKE 'closed_%' AND hpf.dry_run = 1;
```

If gate would have fired EARLIER than actual exit and counter-factual PnL
is positive → flip Phase 3.

### BL-067 soak checkpoint (mandatory check on or after 2026-05-18)

The HPF gate defers to BL-067 conviction-lock (`conviction_locked_at IS NOT NULL` skips the gate). On 2026-05-18, BL-067's 14d soak ends. Re-run §14.4 cohort split:

```sql
SELECT
  CASE WHEN pt.opened_at < '2026-05-04' THEN 'pre-BL-067' ELSE 'post-BL-067' END AS regime,
  COUNT(*) AS n,
  SUM(pt.pnl_usd) AS total_pnl,
  AVG(pt.pnl_usd) AS mean_pnl
FROM paper_trades pt
JOIN high_peak_fade_audit hpf ON pt.id = hpf.trade_id
WHERE pt.status LIKE 'closed_%' AND hpf.dry_run = 0
GROUP BY regime;
```

If BL-067 is RETAINED post-soak: the deferral guard remains correct.
If BL-067 is REVERTED: the `conviction_locked_at IS NULL` guard becomes harmless (no locked trades exist) but verify the gate fires on the full cohort.

**`conviction_locked_at` does NOT auto-clear on BL-067 rollback.** Trades locked under BL-067 will continue to skip HPF even after BL-067 is reverted. To restore HPF coverage on previously-locked trades:

```sql
UPDATE paper_trades SET conviction_locked_at = NULL WHERE status = 'open';
```

### Phase 3 — flip live

```
PAPER_HIGH_PEAK_FADE_DRY_RUN=False
```

Restart pipeline. Watch `high_peak_fade_fired` events.

### Rollback (anytime)

```
PAPER_HIGH_PEAK_FADE_ENABLED=False
```

OR opt-out specific signal:

```sql
UPDATE signal_params SET high_peak_fade_enabled = 0 WHERE signal_type = 'gainers_early';
```

No code rollback required.

## Monitoring queries

**Fire rate by week:**

```sql
SELECT
  strftime('%Y-W%W', fired_at) AS week,
  dry_run,
  COUNT(*) AS n_fires
FROM high_peak_fade_audit
GROUP BY week, dry_run
ORDER BY week DESC;
```

**Per-signal effectiveness (live mode only):**

```sql
SELECT
  pt.signal_type,
  COUNT(*) AS n_fires,
  AVG(pt.pnl_pct) AS avg_pnl_pct,
  SUM(pt.pnl_usd) AS total_pnl_usd
FROM paper_trades pt
JOIN high_peak_fade_audit hpf ON pt.id = hpf.trade_id
WHERE hpf.dry_run = 0
  AND pt.status LIKE 'closed_%'
GROUP BY pt.signal_type;
```

**Conviction-lock defer audit (sanity check the guard works):**

```sql
-- Should always be 0: gate skips locked trades
SELECT COUNT(*)
FROM paper_trades pt
JOIN high_peak_fade_audit hpf ON pt.id = hpf.trade_id
WHERE pt.conviction_locked_at IS NOT NULL;
```

## What this gate does NOT do

- Does not affect trades opened before deploy (those stay on existing exits)
- Does not affect BL-067 conviction-locked trades (deferred by design)
- Does not modify entry pricing or sizing
- Does not handle live-mode (BL-055) execution risk separately — slippage
  modeling is paper-mode 50bps; live transition requires re-validation
  per `findings_high_peak_giveback.md` §14.6

## References

- Proposal: `tasks/findings_high_peak_giveback.md`
- Implementation plan: `tasks/plan_high_peak_fade.md`
- Evaluator gate: `scout/trading/evaluator.py` (search for `BL-NEW-HPF`)
- Config: `scout/config.py` (search for `PAPER_HIGH_PEAK_FADE_`)

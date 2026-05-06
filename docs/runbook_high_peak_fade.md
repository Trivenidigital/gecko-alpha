# High-Peak Fade Exit Gate — Operator Runbook

## What this gate does

Fires when a trade has reached `peak_pct >= PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT`
(default 75%) AND price has retraced `>= PAPER_HIGH_PEAK_FADE_RETRACE_PCT`
(default 15%) from peak. Fires AFTER existing trailing_stop, BEFORE BL-062
peak_fade. Defers to BL-067 conviction-lock when armed (skipped on
`conviction_locked_at IS NOT NULL`).

Backtest evidence: `tasks/findings_high_peak_giveback.md` §14.

## Default state on deploy

- `PAPER_HIGH_PEAK_FADE_ENABLED=False` (master off)
- All `signal_params.high_peak_fade_enabled=0` (no signals opted in)

This means: zero behavior change on deploy. Safe.

## Activation sequence

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

### Phase 2 — review dry-run (after 7 days)

```sql
SELECT
  trade_id, signal_type, peak_pct, peak_price, current_price,
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

# BL-062 Peak-Fade 30-Day Calibration Review

**Scheduled:** 2026-05-23 (approximately 30 days after the `bl062_peak_fade`
cutover recorded in `paper_migrations` at merge time).

**Spec reference:** `docs/superpowers/specs/2026-04-23-bl062-signal-stacking-peak-fade-design.md`

## Procedure

1. Load cutover_ts:

   ```sql
   SELECT cutover_ts FROM paper_migrations WHERE name = 'bl062_peak_fade';
   ```

2. Compute fire count, clip rate, and average delta on the forward cohort:

   ```sql
   WITH cutover AS (
       SELECT cutover_ts AS ts FROM paper_migrations WHERE name = 'bl062_peak_fade'
   ),
   fired AS (
       SELECT id, peak_pct, pnl_pct, checkpoint_48h_pct
       FROM paper_trades, cutover
       WHERE peak_fade_fired_at IS NOT NULL
         AND opened_at >= cutover.ts
   )
   SELECT
       COUNT(*) AS fires,
       SUM(CASE WHEN checkpoint_48h_pct IS NOT NULL
                 AND checkpoint_48h_pct > pnl_pct THEN 1 ELSE 0 END) AS clips,
       ROUND(AVG(pnl_pct - COALESCE(checkpoint_48h_pct, pnl_pct)), 4) AS avg_delta
   FROM fired;
   ```

   (Note: `checkpoint_48h_pct` is the best proxy for "would-have-been-expiry
   P&L" available without a counterfactual. If coverage is thin, fall back
   to a median-of-peers estimate or widen the window.)

3. Compute clip_pct = clips / fires.

## Stop Rule

| tier | trigger | action |
|---|---|---|
| early warning | `fires >= 10 AND clip_pct > 0.25` | set `PEAK_FADE_ENABLED=false` in VPS `.env` + restart gecko-pipeline.service + file investigation ticket |
| primary | `fires >= 20 AND clip_pct > 0.15` | same actions as early warning |

If neither tier triggers, leave the rule on and re-review in another 30 days
(or merge into the ongoing BL-061 ladder review cadence).

## Revert Procedure (if triggered)

```bash
ssh srilu-vps
sudo sed -i 's/^PEAK_FADE_ENABLED=.*/PEAK_FADE_ENABLED=false/' /root/gecko-alpha/.env
sudo systemctl restart gecko-pipeline.service
```

Then file a new ticket capturing: the forward cohort's fire count, the
clip_pct, and the top 5 clipped trades (by delta) for root-cause analysis.

## Cutover Recovery Note

If the `bl062_peak_fade` row in `paper_migrations` is ever manually corrupted
or deleted, **edit the cutover_ts in place** — do not delete and rely on the
migration to re-insert. `INSERT OR IGNORE` writes a *new later* timestamp on
startup, which shifts the A/B boundary forward and invalidates historical
comparisons.

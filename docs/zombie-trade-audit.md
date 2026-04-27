# Zombie open paper-trade audit

The 2026-04-27 fix in `scout/trading/evaluator.py` (PR #53) closes
`paper_trades.status='open'` rows whose token's `price_cache` row has
gone stale or missing AND whose `opened_at` is past
`PAPER_MAX_DURATION_HOURS`. Force-closes happen on the next eval pass
(every `TRADING_EVAL_INTERVAL`, default 30 min).

If the fix doesn't catch a zombie within an hour, it's a separate
upstream issue (e.g., the eval loop itself isn't running). Use the
queries below to confirm.

## How many open trades are past expiry?

```sql
-- On VPS:
sqlite3 /root/gecko-alpha/scout.db "
SELECT
  COUNT(*) AS overdue_open_trades,
  ROUND(AVG((julianday('now') - julianday(opened_at)) * 24), 1) AS avg_hours_open,
  MAX(ROUND((julianday('now') - julianday(opened_at)) * 24, 1)) AS max_hours_open
FROM paper_trades
WHERE status = 'open'
  AND (julianday('now') - julianday(opened_at)) * 24 > 168;  -- match PAPER_MAX_DURATION_HOURS
"
```

Healthy state: `overdue_open_trades=0`. Persistent nonzero = upstream
problem (eval loop dead, settings drift, etc.).

## Which signal types' tokens go stale most?

```sql
sqlite3 /root/gecko-alpha/scout.db "
SELECT
  pt.signal_type,
  COUNT(*) AS open_count,
  ROUND(AVG((julianday('now') - julianday(pt.opened_at)) * 24), 1) AS avg_hours_open
FROM paper_trades pt
LEFT JOIN price_cache pc ON pc.coin_id = pt.token_id
WHERE pt.status = 'open'
  AND (
    pc.coin_id IS NULL
    OR (julianday('now') - julianday(pc.updated_at)) * 86400 > 3600
  )
GROUP BY pt.signal_type
ORDER BY open_count DESC;
"
```

If one signal type dominates the stale list, that source's token-id
shape may be diverging from `price_cache` (chain-suffix mismatch,
casing, etc.).

## What did the zombie expirations look like?

```sql
sqlite3 -header -column /root/gecko-alpha/scout.db "
SELECT
  signal_type,
  COUNT(*) AS forced_closes,
  ROUND(AVG(pnl_pct), 2) AS avg_pnl
FROM paper_trades
WHERE exit_reason IN ('expired_stale_no_price', 'expired_stale_price')
  AND closed_at >= datetime('now', '-7 days')
GROUP BY signal_type;
"
```

Filter by `exit_reason` to keep these out of clean-expiry P&L analysis.

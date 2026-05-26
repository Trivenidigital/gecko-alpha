-- Request-independent soak metric for BL-NEW-TRACKER-COCKPIT-PROMOTION-PATH.
--
-- Counts unique tracker-promoted CoinGecko ids by UTC day from source rows,
-- not from repeated dashboard/API requests. This is the required gate before
-- any TG alert qualification design can claim enough tracker-promotion volume.
--
-- Usage:
--   sqlite3 scout.db < scripts/trade_inbox_tracker_promotion_soak.sql

WITH latest_open_paper AS (
    SELECT DISTINCT token_id
      FROM paper_trades
     WHERE status = 'open'
),
promoted_tracker AS (
    SELECT date(gc.appeared_on_gainers_at) AS utc_day,
           gc.coin_id
      FROM gainers_comparisons gc
      LEFT JOIN latest_open_paper op
        ON op.token_id = gc.coin_id
     WHERE gc.appeared_on_gainers_at >= datetime('now', '-36 hours')
       AND COALESCE(gc.coin_id, '') != ''
       AND (COALESCE(gc.symbol, '') != '' OR COALESCE(gc.name, '') != '')
       AND op.token_id IS NULL
     GROUP BY utc_day, gc.coin_id
)
SELECT utc_day,
       COUNT(*) AS unique_tracker_promoted_coin_ids
  FROM promoted_tracker
 GROUP BY utc_day
 ORDER BY utc_day DESC;


.headers off
.separator " | "
.print === Q2 first-seen distinct ids per month per surface
select 'predictions',m,count(*) from (select coin_id, substr(min(predicted_at),1,7) m from predictions group by 1) group by 2;
select 'paper_trades',m,count(*) from (select token_id, substr(min(opened_at),1,7) m from paper_trades where opened_at>'2020' group by 1) group by 2;
select 'velocity_alerts',m,count(*) from (select coin_id, substr(min(detected_at),1,7) m from velocity_alerts group by 1) group by 2;
select 'slow_burn',m,count(*) from (select coin_id, substr(min(detected_at),1,7) m from slow_burn_candidates group by 1) group by 2;
select 'gainer_accel',m,count(*) from (select coin_id, substr(min(detected_at),1,7) m from gainer_acceleration group by 1) group by 2;
select 'momentum_7d',m,count(*) from (select coin_id, substr(min(detected_at),1,7) m from momentum_7d group by 1) group by 2;
select 'volume_spikes',m,count(*) from (select coin_id, substr(min(detected_at),1,7) m from volume_spikes group by 1) group by 2;
select 'gainers_snapshots',m,count(*) from (select coin_id, substr(min(snapshot_at),1,7) m from gainers_snapshots group by 1) group by 2;
select 'trending_snapshots',m,count(*) from (select coin_id, substr(min(snapshot_at),1,7) m from trending_snapshots group by 1) group by 2;
select 'dex_pool_discoveries(base_token)',m,count(*) from (select network||base_token_address k, substr(min(first_seen_at),1,7) m from dex_pool_discoveries group by 1) group by 2;
select 'tg_social_signals(token)',m,count(*) from (select token_id, substr(min(created_at),1,7) m from tg_social_signals where resolution_state='RESOLVED' group by 1) group by 2;
select 'signal_first_seen',m,count(*) from (select token_id, substr(min(first_seen_at),1,7) m from signal_first_seen group by 1) group by 2;
select 'entry_mcap_snapshots',m,count(*) from (select contract_address, substr(min(first_seen_at),1,7) m from entry_mcap_snapshots group by 1) group by 2;
select 'conviction_watchlist',m,count(*) from (select coin_id, substr(min(snapshot_at),1,7) m from conviction_watchlist_snapshots group by 1) group by 2;
select 'narrative_inbound(x)',m,count(*) from (select coalesce(resolved_coin_id,extracted_ca,extracted_cashtag) k, substr(min(received_at),1,7) m from narrative_alerts_inbound group by 1) group by 2;
.print === signal_outcome_ledger surfaces by month (rows, distinct tokens)
select surface, substr(emitted_at,1,7) m, count(*), count(distinct token_id) from signal_outcome_ledger group by 1,2 order by 1,2;
.print === dex_pool_discoveries network x month (pools)
select network, substr(first_seen_at,1,7), count(*) from dex_pool_discoveries group by 1,2 order by 1,2;
.print === dex_pool_discoveries totals: rows, pools, tokens, liq>=250k, fdv, vol_h1, goplus
select count(*), count(distinct network||pool_address), count(distinct network||base_token_address), sum(liquidity_usd>=250000), sum(fdv_usd is not null), sum(volume_h1_usd is not null), sum(goplus_safe is not null) from dex_pool_discoveries;
select 'since 09-19', count(*), sum(liquidity_usd>=250000) from dex_pool_discoveries where first_seen_at>='2026-09-19';
.print === candidates chain
select chain, count(*) from candidates group by 1;

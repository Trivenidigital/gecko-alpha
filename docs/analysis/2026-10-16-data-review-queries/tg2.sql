.headers on
.separator " | "
select id, message_pk, token_id, symbol, contract_address, chain, mcap_at_sighting, resolution_state, source_channel_handle, created_at, paper_trade_id, substr(resolution_snapshot_json,1,400) from tg_social_signals where resolution_state='RESOLVED' order by id desc limit 4;
.print === resolved counts
select source_channel_handle, resolution_state, count(*), count(distinct token_id), sum(paper_trade_id is not null) from tg_social_signals group by 1,2 order by 1;
.print === token_id shapes
select case when token_id like '0x%' then 'evm_addr' when token_id like 'dex:%' then 'dex:' when length(token_id)>=32 and token_id not like '%-%' then 'base58?' else 'slug?' end k, count(*) from tg_social_signals where resolution_state='RESOLVED' group by 1;
.print === paper trades from tg
select signal_type, count(*), min(opened_at), max(opened_at), avg(peak_pct), avg(pnl_pct) from paper_trades where signal_type like '%tg%' or signal_type like '%social%' group by 1;
.print === price_cache freshness
select substr(updated_at,1,10) d, count(*) from price_cache where updated_at>='2026-09-15' group by 1;

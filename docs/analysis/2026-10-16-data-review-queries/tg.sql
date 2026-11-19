.headers on
.mode list
.separator " | "
.print === channels
select * from tg_social_channels;
.print === messages per channel
select channel_handle, count(*) n, min(posted_at) first, max(posted_at) last,
 sum(contracts is not null and contracts not in ('','[]')) with_ca,
 sum(cashtags is not null and cashtags not in ('','[]')) with_cashtag,
 sum((contracts is not null and contracts not in ('','[]')) or (cashtags is not null and cashtags not in ('','[]'))) with_either
from tg_social_messages group by 1 order by n desc;
.print === sample contracts/cashtags format
select contracts, cashtags from tg_social_messages where contracts not in ('','[]') limit 3;
.print === messages by month
select substr(posted_at,1,7) m, count(*) from tg_social_messages group by 1;
.print === tg_social_signals per channel
select source_channel_handle, count(*), count(distinct coalesce(contract_address,token_id)), min(created_at), max(created_at), group_concat(distinct resolution_state) from tg_social_signals group by 1 order by 2 desc;
.print === health
select * from tg_social_health;
.print === watermarks
select * from tg_social_watermarks;
.print === source_calls by source
select source_type, source_id, count(*) n, min(call_ts), max(call_ts), sum(price_at_call is not null) p0, sum(forward_1h_pct is not null) f1h, sum(forward_6h_pct is not null) f6h, sum(forward_24h_pct is not null) f24h, sum(max_favorable_pct_24h is not null) mfe from source_calls group by 1,2 order by n desc;
.print === source_calls outcome_status / resolved_state / call_kind
select outcome_status, count(*) from source_calls group by 1;
select resolved_state, count(*) from source_calls group by 1;
select call_kind, count(*) from source_calls group by 1;
select identity_kind, chain, count(*) from source_call_price_snapshots group by 1,2;
.print === snapshot sample
select * from source_call_price_snapshots order by id desc limit 3;
select * from source_call_price_snapshot_runs order by id desc limit 3;
.print === snapshots per identity
select count(*) identities, avg(n), max(n), min(first), max(last) from (select identity_key, count(*) n, min(snapshot_at) first, max(snapshot_at) last from source_call_price_snapshots group by 1);
select n_bucket, count(*) from (select identity_key, case when count(*)=1 then '1' when count(*)<=3 then '2-3' when count(*)<=10 then '4-10' else '>10' end n_bucket from source_call_price_snapshots group by 1) group by 1;
.print === span per identity (days) distribution
select case when d<1 then '<1d' when d<7 then '1-7d' when d<30 then '7-30d' else '>=30d' end b, count(*) from (select identity_key, julianday(max(snapshot_at))-julianday(min(snapshot_at)) d from source_call_price_snapshots group by 1) group by 1;
.print === runs stats
select substr(ran_at,1,7), count(*), sum(identities_seen), sum(snapshots_written), sum(provider_errors), sum(pools_unresolved), sum(empty_ohlcv) from source_call_price_snapshot_runs group by 1;
.print === source_calls cluster identity sample
select source_id, symbol, contract_address, chain, call_ts, cluster_identity, cluster_identity_kind, price_at_call, forward_24h_pct, max_favorable_pct_24h from source_calls where price_at_call is not null order by id desc limit 5;

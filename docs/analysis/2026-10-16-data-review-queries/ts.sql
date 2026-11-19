.headers off
.separator " | "
.print === Q4 volume_snapshots rows, ids, rows/id, min, max
select count(*), count(distinct contract_address), round(count(*)*1.0/count(distinct contract_address),1), min(scanned_at), max(scanned_at) from volume_snapshots;
select n_bucket, count(*) from (select contract_address, case when count(*)<2 then '1' when count(*)<10 then '2-9' when count(*)<100 then '10-99' else '100+' end n_bucket from volume_snapshots group by 1) group by 1;
.print === volume_snapshots key shapes
select case when contract_address like '0x%' then 'evm' when contract_address like '%-%' then 'slug' when length(contract_address)>=32 then 'b58' else 'other' end, count(distinct contract_address) from volume_snapshots group by 1;
.print === txns_h1_buys rows, ids, min, max, sources
select count(*), count(distinct contract_address), min(scanned_at), max(scanned_at), group_concat(distinct source) from txns_h1_buys_snapshots;
.print === score_history
select count(*), count(distinct contract_address), min(scanned_at), max(scanned_at) from score_history;
.print === distinct scan minutes per day in volume_snapshots
select substr(scanned_at,1,10), count(distinct substr(scanned_at,1,16)) from volume_snapshots where scanned_at>='2026-10-13' group by 1;
.print === signal_outcome_ledger label coverage
select label_status, count(*), sum(r24h is not null), sum(r7d is not null), sum(peak7d is not null) from signal_outcome_ledger group by 1;
select kind, count(*) from signal_outcome_ledger group by 1;
.print === price_cache dex rows by updated day count last 3 days
select count(*) from price_cache where coin_id like 'dex:%' and updated_at>='2026-10-13';

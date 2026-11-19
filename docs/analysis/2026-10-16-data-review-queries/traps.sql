.headers off
.separator " | "
.print === T1 slug in contract_address (slug-like, total)
select 'candidates', sum(contract_address not like '0x%' and contract_address like '%-%'), count(*) from candidates;
select 'entry_mcap_snapshots', sum(contract_address not like '0x%' and contract_address like '%-%'), count(*) from entry_mcap_snapshots;
select 'alerts', sum(contract_address not like '0x%' and contract_address like '%-%'), count(*) from alerts;
select 'score_history distinct', count(distinct case when contract_address not like '0x%' and contract_address like '%-%' then contract_address end), count(distinct contract_address) from score_history;
select 'contract_coin_map', sum(contract_address not like '0x%' and contract_address like '%-%'), count(*) from contract_coin_map;
.print === T2 timestamp formats
select 'predictions.predicted_at sep,len>19', substr(predicted_at,11,1), length(predicted_at)>19, count(*) from predictions group by 2,3;
select 'predictions.created_at sep,len', substr(created_at,11,1), length(created_at), count(*) from predictions group by 2,3;
select 'paper_trades.opened_at sep,+00:00', substr(opened_at,11,1), opened_at like '%+00:00', count(*) from paper_trades group by 2,3;
select 'paper_trades opened_at 1970 sentinel', count(*) from paper_trades where opened_at<'2020';
select 'source_calls.call_ts Z,+00:00', call_ts like '%Z', call_ts like '%+00:00', count(*) from source_calls group by 2,3;
select 'signal_params.updated_at Z', updated_at like '%Z', count(*) from signal_params group by 2;
select 'dex_pool pool_created_at Z / first_seen +00:00 / total', sum(pool_created_at like '%Z'), sum(first_seen_at like '%+00:00'), count(*) from dex_pool_discoveries;
select 'tg_social_channels.removed_at space-format', removed_at like '% %', count(*) from tg_social_channels where removed_at is not null group by 2;
.print === T4 EVM case mix
select 'dex_pool mixed-case evm base tokens', sum(base_token_address like '0x%' and base_token_address<>lower(base_token_address)), sum(base_token_address like '0x%') from dex_pool_discoveries;
select 'contract_coin_map mixed-case evm', sum(contract_address like '0x%' and contract_address<>lower(contract_address)), sum(contract_address like '0x%') from contract_coin_map;
select 'tg_social_signals mixed-case evm', sum(contract_address like '0x%' and contract_address<>lower(contract_address)), sum(contract_address like '0x%') from tg_social_signals;
select 'contract_coin_map same addr+chain diff case', count(*) from (select chain, lower(contract_address) a from contract_coin_map where contract_address like '0x%' group by 1,2 having count(distinct contract_address)>1);
select 'price_cache dex evm keys dup by case', count(*) from (select lower(coin_id) from price_cache where coin_id like 'dex:%:0x%' group by 1 having count(*)>1);
.print === T5 duplicate identity
select 'coin_ids with addresses on >1 chain', count(*) from (select coin_id from contract_coin_map where coin_id is not null and coin_id<>'' group by 1 having count(distinct chain)>1);
select 'chain+address mapped to >1 coin_id', count(*) from (select chain, lower(contract_address) from contract_coin_map where coin_id is not null and coin_id<>'' group by 1,2 having count(distinct coin_id)>1);
select 'contract_coin_map coin_id null', sum(coin_id is null or coin_id=''), count(*) from contract_coin_map;
select 'ccm source,address_type', source, address_type, count(*) from contract_coin_map group by 2,3 order by 4 desc limit 8;
select 'predictions symbols mapping to >1 coin_id', count(*) from (select upper(symbol) from predictions group by 1 having count(distinct coin_id)>1);
.print === T7 tg duplicates / backfill
select 'tg_social_signals rows, distinct msg+token', count(*), count(distinct message_pk||'|'||token_id) from tg_social_signals;
select 'tg msgs posted >7d before parsed', count(*) from tg_social_messages where julianday(parsed_at)-julianday(posted_at)>7;
select 'source_calls ingest_delay>1d, max days', count(*), max(ingest_delay_sec)/86400.0 from source_calls where ingest_delay_sec>86400;
.print === T8 resolved_state vocabulary
select resolved_state, source_type, count(*) from source_calls group by 1,2;
.print === T9 null prices
select 'predictions bad price', sum(price_at_prediction is null or price_at_prediction<=0), count(*) from predictions;
select 'paper_trades bad entry', sum(entry_price is null or entry_price<=0), count(*) from paper_trades;
.print === T10 control/holdout predictions
select is_control, is_holdout, count(*) from predictions group by 1,2;

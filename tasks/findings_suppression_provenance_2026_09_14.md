# DASH11 retained producer, coverage and price-provenance findings

Observed 2026-09-14T13:03:47.090417Z. Findings only; no cost/ranking calculation, application changes, DB writes, service/config/account calls, paid API requests or sends. Production HEAD was5c43526c46a33b06e7ed18509c8a0ea43c7772d3;777 deployment remained a separate root-owned step. The read transaction closed after5.038961s. All queries succeeded;59 aggregate rows emitted, longest query1.495461s, within45s total/10s query/1000 output rows/512MiB limits.

Evidence files:

- [Pre-query assumptions](https://github.com/Trivenidigital/gecko-alpha/blob/docs/closeout-evidence-20260914/tasks/evidence/closeout_2026_09_14/dash11-retained-provenance-assumptions-20260914.md)
- [Probe](https://github.com/Trivenidigital/gecko-alpha/blob/docs/closeout-evidence-20260914/tasks/evidence/closeout_2026_09_14/dash11-retained-provenance-audit-20260914.py)
- [Fresh aggregate evidence](https://github.com/Trivenidigital/gecko-alpha/blob/docs/closeout-evidence-20260914/tasks/evidence/closeout_2026_09_14/runtime-dash11-retained-provenance-audit-20260914.txt)
- [Exact deployed-source excerpts with file lines and hashes](https://github.com/Trivenidigital/gecko-alpha/blob/docs/closeout-evidence-20260914/tasks/evidence/closeout_2026_09_14/dash11-retained-provenance-source-evidence-20260914.json)

## Source identity and limits

All six observed source hashes match immutable5c Git blobs. signals.py, outcome_ledger.py and spikes/detector.py also match829d12b1. config.py, db.py and main.py differ from829d12b1, so their findings below are based on the actual5c blobs rather than presumed whole-master deployment. This is consistent with selective release, not unexpected drift.

Windows execution only transmitted probe text to ssh stdin; stdout/stderr went to a new local file, read in a separate call. The probe imported no application modules, opened the named live SQLite file mode=ro/query_only, used memory-only temporary storage, denied writes/ATTACH/DETACH, and closed its pinned snapshot. No token-level rows or price values were emitted.

The Linux aggregate queries use SQLite julianday normalized half-open bounds at its date-function precision, with the same explicit UTC window for both populations. They are aggregate comparability evidence, not event-by-event identity reconciliation or proof of every producer attempt. Table-wide SQLite timestamp checks found zero invalid/future timestamps for ledger and decisions; this does not independently validate every possible Python timestamp parser edge case. Active flags, configured retention, expected per-signal arrival rates, journal heartbeats and deletion receipts were not queried.

## Retained activity and retention

| Table | Retained rows | Oldest retained timestamp UTC | Newest retained timestamp UTC |
|---|---:|---|---|
| signal_outcome_ledger |575,101|2026-07-03 03:19:01|2026-09-14 13:03:05|
| trade_decision_events |1,092,478|2026-07-31 12:46:48|2026-09-14 13:00:57|
| volume_history_cg |1,246,590|2026-09-04 12:53:16|2026-09-14 13:00:54|
| price_cache |57,101|2026-04-15 10:31:25|2026-09-14 13:03:07|

The ledger received a retained emission about41s before observation; decisions about170s. The newest terminal-label timestamp was12:45:55Z (~18minutes before observation). These are evidence of recent recorded activity, not a configured freshness SLO or proof every path is healthy. Retained pending/partial rows total27,239;65 were older than7d, with the oldest at2026-09-07 12:46:25Z, only~17minutes beyond maturity. This alone does not establish a stalled labeler.

Deployed config source defaults decision retention to45d and price-history retention to10d; actual spans are broadly consistent. The deployed maintenance code invokes both pruners independently and invokes label_pending. Runtime setting overrides and actual last prune executions are unknown because config/journal inspection was outside this audit. No explicit ledger-prune implementation or SIGNAL_OUTCOME_LEDGER_RETENTION setting was found in the searched source/scripts; that negative source search is not a guarantee of indefinite retention or absence of external deletion.

price_cache is an overwritten current-observation store; its oldest updated_at is a stale entry, not a historical retention window. Its56,897 positive-price rows and recent global maximum cannot prove coverage/freshness for the1530-token suppression cohort. volume_history_cg currently retains roughly10d, materially shorter than the ledger's observed history; retrospective reconstruction beyond retained price observations is incomplete.

## Seven-day population comparability

Window: [2026-09-07T13:03:47.090417Z,2026-09-14T13:03:47.090417Z).
Ledger filter: kind=gated_out_sample plus JSON reason=suppressed and source_layer=dispatcher. Decisions: reason=suppressed, all decision values, same window.

| Signal | Ledger rows | Ledger distinct tokens | Matching-scope decision rows | Positive emission anchors | Latest ledger event UTC |
|---|---:|---:|---:|---:|---|
| losers_contrarian |13,793|146|13,793|13,793|2026-09-14 13:00:57|
| chain_completed |4,485|779|0|0|2026-09-14 13:03:05|
| first_signal |312|20|0|0|2026-09-14 02:57:20|

The only recorded decision population was losers_contrarian / scout.trading.signals / blocked, also146 distinct tokens. Equal aggregate row/token counts do not prove matching identities or complete capture. Neither token sets nor receipt pairs were joined in this audit.

The source path explains the ledger-only branches: losers_contrarian calls _emit_dispatch_decision then _record_suppressed_ledger_emission before continuing (signals.py:714-735). first_signal and chain_completed call only the ledger recorder with price=None, then continue before the later cache lookup/open-trade path (signals.py:850-872 and1408-1431). Thus a global ledger/decision ratio would conflate different instrumentation contracts. The ledger-only result is not evidence of missing trade_decision_events writes from those branches: the inspected branches do not request them.

first_signal's latest recorded suppression is~10.1hours old. Demand/cadence and active flags are unverified, so do not attribute this gap to a disabled/broken producer. The recorder also depends on LEDGER_SAMPLE_SUPPRESSED and the global ledger enable check and is fail-soft (signals.py:60-93); seeing rows does not prove all attempts persisted.

## Fourteen-day labels are not seven-day returns

Window: [2026-08-31T13:03:47.090417Z,2026-09-14T13:03:47.090417Z).

| Label status | Rows | Recorded r24h | Recorded r7d | Positive emission anchors |
|---|---:|---:|---:|---:|
| complete |9,832|9,170|5,346|9,467|
| partial |184|184|0|184|
| pending |18,452|0|0|13,643|
| unlabelable |3,843|0|0|0|
| Total |32,311|9,354|5,346|23,294|

There are1530 distinct tokens under the window-relative earliest-anchor rule; only51 earliest anchors have r7d and185 have a positive emission anchor. Status-group distinct-token counts must not be summed because a token can occur in multiple groups.

Of9,832 complete rows,4,486 have no r7d. This follows the actual finalizer: after7d, any available horizon OR peak7d can mark complete; peak7d may be recorded without any emission anchor (outcome_ledger.py:683-694,795-808). Missing anchor rows therefore can have complete labels but cannot have computed forward returns. A complete-count maturity claim would be false. The51 earliest anchors with recorded r7d are neither proof of adequate statistical evidence nor permission to compute/rank costs; this audit performed neither.

## Price provenance remains unverified

Actual ledger schema stores price_at_emission, anchor_cache_age_seconds, return columns, peak7d, and label/labeled timestamps. It does NOT store an emission observation ID/source timestamp or a selected horizon observation ID/source/timestamp. liquidity_source is liquidity provenance, not a price-source field. price_cache and volume_history_cg likewise lack a per-row upstream provider receipt/source discriminator.

The source describes a price-selection preference, not durable per-label provenance: _price_at_or_after first takes the earliest positive volume_history_cg price at/after the requested horizon, otherwise the current price_cache observation if within the configured lateness bound; it returns only a float (outcome_ledger.py:639-678). label_pending persists the resulting numeric return and final state, not which observation supplied it (747-818). Historical lookup has no explicit maximum horizon lateness, while cache fallback does. No actual horizon lateness distribution was queried here.

The observed14d suppression cohort has23,294 positive anchors, every one carrying zero anchor-cache age;9,017 rows have unknown/null age. That zero is not measured freshness for these callers: record_emission defaults non-null supplied prices to0.0 when no explicit age is passed (outcome_ledger.py:511-518), and the suppression helper supplies price without an age. losers_contrarian supplies its snapshot price; chain/first supplyNone. A zero age cannot establish source freshness.

record_volume writes positive-volume CoinGecko-shaped input prices with the ingestion timestamp (spikes/detector.py:18-48); price_cache can be refreshed by multiple lanes including enrollment polling. Source routing does not establish which surviving or pruned observation historically supplied each stored return. Old cache versions are overwritten, price history is pruned, and per-label source selection is absent. Therefore historical per-label price provenance stays UNKNOWN/UNVERIFIED even where recorded r7d is present.

## Next gate

Keep the released surface informational: retained counts, unknown coverage and unverified provenance. These findings introduce no new blocker to that already-reviewed scope.

HOLD future cost/ranking conclusions until an independently reviewed evidence contract establishes comparable producer denominators/attempt-to-receipt coverage, complete event identity reconciliation for any compared cohort, and price-observation lineage/freshness/lateness for selected emission and horizon prices. Evaluate earliest-window anchors and missing-price bias explicitly; terminal label counts are not a substitute. If existing stored data cannot prove historical lineage, label that dimension unverifiable and scope any new forward evidence separately rather than manufacturing a retrospective answer. No implementation, new table, policy change, soak duration or deployment is proposed or authorized by this findings-only audit.

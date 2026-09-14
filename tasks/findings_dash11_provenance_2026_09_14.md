# DASH-11 retained-price provenance audit — 2026-09-14

Closeout reconciliation: tasks/current_closeout_queue_2026_09_14.md records the later 13:03 UTC audit and verified 77751890 dashboard deployment. The observations below remain the separate historical 00:26 UTC, 120-day audit; they are not current runtime or deployment verification.

## Current closeout — 2026-09-14

PR #585 merged the bounded health-only API/Pipeline panel as `829d12b1` at 12:32:18Z. The recommendation below is historical and must not trigger a duplicate UI build. Next gate: coordinated deployment with fresh runtime and smoke verification; this documentation closeout makes no deployment claim. The 120-day audit cohort below is distinct from #585's 14-day recorded-label availability window. Its counts are not current panel counts or economic validation. Producer coverage and price-provenance limitations remain unresolved.

## Historical result and next engineering gate (00:26 UTC audit)

DASH-11 health-only dashboard integration remains actionable. Keep monetary/ranking interpretations unverified: 189 of 214 earliest-token matured anchors have seven-day horizons older than the earliest surviving historical price observation. Only 25 have a numerically matching current historical candidate. This is a present-day replayability limitation, **not evidence that 189 original labels were wrong**.

Ship a subsequent bounded health view with producer populations, unknown coverage, maturation grain/window, freshness and explicit EXPERIMENTAL / not-for-pruning context. Do not require an operator decision to build that already-authorized read-only view. Do not interpret the existing n>=10 gate as a provenance or profitability gate. This findings PR resolves the previously uncompleted retention/provenance inspection from tasks/findings_autonomous_product_readiness_2026_09_13.md:151; it does not implement the UI or close DASH-11.

## State checked and selection

- Successfully fetched origin/master e6a55d7a and created isolated branch docs/dash11-provenance-audit-20260914. AGENTS.md absent; prompt-provided instructions applied. Read automation memory, lessons, todo and top Current Final Backlog Snapshot plus authoritative reconciliation.
- Original cockpit parent shipped; signal trust/cockpit successors DASH-08/DASH-11. Local dated execution tracker downgrades DASH-08 after quarantine. Negative historical pool probe stays closed; no paid calls or activation.
- Open PRs #577 (postmortem history) and #578 (Telegram outcome counts) have separate work; no duplication or shared PR mutation. Recent task inventory is not proof all automation runs are idle.
- Production SSH 00:22:50Z: HEAD d2f0d61edc63cb55ae159ec952cce404991f21f5. At 00:23:20Z actual gecko-pipeline, gecko-dashboard and hermes-gateway units active/running. Initial guessed gecko-alpha unit was inactive; actual-unit inventory corrected that lookup. No service failure inference from a guessed unit name.
- Installed analyzer ran without --send at 00:23:20.620802Z: population mismatch warnings remained, n=214. That run was separate from the consistent snapshot below; no economic conclusion is drawn from its dollar output.

## Pinned runtime evidence

One SQLite mode=ro/query_only BEGIN transaction, as-of **2026-09-14T00:26:45.560579+00:00**, completed in 6.07 seconds. Explicit 7d health and 120d maturation windows, one UTC now. Outer timeout 45s; SQLite progress deadline 30s. Historical lookup EXPLAIN used idx_vol_hist_cg on coin_id/recorded_at. EXPLAIN was emitted before the loop, but inspected by the operator agent after the bounded command completed; it was not an interactive pre-execution gate. No timeout occurred.

| Measure | Observed |
|---|---:|
| 120d dispatcher-suppression rows | 515,692 |
| 120d distinct earliest-token anchors | 5,079 |
| Earliest anchors with non-null r7d | 214 |
| Tokens with equal earliest timestamps | 0 |
| Malformed verdict objects among queried gated-out rows | 0 |
| Matured anchor emissions | July 10 04:06:14.670663Z to September 6 13:06:34.918613Z |
| Surviving history minimum | September 3 23:48:08.836629Z |
| Surviving history maximum | September 14 00:22:36.003617Z |
| Matured horizons before global history minimum | 189 |
| First surviving historical candidate <=2h after horizon | 25 |
| First surviving historical candidate >2h after horizon | 163 |
| No surviving historical candidate for token/horizon | 26 |
| Candidate return numerically matches stored r7d | 25 |
| Candidate return numerically differs | 163 |
| Invalid/missing/nonfinite anchor or return | 0 |
| Candidate lag range | 0.0143 to 1,375.5082 hours |

The 2h split is a diagnostic benchmark corresponding to the source cache-lateness default, not a verified runtime configuration or an approved historical-lateness rule. Returns were compared using stored price_at_emission, never a reconstructed entry cache price, with math.isclose(relative=1e-9, absolute=1e-9). Missing or different current candidates cannot recover the original labeling observation; matching candidates establish numerical consistency only. No present cache lookup was used to certify original source or timestamp.

| 7d producer | Ledger suppression rows | Suppressed decision rows |
|---|---:|---:|
| losers_contrarian | 13,563 | 13,563 |
| chain_completed | 4,472 | 0 |
| first_signal | 319 | 0 |
| Total | 18,354 | 13,563 |

Even equal counts do not prove event identity or complete coverage. Existing population-mismatch warnings are correct; do not clamp aggregate ratio to 100% or hide the ledger-only lanes.

Latest suppression emission: 00:22:40.510548Z. Latest non-null labeled_at in the 120d suppression cohort: September 13 23:41:51.188236Z. Observed arrivals: **2,187/24h**, **141/1h**. These show current arrivals, not a guaranteed forward rate or freshness SLO validation. No forward soak is prescribed to reconstruct already-missing original observations.

## Source path and evidence limits

Source citations refer to inspected master e6a55d7a:

- scripts/suppression_cost_rollup.py:137 reads decision populations; :146 reads ledger; :166 filters dispatcher suppression. :193 selects earliest per token before :199 checks r7d. Equal timestamps preserve first encountered row; no stable identity tie-break is guaranteed, but this cohort had zero ties. :276 cost gate uses n alone.
- scout/outcome_ledger.py:747 selects pending/partial rows. :783 calls _price_at_or_after and :787 computes return from stored entry price. :809 finalizes rows; labeled_at is row finalization, not per-horizon observed_at.
- scout/outcome_ledger.py:655 selects first positive volume_history_cg observation at/after deadline with no upper lateness bound. :666 cache fallback has a lateness check. This reachable code path establishes a possible late-history selection; it does not establish which source produced these historical labels.
- Production PRAGMA table_info returned the ledger's 19 fields, including price_at_emission, anchor_cache_age_seconds, r7d and labeled_at, but no per-horizon price source/observed timestamp. Creation is scout/db.py:10001. No claim is made that the whole system lacks every possible external audit record.
- scout/db.py:12767 exposes history pruning. Surviving bounds do not prove the current configured keep_days or that this method caused the missing history. Effective retention config and prune job execution were not verified and are not required for the narrower surviving-data finding.

Four function-segment SHA256 values matched current checkout and production after explicit UTF-8 decoding (initial Windows default-decoding mismatch was corrected):

| Function | SHA256 |
|---|---|
| _price_at_or_after | ec1f9645a8deacd6d03d1e023c6603f2099f0d8c1a9cd3b3e5ebdc544e93964d |
| label_pending | e26e34f17d342eb1737127769f78627afb7fb59d77f801568eaa358c2e495ae8 |
| analyze | 1e0f8cc8463c4f786d7f322e3518cece377fe5ce431b4b844cdc9910b5d8348d |
| prune_volume_history_cg | c5dd5892722f61798015f9896f75079252b2ca2819397cb7bca8ffe079391798 |

This verifies those source segments on disk, not all production files or the loaded process revision. No process restart or deploy occurred.

## Review, verification and next gate

Plan and design each received two parallel reviews: dash_drift (structure/cohort evidence) and audit_safety (runtime safety/attribution). Folds adopted: canonical cohort, ties, pinned transaction, deadlines, indexed lookup, explicit invalid-anchor counts, UTF-8 source equivalence, and no corruption inference from present-day non-replayability. PR #579 reviews completed at ad4453d3: dash_drift approved logic/concurrency and audit_safety approved ops-safety/silent-failure. Both verified the 19-field schema correction; analyzer maturity citation also corrected. No unresolved review findings. Clearance metadata records those actual terminal verdicts.

Existing analyzer tests: `uv run --no-sync pytest -q tests/test_suppression_cost_rollup_script.py --tb=short` — **15 passed**. These verify unchanged analyzer behavior, not historical label correctness. No new runtime tests are appropriate for documentation-only changes. Diff and citation checks precede publication.

Next engineering gate: use these data limits in the health-only API/UI plan; retain unknown provenance and population mismatch as explicit states. Monetary interpretations require evidence of horizon timing/source and cohort validity beyond n; historical recovery is unproven and new provenance storage would need its own additive-writer/watchdog design. No dispatch, pruning, trading, paid vendor, configuration, schema or production mutation was performed. Revert this docs PR for rollback.

## Reproduction

The following scratch program was passed on stdin to production Python under `timeout 45` from /root/gecko-alpha. It reads only, outputs aggregate evidence, and creates no remote files. Set explicit UTF-8 decoding for source hashing on Windows. Results naturally change with as-of time. On timeout, report incomplete evidence, never zero rows. For reuse, inspect the printed query plan in a separate preflight before executing the cohort loop.

```python
import sqlite3,json,time,math,hashlib,ast
from datetime import datetime,timedelta,timezone
from collections import Counter
from pathlib import Path
now=datetime.now(timezone.utc); start=time.monotonic()
def out(k,v): print(json.dumps({k:v}),flush=True)
for path,names in [('scout/outcome_ledger.py',['_price_at_or_after','label_pending']),('scripts/suppression_cost_rollup.py',['analyze']),('scout/db.py',['prune_volume_history_cg'])]:
    src=Path(path).read_text(); tree=ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names:
            out('source_hash',{'function':n.name,'sha256':hashlib.sha256(ast.get_source_segment(src,n).encode()).hexdigest()})
c=sqlite3.connect('file:/root/gecko-alpha/scout.db?mode=ro',uri=True,timeout=5); c.row_factory=sqlite3.Row
try:
 c.execute('PRAGMA query_only=ON'); c.set_progress_handler(lambda: int(time.monotonic()-start>30),10000); c.execute('BEGIN')
 out('as_of',now.isoformat())
 out('schema',[r['name'] for r in c.execute('PRAGMA table_info(signal_outcome_ledger)')])
 out('history_plan',[tuple(r) for r in c.execute('EXPLAIN QUERY PLAN SELECT price,recorded_at FROM volume_history_cg WHERE coin_id=? AND recorded_at>=? AND price IS NOT NULL AND price>0 ORDER BY recorded_at ASC LIMIT 1',('probe',now.isoformat()))])
 h=c.execute('SELECT MIN(recorded_at),MAX(recorded_at) FROM volume_history_cg').fetchone();out('history_bounds',list(h))
 rows=c.execute("SELECT token_id,surface,gate_verdicts,emitted_at,price_at_emission,r7d,labeled_at,label_status FROM signal_outcome_ledger WHERE kind='gated_out_sample' AND emitted_at>=? ORDER BY emitted_at DESC",((now-timedelta(days=120)).isoformat(),)).fetchall()
 supp=[]; malformed=0
 for r in rows:
  try: v=json.loads(r['gate_verdicts'])
  except (TypeError,ValueError): malformed+=1;continue
  if not isinstance(v,dict):malformed+=1;continue
  if v.get('reason')=='suppressed' and v.get('source_layer')=='dispatcher':supp.append(r)
 earliest={}
 for r in supp:
  if r['token_id'] not in earliest or r['emitted_at']<earliest[r['token_id']]['emitted_at']:earliest[r['token_id']]=r
 ties=Counter(r['token_id'] for r in supp if r['emitted_at']==earliest[r['token_id']]['emitted_at'])
 matured=[r for r in earliest.values() if r['r7d'] is not None]
 out('cohort',{'rows':len(supp),'tokens':len(earliest),'matured':len(matured),'equal_earliest_tokens':sum(v>1 for v in ties.values()),'malformed':malformed,'emitted_min':min(r['emitted_at'] for r in matured),'emitted_max':max(r['emitted_at'] for r in matured)})
 out('population_7d',dict(Counter(r['surface'] for r in supp if r['emitted_at']>=(now-timedelta(days=7)).isoformat())))
 out('decisions_7d',{r[0]:r[1] for r in c.execute("SELECT signal_type,COUNT(*) FROM trade_decision_events WHERE reason='suppressed' AND created_at>=? GROUP BY signal_type",((now-timedelta(days=7)).isoformat(),))})
 out('freshness',{'last_emitted':max(r['emitted_at'] for r in supp),'last_labeled':max(r['labeled_at'] or '' for r in supp),'rows_24h':sum(r['emitted_at']>=(now-timedelta(days=1)).isoformat() for r in supp),'rows_1h':sum(r['emitted_at']>=(now-timedelta(hours=1)).isoformat() for r in supp)})
 counts=Counter(); lags=[]
 for r in matured:
  deadline=datetime.fromisoformat(r['emitted_at'])+timedelta(days=7)
  if h[0] and deadline<datetime.fromisoformat(h[0]):counts['horizon_before_retained_min']+=1
  base=r['price_at_emission']
  if base is None or not math.isfinite(base) or base<=0 or not math.isfinite(r['r7d']):counts['invalid_anchor_or_return']+=1;continue
  p=c.execute('SELECT price,recorded_at FROM volume_history_cg WHERE coin_id=? AND recorded_at>=? AND price IS NOT NULL AND price>0 ORDER BY recorded_at ASC LIMIT 1',(r['token_id'],deadline.isoformat())).fetchone()
  if p is None:counts['no_surviving_historical_candidate']+=1;continue
  lag=(datetime.fromisoformat(p[1])-deadline).total_seconds()/3600;lags.append(lag)
  counts['candidate_within_2h' if lag<=2 else 'candidate_later_than_2h']+=1
  counts['numerically_matches' if math.isclose(p[0]/base-1,r['r7d'],rel_tol=1e-9,abs_tol=1e-9) else 'numerically_differs']+=1
 out('replay_diagnostics',dict(counts));out('candidate_lag_hours',{'min':min(lags) if lags else None,'max':max(lags) if lags else None});out('elapsed_seconds',time.monotonic()-start)
finally:c.close()
```

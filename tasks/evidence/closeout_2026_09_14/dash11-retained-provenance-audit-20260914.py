"""One bounded aggregate-only readonly audit. No imports of application code."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import resource
import signal
import sqlite3
import subprocess
import time
resource.setrlimit(resource.RLIMIT_AS, (512*1024**2,512*1024**2))
started=time.monotonic()
def abort(*_): raise TimeoutError('total_45s_limit')
signal.signal(signal.SIGALRM,abort); signal.alarm(45)
now=dt.datetime.now(dt.timezone.utc)
params={'now':now.isoformat(),'w7':(now-dt.timedelta(days=7)).isoformat(),'w14':(now-dt.timedelta(days=14)).isoformat()}
out={'as_of':params['now'],'window_start':params['w7'],'lookback_start':params['w14'],'limits':{'total_seconds':45,'query_seconds':10,'aggregate_rows_total':1000,'address_space_mib':512},'assumptions':['Retained rows do not prove complete producer capture','Compare exact UTC half-open seven-day populations with matching suppression scope','Fourteen-day label status is separate from r7d availability','Stored price lineage must identify the actual selected observation; source code alone cannot reconstruct it','Observed retention span is not active retention configuration'],'observations':{},'errors':{}}
conn=None; used=0
try:
 root=Path('/root/gecko-alpha')
 out['runtime_head']=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],timeout=3,text=True).strip()
 out['source_sha256']={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in ('scout/trading/signals.py','scout/outcome_ledger.py','scout/db.py','scout/main.py','scout/config.py','scout/spikes/detector.py')}
 conn=sqlite3.connect('file:/root/gecko-alpha/scout.db?mode=ro',uri=True,timeout=.25)
 conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,65536)
 conn.execute('PRAGMA query_only=ON');conn.execute('PRAGMA temp_store=MEMORY')
 denied={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH,sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE}
 conn.set_authorizer(lambda action,*_:sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
 conn.execute('BEGIN')
 def query(name,sql,args=params):
  global used
  if time.monotonic()-started>=44: raise TimeoutError('total_45s_limit')
  begin=time.monotonic(); cur=None
  conn.set_progress_handler(lambda:int(time.monotonic()-begin>=10 or time.monotonic()-started>=44),1000)
  try:
   cur=conn.execute(sql,args); rows=cur.fetchmany(1001-used)
   if used+len(rows)>1000:raise RuntimeError('aggregate_output_limit')
   used+=len(rows);out['observations'][name]={'columns':[x[0] for x in cur.description],'rows':rows,'seconds':round(time.monotonic()-begin,6)}
  except (sqlite3.Error,RuntimeError) as exc:out['errors'][name]=type(exc).__name__+':'+str(exc)[:120]
  finally:
   if cur is not None:cur.close()
   conn.set_progress_handler(None,0)
 for table in ('signal_outcome_ledger','trade_decision_events','price_cache','volume_history_cg'):
  query(table+'_columns','SELECT name,type FROM pragma_table_info(?)',(table,))
 predicate="kind='gated_out_sample' AND CASE WHEN json_valid(gate_verdicts) THEN json_extract(gate_verdicts,'$.reason')='suppressed' AND json_extract(gate_verdicts,'$.source_layer')='dispatcher' ELSE 0 END"
 w7="julianday(emitted_at)>=julianday(:w7) AND julianday(emitted_at)<julianday(:now)"
 w14="julianday(emitted_at)>=julianday(:w14) AND julianday(emitted_at)<julianday(:now)"
 query('ledger_retained','SELECT count(*) AS rows,min(emitted_at) AS oldest,max(emitted_at) AS newest,max(labeled_at) AS latest_terminal_label,sum(julianday(emitted_at) IS NULL) AS invalid_timestamp,sum(julianday(emitted_at)>julianday(:now)) AS future_timestamp FROM signal_outcome_ledger')
 query('decision_retained','SELECT count(*) AS rows,min(created_at) AS oldest,max(created_at) AS newest,sum(julianday(created_at) IS NULL) AS invalid_timestamp,sum(julianday(created_at)>julianday(:now)) AS future_timestamp FROM trade_decision_events')
 query('ledger_suppression_7d_by_signal','SELECT surface,count(*) AS rows,count(DISTINCT token_id) AS tokens,min(emitted_at) AS oldest,max(emitted_at) AS newest,sum(price_at_emission IS NOT NULL AND price_at_emission>0) AS positive_anchor_rows FROM signal_outcome_ledger WHERE '+predicate+' AND '+w7+' GROUP BY surface')
 query('decision_suppression_7d_by_signal',"SELECT signal_type,source_module,decision,count(*) AS rows,count(DISTINCT token_id) AS tokens,min(created_at) AS oldest,max(created_at) AS newest FROM trade_decision_events WHERE reason='suppressed' AND julianday(created_at)>=julianday(:w7) AND julianday(created_at)<julianday(:now) GROUP BY signal_type,source_module,decision")
 query('suppression_14d_label_and_anchor_coverage','SELECT label_status,count(*) AS rows,count(DISTINCT token_id) AS tokens,sum(r24h IS NOT NULL) AS recorded_r24h,sum(r7d IS NOT NULL) AS recorded_r7d,sum(price_at_emission IS NOT NULL AND price_at_emission>0) AS positive_anchor_rows,sum(anchor_cache_age_seconds IS NULL) AS unknown_anchor_age_rows,sum(anchor_cache_age_seconds=0) AS zero_anchor_age_rows,sum(anchor_cache_age_seconds>0) AS positive_anchor_age_rows FROM signal_outcome_ledger WHERE '+predicate+' AND '+w14+' GROUP BY label_status')
 query('suppression_14d_earliest_anchor',"WITH cohort AS (SELECT token_id,r7d,price_at_emission,row_number() OVER (PARTITION BY token_id ORDER BY julianday(emitted_at),id) AS rn FROM signal_outcome_ledger WHERE "+predicate+' AND '+w14+" AND token_id IS NOT NULL AND trim(token_id)!='') SELECT count(*) AS tokens,sum(r7d IS NOT NULL) AS earliest_anchor_r7d_tokens,sum(price_at_emission IS NOT NULL AND price_at_emission>0) AS earliest_positive_anchor_tokens FROM cohort WHERE rn=1")
 query('label_queue_retained',"SELECT label_status,count(*) AS rows,min(emitted_at) AS oldest,max(emitted_at) AS newest,sum(julianday(emitted_at)<julianday(:w7)) AS older_than_7d FROM signal_outcome_ledger WHERE label_status IN ('pending','partial') GROUP BY label_status")
 query('price_observation_retained',"SELECT 'volume_history_cg' AS table_name,count(*) AS rows,min(recorded_at) AS oldest,max(recorded_at) AS newest,sum(price IS NOT NULL AND price>0) AS positive_price_rows FROM volume_history_cg UNION ALL SELECT 'price_cache',count(*),min(updated_at),max(updated_at),sum(current_price IS NOT NULL AND current_price>0) FROM price_cache")
except Exception as exc:out['errors']['probe']=type(exc).__name__+':'+str(exc)[:120]
finally:
 if conn is not None:conn.close()
 signal.alarm(0)
 out['elapsed_seconds']=round(time.monotonic()-started,6);out['aggregate_rows']=used;out['complete']=not out['errors']
 print(json.dumps(out,sort_keys=True))

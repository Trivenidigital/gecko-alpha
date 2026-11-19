import { build } from '../dashboard/frontend/node_modules/esbuild/lib/main.js'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
const outfile=process.argv[2], htmlfile=process.argv[3]
await build({stdin:{contents:`
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {SuppressionHealthView} from './components/SuppressionCohortHealthPanel.jsx';
const data={meta:{observed_at:'2026-09-14T02:46:47Z',window_start:'2026-09-07T02:46:47Z',lookback_start:'2026-08-31T02:46:47Z'},state:'recorded_counts_only',population:[{signal_type:'chain_completed',ledger_rows:4452,decision_rows:0,state:'ledger_only'},{signal_type:'losers_contrarian',ledger_rows:13508,decision_rows:13508,state:'equal_counts'},{signal_type:null,ledger_rows:1,decision_rows:0,state:'ledger_only'}],cohort:{rows:30977,distinct_tokens:1517,recorded_r24h:8235,recorded_r7d:4804,earliest_anchor_tokens_with_recorded_r7d:50,invalid_token_rows:0,label_status:{complete:8641,partial:193,pending:18301,unlabelable:3842,unknown:0}},diagnostics:{broader_selected_gated_out_rows:48027,malformed_verdict_rows:0,excluded_ledger_timestamp_rows:0,excluded_decision_timestamp_rows:0}};
const render=view=>renderToStaticMarkup(React.createElement(SuppressionHealthView,{view,onRefresh:()=>{}}));
const normal=render({state:'ready',data});
assert(normal.includes('(14 days)') && normal.includes('(7 days)'));
assert(normal.includes('earliest anchors within 14 days'));
assert(normal.includes('Unknown signal') && normal.includes('ledger only'));
assert(normal.includes('Coverage unknown') && normal.includes('unparseable timestamps cannot be assigned'));
assert(!normal.includes('(30 days)') && !normal.includes('$'));
const unavailable=render({state:'unavailable'}),loading=render({state:'loading'});
assert(unavailable.includes('role="alert"') && !unavailable.includes('30,977'));
assert(loading.includes('disabled=""') && !loading.includes('30,977'));
const empty=render({state:'ready',data:{...data,state:'insufficient_data',population:[],cohort:{rows:0,distinct_tokens:0,recorded_r24h:0,recorded_r7d:0,earliest_anchor_tokens_with_recorded_r7d:0,invalid_token_rows:0,label_status:{complete:0,partial:0,pending:0,unlabelable:0,unknown:0}},diagnostics:{broader_selected_gated_out_rows:0,malformed_verdict_rows:0,excluded_ledger_timestamp_rows:0,excluded_decision_timestamp_rows:0}}});
assert(empty.includes('Insufficient data'));
const escaped=render({state:'ready',data:{...data,population:[{signal_type:'<img src=x>',ledger_rows:1,decision_rows:0,state:'ledger_only'}]}});
assert(escaped.includes('&lt;img') && !escaped.includes('<img'));
fs.writeFileSync(${JSON.stringify(htmlfile)},'<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>'+fs.readFileSync('dashboard/frontend/style.css','utf8')+'</style></head><body><main style="max-width:1000px;margin:20px auto">'+normal+empty+unavailable+loading+'<div style="width:360px;max-width:100%">'+normal+'</div></main></body></html>');
`,resolveDir:path.resolve('dashboard/frontend'),loader:'jsx'},outfile,bundle:true,platform:'node',format:'cjs'})
await import(pathToFileURL(outfile))

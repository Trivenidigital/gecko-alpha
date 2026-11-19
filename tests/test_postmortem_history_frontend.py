"""Execute the real history fetch/paging state machine in Node."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_history_paging_loading_error_reset_and_races():
    script = r"""
import assert from 'node:assert/strict';
import { createHistoryController, historyText, historyPercent } from './dashboard/frontend/postmortemHistory.js';
const requests = [];
const fetcher = (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
let seen=[];
const ctrl = createHistoryController(s => seen.push(s), fetcher);
const ok = (id, next=null) => ({ok:true,json:async()=>({meta:{ok:true,total_records:31},rows:[{id}],has_more:next!==null,next_before_id:next})});
let first = ctrl.refresh();
assert.equal(ctrl.getState().loading,true);
requests[0].resolve(ok('9007199254740995','9007199254740993')); await first;
assert.equal(ctrl.getState().loading,false);
let next=ctrl.next();
assert.match(requests[1].url,/before_id=9007199254740993/);
assert.equal(ctrl.getState().rows.length,0);
requests[1].resolve(ok('2')); await next;
assert.equal(ctrl.getState().previous.length,1);
let back=ctrl.previous(); requests[2].resolve(ok('9007199254740995','9007199254740993')); await back;
assert.equal(ctrl.getState().cursor,null);
assert.equal(ctrl.getState().previous.length,0);
let fail=ctrl.next(); requests[3].reject(Error('network')); await fail;
assert.equal(ctrl.getState().error,'History unavailable. Retry the request.');
assert.equal(ctrl.getState().rows.length,0);
let retry=ctrl.retry(); requests[4].resolve(ok('2')); await retry;
let reset=ctrl.refresh(); requests[5].resolve({ok:true,json:async()=>({meta:{ok:true,total_records:0},rows:[],has_more:false,next_before_id:null})}); await reset;
assert.equal(ctrl.getState().previous.length,0); assert.equal(ctrl.getState().cursor,null);
assert.equal(ctrl.getState().error,null); assert.deepEqual(ctrl.getState().rows,[]);
// Older success and finally cannot replace the latest pending state.
let old=ctrl.refresh(); let latest=ctrl.refresh();
assert.equal(requests[6].options.signal.aborted,true);
requests[6].resolve(ok('stale')); await old;
assert.equal(ctrl.getState().loading,true); assert.deepEqual(ctrl.getState().rows,[]);
requests[7].resolve(ok('current')); await latest;
assert.equal(ctrl.getState().rows[0].id,'current');
// Older failure and finally cannot replace a newer success.
old=ctrl.refresh(); latest=ctrl.refresh(); requests[9].resolve(ok('newest')); await latest;
requests[8].reject(Error('stale failure')); await old;
assert.equal(ctrl.getState().error,null); assert.equal(ctrl.getState().rows[0].id,'newest');
assert.equal(ctrl.getState().loading,false);
let httpError=ctrl.refresh(); requests[10].resolve({ok:false,status:503}); await httpError;
assert.equal(ctrl.getState().error,'History unavailable. Retry the request.');
let stopped=ctrl.refresh(); const notifications=seen.length; ctrl.dispose();
requests[11].resolve(ok('unmounted')); await stopped; assert.equal(seen.length,notifications);
assert.equal(historyPercent(null),'Unavailable'); assert.equal(historyPercent(0),'0.00%'); assert.equal(historyPercent(-2),'-2.00%');
assert.equal(historyText(null,'too_long','No recorded reason'),'Unavailable (too long)');
assert.equal(historyText(null,null,'No recorded reason'),'No recorded reason');
assert.equal(historyText('<script>','', 'Unavailable'),'<script>');
console.log('ok');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr

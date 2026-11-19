"""Exercise the production request controller, including late responses."""

import subprocess
from pathlib import Path


def test_summary_controller_races_and_state_labels():
    script = r"""
import assert from 'node:assert/strict';
import {createSummaryController, summaryHeading, formatPP} from './dashboard/frontend/stopShortfallSummary.js';
let requests=[], seen=[];
const fetcher=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
const c=createSummaryController(s=>seen.push(s),fetcher);
const ok=(state='available')=>({ok:true,json:async()=>({meta:{ok:true},data:{state,mean_shortfall_pp:0}})});
let old=c.refresh(), latest=c.refresh();
assert.equal(requests[0].options.signal.aborted,true);
requests[0].resolve(ok()); await old; assert.equal(c.getState().loading,true);
requests[1].resolve(ok('empty')); await latest;
assert.equal(c.getState().payload.data.state,'empty');
old=c.refresh(); latest=c.refresh(); requests[3].resolve(ok('no_eligible_rows')); await latest;
requests[2].reject(Error('late')); await old;
assert.equal(c.getState().error,null); assert.equal(c.getState().payload.data.state,'no_eligible_rows');
let failure=c.refresh(); assert.equal(c.getState().payload,null);
requests[4].resolve({ok:false,json:async()=>({meta:{data_missing_reason:'query_timeout'}})}); await failure;
assert.equal(c.getState().error,'query timeout'); assert.equal(c.getState().payload,null);
let stopped=c.refresh(); let n=seen.length; c.dispose(); requests[5].resolve(ok()); await stopped;
assert.equal(seen.length,n);
assert.equal(formatPP(0),'0.00 pp'); assert.equal(formatPP(null),'Unavailable');
assert.equal(summaryHeading('empty'),'No stored paper stop exits.');
assert.equal(summaryHeading('no_eligible_rows'),'No eligible stop-exit evidence.');
assert(requests.every(r=>r.url==='/api/trading/stop-shortfall-summary'));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

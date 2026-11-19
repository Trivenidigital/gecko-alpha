"""Execute the production controller with delayed responses and real projection."""

import subprocess
from pathlib import Path


def test_lane_controller_ownership_clocks_and_exact_keys():
    script = r"""
import assert from 'node:assert/strict';
import {createLaneStatusController, projectLaneStatus} from './dashboard/frontend/signalLaneStatus.js';
let wall=Date.parse('2026-09-14T03:00:00Z'), mono=100, pending=[], timers=new Map(), next=0;
const clock=()=>({wall,mono});
const fetcher=(url,opts)=>new Promise((resolve,reject)=>pending.push({url,opts,resolve,reject}));
const scheduler={setTimeout:fn=>{timers.set(++next,fn);return next},clearTimeout:id=>timers.delete(id)};
const evidence={enabled:{sqlite_type:'integer',display:'1',truncated:false,valid:true},suspended_at:{sqlite_type:'null',display:null,truncated:false,valid:true},suspended_reason:{sqlite_type:'null',display:null,truncated:false,valid:true},last_calibration_at:{sqlite_type:'null',display:null,truncated:false,valid:true}};
const payload=(stamp=wall)=>({meta:{ok:true,source:'signal_params',read_only:true,not_execution_eligibility:true,observed_at:new Date(stamp).toISOString()},lanes:[{signal_type:'__proto__',state:'enabled',reason:'enabled_in_store',evidence},{signal_type:'blocked_lane',state:'suspended',reason:'suspended_in_store',evidence}]});
const response=p=>({ok:true,status:200,json:async()=>p});
const c=createLaneStatusController(()=>{}, {fetcher,clock,scheduler});
let old=c.refresh(), fresh=c.refresh();
assert.equal(pending[0].opts.signal.aborted,true);
pending[1].resolve(response(payload())); await fresh;
pending[0].reject(Error('old'));await old;
let p=projectLaneStatus(c.getState(),['__proto__','blocked_lane','tracker'],clock());
assert.deepEqual(p.lanes.map(x=>x.state),['enabled','suspended','unknown']);
assert.equal(p.phase,'current');
wall+=60000;mono+=60000;
assert.equal(projectLaneStatus(c.getState(),['__proto__'],clock()).phase,'stale');
fresh=c.refresh();pending[2].resolve(response(payload()));await fresh;
wall-=6000;
assert.equal(projectLaneStatus(c.getState(),['__proto__'],clock()).phase,'clock_skew');
wall+=6000;
fresh=c.refresh();pending[3].resolve(response(payload()));await fresh;
old=c.refresh();c.setPaused(true);pending[4].resolve(response(payload()));await old;
assert.equal(c.getState().phase,'paused');
assert.equal(projectLaneStatus(c.getState(),['__proto__'],clock()).phase,'paused');
c.setPaused(false); // resume always starts a new request
assert.notEqual(c.getState().phase,'current');
pending[5].resolve(response(payload()));await new Promise(r=>setImmediate(r));
fresh=c.refresh();const timeout=[...timers.values()][0];timeout();
pending[6].resolve(response(payload()));await fresh;
assert.equal(c.getState().phase,'unavailable');
fresh=c.refresh();pending[7].resolve(response(payload(wall+6000)));await fresh;
assert.equal(c.getState().phase,'unavailable');
fresh=c.refresh();let malformed=payload();malformed.lanes.push(malformed.lanes[0]);pending[8].resolve(response(malformed));await fresh;
assert.equal(c.getState().phase,'unavailable');
fresh=c.refresh();let empty=payload();empty.lanes=[];pending[9].resolve(response(empty));await fresh;
assert.equal(projectLaneStatus(c.getState(),['__proto__'],clock()).lanes[0].reason,'no_store_row');
assert.equal(projectLaneStatus(c.getState(),[],clock()).lanes[0].reason,'missing_source');
old=c.refresh();c.dispose();pending[10].resolve(response(payload()));await old;
assert.notEqual(c.getState().phase,'current');
assert.equal(timers.size,0);
assert(pending.every(r=>r.url==='/api/signal_lane_status'));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_actual_annotation_render_and_existing_board_are_independent(tmp_path):
    import pytest

    root = Path(__file__).resolve().parents[1]
    if not (root / "dashboard/frontend/node_modules/esbuild").exists():
        pytest.skip(
            "Actual React render requires npm ci; controller test remains Node-only"
        )
    script = r"""
import {build} from './dashboard/frontend/node_modules/esbuild/lib/main.js';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
const outfile=process.argv[1];
await build({stdin:{contents:`
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import assert from 'node:assert/strict';
import Annotation from './components/LaneStatusAnnotations.jsx';
import {projectLaneStatus} from './signalLaneStatus.js';
import {buildTradeDecisionBoard} from './components/tradeDecisionBoard.js';
const now={wall:Date.parse('2026-09-14T03:00:00Z'),mono:100};
const rows=[{token_id:'a',source_corpus:'paper',group:'watch',surfaces:['enabled_lane','suspended_lane','tracker'],adjusted_score:80},{token_id:'b',source_corpus:'tracker',group:'watch',surfaces:['tracker'],adjusted_score:50}];
const payload={groups:{act_now:[],watch:rows,already_ran:[],blocked:[]},meta:{}};
const before=JSON.stringify(payload),board=JSON.stringify(buildTradeDecisionBoard(payload));
const status={phase:'current',snapshot:{observed:now.wall,observedAt:new Date(now.wall).toISOString(),received:now,lanes:new Map([['enabled_lane',{signal_type:'enabled_lane',state:'enabled',reason:'enabled_in_store'}],['suspended_lane',{signal_type:'suspended_lane',state:'suspended',reason:'suspended_in_store'}]])}};
const html=renderToStaticMarkup(React.createElement(Annotation,{surfaces:rows[0].surfaces,status,now}));
assert(html.includes('enabled_lane: enabled'));
assert(html.includes('suspended_lane: suspended'));
assert(html.includes('tracker: unknown'));
assert(html.includes('<summary') && html.includes('Recorded lane status details'));
const stale=renderToStaticMarkup(React.createElement(Annotation,{surfaces:rows[0].surfaces,status,now:{wall:now.wall+60000,mono:now.mono+60000}}));
assert(stale.includes('Lane status: stale'));
assert(stale.includes('last observed enabled'));
assert(!stale.split('</summary>')[0].includes('enabled_lane: enabled'));
for(const row of rows)projectLaneStatus(status,row.surfaces,now);
assert.equal(JSON.stringify(payload),before);
assert.equal(JSON.stringify(buildTradeDecisionBoard(payload)),board);
`,resolveDir:path.resolve('dashboard/frontend'),loader:'jsx'},outfile,bundle:true,platform:'node',format:'cjs'});
await import(pathToFileURL(outfile));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(tmp_path / "render.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

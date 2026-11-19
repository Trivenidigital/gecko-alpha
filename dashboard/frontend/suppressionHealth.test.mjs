import test from 'node:test'
import assert from 'node:assert/strict'
import { createHealthController } from './suppressionHealth.js'

const valid = { meta: { ok: true, observed_at: '2026-09-14T00:00:00Z', window_start: '2026-09-07T00:00:00Z', lookback_start:'2026-08-31T00:00:00Z', window_days:7, lookback_days:14, coverage: 'unknown', provenance: 'unverified', conclusions: 'insufficient_evidence_for_cost_or_ranking' }, state: 'insufficient_data', population: [], cohort: {rows:0,distinct_tokens:0,recorded_r24h:0,recorded_r7d:0,earliest_anchor_tokens_with_recorded_r7d:0,invalid_token_rows:0,label_status:{complete:0,partial:0,pending:0,unlabelable:0,unknown:0}}, diagnostics: {broader_selected_gated_out_rows:0,malformed_verdict_rows:0,excluded_ledger_timestamp_rows:0,excluded_decision_timestamp_rows:0} }
test('manual-only controller invalidates timed-out and disposed requests', async () => {
  const states = [], pending = [], timers = []
  const controller = createHealthController(s => states.push(s), {
    fetcher: () => new Promise(resolve => pending.push(resolve)),
    schedule: fn => { timers.push(fn); return timers.length }, cancel: () => {},
  })
  const first = controller.refresh()
  assert.equal(pending.length, 1)
  timers[0]()
  assert.equal(states.at(-1).state, 'unavailable')
  pending[0]({ ok: true, json: async () => valid })
  await first
  assert.equal(states.at(-1).state, 'unavailable')
  const second = controller.refresh()
  controller.dispose()
  const count = states.length
  pending[1]({ ok: true, json: async () => valid })
  await second
  assert.equal(states.length, count)
  assert.equal(pending.length, 2)
})
test('successful empty and unavailable responses stay distinct', async () => {
  let state
  const controller = createHealthController(s => { state = s }, { fetcher: async () => ({ ok: true, json: async () => valid }) })
  await controller.refresh()
  assert.equal(state.state, 'ready')
  assert.equal(state.data.state, 'insufficient_data')
  controller.dispose()
  const bad = createHealthController(s => { state = s }, { fetcher: async () => ({ ok: false }) })
  await bad.refresh()
  assert.equal(state.state, 'unavailable')
  bad.dispose()
})
test('reject partial counts, wrong windows and malformed table cells', async () => {
  for (const mutate of [x => {x.meta.lookback_days=30}, x => {delete x.cohort.rows}, x => {x.population=[{signal_type:'x'}]}, x => {x.cohort.rows=-1}]) {
    const data = structuredClone(valid); mutate(data)
    let state
    const controller = createHealthController(s => {state=s}, {fetcher:async()=>({ok:true,json:async()=>data})})
    await controller.refresh()
    assert.equal(state.state,'unavailable')
    controller.dispose()
  }
})
test('older rejection cannot replace a newer successful observation', async () => {
  let state; const pending=[]
  const controller=createHealthController(s=>{state=s},{fetcher:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))})
  const old=controller.refresh(), latest=controller.refresh()
  pending[1].resolve({ok:true,json:async()=>valid}); await latest
  pending[0].reject(Error('late')); await old
  assert.equal(state.state,'ready'); controller.dispose()
})

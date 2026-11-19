import assert from 'node:assert/strict'
import { createTelegramOutcomeRequest } from '../dashboard/frontend/components/telegramOutcomeRequest.js'

const pending = []
let state = {}
const changes = []
const request = createTelegramOutcomeRequest((update) => {
  state = {...state, ...update}
  changes.push({...state})
}, (url, options) => new Promise((resolve, reject) => pending.push({url, options, resolve, reject})))
const ok = (n) => ({ok:true, json:async()=>({meta:{ok:true},total_events:n})})

const first = request.load(1)
assert.equal(state.loading, true)
const second = request.load(30)
assert.equal(pending[0].options.signal.aborted, true)
assert.equal(pending[1].url, '/api/tg_alerts/outcomes?days=30')
pending[1].resolve(ok(30)); await second
assert.equal(state.data.total_events,30)
const count = changes.length
pending[0].resolve(ok(1)); await first
assert.equal(changes.length,count, 'stale success/finally mutated state')

const third = request.load(7)
assert.equal(state.data,null)
assert.equal(state.error,null)
const fourth = request.load(1)
pending[2].reject(new Error('older error')); await third
assert.equal(state.loading,true,'stale error/finally stopped newer loading')
pending[3].resolve({ok:false}); await fourth
assert.equal(state.loading,false)
assert.equal(state.data,null)
assert.equal(state.error,'Counts unavailable. Retry the request.')

const unavailable = request.load(1)
pending[4].resolve({ok:true,json:async()=>({meta:{ok:false},total_events:0})}); await unavailable
assert.equal(state.data,null,'HTTP200 unavailable became zero success')
assert.ok(state.error)

const network = request.load(1)
pending[5].reject(new Error('private network detail')); await network
assert.equal(state.error,'Counts unavailable. Retry the request.')

const zero = request.load(1)
pending[6].resolve(ok(0)); await zero
assert.equal(state.data.total_events,0)
assert.equal(state.error,null)

const disposed = request.load(7)
request.dispose()
const beforeDisposed = changes.length
pending[7].resolve(ok(7)); await disposed
assert.equal(changes.length,beforeDisposed,'disposed request emitted state')
await request.load(1)
assert.equal(pending.length,8)
console.log('Telegram outcome lifecycle checks passed')

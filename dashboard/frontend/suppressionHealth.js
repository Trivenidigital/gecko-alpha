function validPayload(data) {
  const m=data?.meta, c=data?.cohort, d=data?.diagnostics
  const count = value => Number.isSafeInteger(value) && value>=0
  const fields = (object, keys) => object && keys.every(key=>count(object[key]))
  const states=['equal_counts','ledger_only','decision_only','ledger_excess','ledger_fewer']
  if (!m?.ok || m.coverage!=='unknown' || m.provenance!=='unverified' ||
      m.conclusions!=='insufficient_evidence_for_cost_or_ranking' || m.window_days!==7 || m.lookback_days!==14 ||
      !['observed_at','window_start','lookback_start'].every(key=>typeof m[key]==='string' && Number.isFinite(Date.parse(m[key]))) ||
      !['insufficient_data','recorded_counts_only'].includes(data.state) ||
      !fields(c,['rows','distinct_tokens','recorded_r24h','recorded_r7d','earliest_anchor_tokens_with_recorded_r7d','invalid_token_rows']) ||
      !fields(c.label_status,['complete','partial','pending','unlabelable','unknown']) ||
      !fields(d,['broader_selected_gated_out_rows','malformed_verdict_rows','excluded_ledger_timestamp_rows','excluded_decision_timestamp_rows']) ||
      !Array.isArray(data.population) || data.population.length>256) return false
  const keys=new Set()
  return data.population.every(row=> {
    if (!row || !(row.signal_type===null || typeof row.signal_type==='string' && row.signal_type.length<=128) ||
        keys.has(row.signal_type) || !fields(row,['ledger_rows','decision_rows']) || !states.includes(row.state)) return false
    keys.add(row.signal_type); return true
  })
}

export function createHealthController(publish, { fetcher = fetch, schedule = setTimeout, cancel = clearTimeout } = {}) {
  let generation = 0, disposed = false, abort = null, timer = null
  return {
    async refresh() {
      if (disposed) return
      const own = ++generation
      abort?.abort()
      cancel(timer)
      abort = new AbortController()
      publish({ state: 'loading' })
      timer = schedule(() => {
        if (disposed || own !== generation) return
        generation++
        abort.abort()
        publish({ state: 'unavailable' })
      }, 7000)
      try {
        const response = await fetcher('/api/suppression_cohort/health', { signal: abort.signal, cache: 'no-store' })
        if (!response.ok) throw new Error('unavailable')
        const data = await response.json()
        if (!validPayload(data)) throw new Error('invalid')
        if (!disposed && own === generation) publish({ state: 'ready', data })
      } catch {
        if (!disposed && own === generation) publish({ state: 'unavailable' })
      } finally {
        if (own === generation) cancel(timer)
      }
    },
    dispose() { disposed = true; generation++; abort?.abort(); cancel(timer) },
  }
}

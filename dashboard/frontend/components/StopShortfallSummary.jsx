import React, { useEffect, useRef, useState } from 'react'
import { createSummaryController, formatPP, summaryHeading } from '../stopShortfallSummary.js'

export default function StopShortfallSummary() {
  const [state, setState] = useState({ loading: true, payload: null, error: null })
  const controller = useRef(null)
  useEffect(() => {
    const request = createSummaryController(setState)
    controller.current = request
    request.refresh()
    return () => request.dispose()
  }, [])
  const data = state.payload?.data
  return <section aria-label="Historical entry-stop shortfall" style={{ margin: '20px 0', padding: 16, border: '1px solid #3a3a4a', borderRadius: 8 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
      <h3 style={{ margin: 0, fontSize: 16 }}>Historical entry-stop shortfall <small style={{ color: '#aab', fontSize: 11 }}>PAPER / EXPERIMENTAL</small></h3>
      <button disabled={state.loading} onClick={() => controller.current?.refresh()}>{state.error ? 'Retry' : 'Refresh'}</button>
    </div>
    <p style={{ color: '#aab', fontSize: 12 }}>All stored paper stop exits — independent of table filters.</p>
    <div aria-live="polite">
      {state.loading ? <p>Loading summary…</p> : state.error ? <p>Summary unavailable: {state.error}.</p> : data && <>
        {summaryHeading(data.state) && <p>{summaryHeading(data.state)}</p>}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24 }}>
          <div>Mean <strong>{formatPP(data.mean_shortfall_pp)}</strong></div>
          <div>Median <strong>{formatPP(data.median_shortfall_pp)}</strong></div>
          <div>Eligible paper trades <strong>{data.eligible_rows} / {data.total_stop_rows}</strong> stop exits</div>
          <div>Modeled {data.modeled_rows} · Unavailable {data.unavailable_rows}</div>
        </div>
        {Object.keys(data.exclusions_by_reason || {}).length > 0 && <p style={{ fontSize: 12 }}>
          Exclusions: {Object.entries(data.exclusions_by_reason).map(([reason, count]) => `${reason.replaceAll('_', ' ')}: ${count}`).join(' · ')}
        </p>}
        {data.total_stop_rows > 0 && <p style={{ fontSize: 12 }}>Historical sample; repeated tokens may appear. Not for pruning, sizing or dispatch.</p>}
        <p style={{ fontSize: 11, color: '#aab' }}>Snapshot generated {state.payload.meta.generated_at}</p>
      </>}
    </div>
    <p style={{ fontSize: 12, color: '#aab', marginBottom: 0 }}>Recorded exits already include modeled paper slippage. This compares recorded exit prices with the frozen entry stop; it is not execution slippage, terminal-stop overshoot or expectancy.</p>
  </section>
}

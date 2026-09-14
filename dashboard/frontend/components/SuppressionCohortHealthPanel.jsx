import React, { useEffect, useRef, useState } from 'react'
import { createHealthController } from '../suppressionHealth.js'

const number = value => Number.isInteger(value) ? value.toLocaleString() : '—'

export function SuppressionHealthContent({ data }) {
  const c = data.cohort, d = data.diagnostics
  return <>
    <p>Last observed {data.meta.observed_at}. Refresh to update.</p>
    <p>Population: {data.meta.window_start} to {data.meta.observed_at} (7 days).<br />
      Label lookback: {data.meta.lookback_start} to {data.meta.observed_at} (14 days). Retained rows only; older records are outside this view.</p>
    {data.state === 'insufficient_data' && <p role="status">Insufficient data: no classified suppression records in this lookback.</p>}
    <p>Coverage unknown. Equal counts do not prove matched events; some producers record only ledger rows.</p>
    <div style={{ overflowX: 'auto' }} tabIndex={0} role="region" aria-label="Recorded population counts"><table>
      <thead><tr><th>Signal</th><th>Ledger rows</th><th>Decision rows</th><th>Population</th></tr></thead>
      <tbody>{data.population.map(row => <tr key={row.signal_type ?? '__unknown'}>
        <td>{row.signal_type ?? 'Unknown signal'}</td><td>{number(row.ledger_rows)}</td>
        <td>{number(row.decision_rows)}</td><td>{row.state.replaceAll('_', ' ')}</td>
      </tr>)}</tbody>
    </table></div>
    <p>{number(c.rows)} cohort rows · {number(c.distinct_tokens)} distinct tokens · {number(c.earliest_anchor_tokens_with_recorded_r7d)} earliest anchors within 14 days with recorded r7d</p>
    <p>Anchors change as the window moves; changes in this count do not establish improved maturation or provenance.</p>
    <p>Recorded label availability: r24h {number(c.recorded_r24h)}; r7d {number(c.recorded_r7d)}. Availability does not verify price provenance.</p>
    <p>Stored status: {Object.entries(c.label_status ?? {}).map(([key, value]) => `${key}: ${number(value)}`).join(' · ')}. “Complete” is not a guarantee of an r7d label.</p>
    <p>Broader selected gated-out rows: {number(d.broader_selected_gated_out_rows)}; malformed verdicts: {number(d.malformed_verdict_rows)}. Invalid token rows: {number(c.invalid_token_rows)}.</p>
    <p>Selected timestamp parse exclusions: ledger {number(d.excluded_ledger_timestamp_rows)}, decisions {number(d.excluded_decision_timestamp_rows)}. Table-wide invalid/future timestamp counts are not measured here; unparseable timestamps cannot be assigned to these windows.</p>
  </>
}

export function SuppressionHealthView({ view, onRefresh }) {
  return <section className="panel suppression-health" aria-label="Suppression cohort health" style={{ padding: '1rem', overflowWrap: 'anywhere' }}>
    <h3>Suppression cohort health <small>EXPERIMENTAL</small></h3>
    <p>Recorded labels do not verify prices; insufficient evidence for cost or ranking.</p>
    <button className="filter-btn" disabled={view.state === 'loading'} onClick={onRefresh}>Refresh cohort counts</button>
    {view.state === 'loading' && <p role="status">Loading recorded counts…</p>}
    {view.state === 'unavailable' && <p role="alert">Cohort counts unavailable. Retry with Refresh.</p>}
    {view.state === 'ready' && <SuppressionHealthContent data={view.data} />}
  </section>
}

export default function SuppressionCohortHealthPanel() {
  const [view, setView] = useState({ state: 'loading' })
  const controller = useRef(null)
  useEffect(() => {
    const owner = createHealthController(setView)
    controller.current = owner
    owner.refresh()
    return () => { owner.dispose(); controller.current = null }
  }, [])
  return <SuppressionHealthView view={view} onRefresh={() => controller.current?.refresh()} />
}

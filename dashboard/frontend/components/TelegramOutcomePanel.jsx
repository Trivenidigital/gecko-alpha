import React, {useEffect, useState} from 'react'
import {createTelegramOutcomeRequest} from './telegramOutcomeRequest.js'

const OUTCOMES = [
  ['sent', 'Recorded sent'],
  ['blocked_eligibility', 'Blocked eligibility'],
  ['blocked_cooldown', 'Blocked cooldown'],
  ['blocked_dedup_24h', 'Blocked 24-hour deduplication'],
  ['dispatch_failed', 'Recorded dispatch failure'],
  ['announcement_sent', 'Recorded announcement sent'],
  ['m1_5c_announcement_sent', 'Recorded M1.5c announcement sent'],
  ['other_recorded_outcomes', 'Other recorded outcomes'],
]
const ELIGIBILITY = [
  ['paper_open_universe', 'Paper-open universe filter'],
  ['detection_universe', 'Detection-lane universe filter'],
  ['other_or_unspecified', 'Other or unspecified eligibility'],
]

export default function TelegramOutcomePanel() {
  const [days, setDays] = useState(1)
  const [refresh, setRefresh] = useState(0)
  const [state, setState] = useState({loading: true, data: null, error: null})
  useEffect(() => {
    const request = createTelegramOutcomeRequest((update) => setState((prior) => ({...prior, ...update})))
    request.load(days)
    return () => request.dispose()
  }, [days, refresh])
  const {loading, data, error} = state
  const retry = () => setRefresh((value) => value + 1)
  return (
    <section className="panel" aria-label="Recorded Telegram outcomes">
      <div className="panel-header">
        Recorded Telegram outcomes
        <span className="funnel-window-picker">
          {[1, 7, 30].map((value) => (
            <button key={value} type="button" aria-pressed={days === value}
              className={`funnel-window-btn ${days === value ? 'active' : ''}`}
              onClick={() => setDays(value)}>{value}d</button>
          ))}
          <button type="button" className="funnel-window-btn" onClick={retry}>Refresh</button>
        </span>
      </div>
      <div style={{padding: '16px', lineHeight: 1.6}}>
      <p>Counts are recorded events, not unique tokens. Recorded sent is not independent delivery confirmation.</p>
      <p>Eligibility detail describes recorded exclusions. Other or unspecified does not mean proven ineligible.</p>
      {loading && <div className="empty-state" role="status">Loading recorded outcomes…</div>}
      {!loading && error && <div className="empty-state" role="alert">{error} <button type="button" onClick={retry}>Retry</button></div>}
      {!loading && !error && data && (
        <>
          <p><strong>{data.total_events}</strong> recorded events in this window</p>
          <p>Window: {data.meta.window_start} through {data.meta.as_of} (inclusive).</p>
          <p>SQLite interprets parseable timestamps without an offset as UTC; comparisons use approximately millisecond precision.</p>
          {(data.meta.table_wide_invalid_timestamp_count > 0 || data.meta.table_wide_future_timestamp_count > 0) && (
            <p>Across the entire ledger, excluded from window counts: {data.meta.table_wide_invalid_timestamp_count} unparseable timestamps; {data.meta.table_wide_future_timestamp_count} future timestamps.</p>
          )}
          {data.total_events === 0 && <div className="empty-state">No recorded events in this window.</div>}
          <div className="tg-table-scroll">
            <table className="tg-table">
              <thead><tr><th>Recorded outcome</th><th>Events</th></tr></thead>
              <tbody>{OUTCOMES.map(([key, label]) => <tr key={key}><td>{label}</td><td>{data.outcomes[key]}</td></tr>)}</tbody>
            </table>
          </div>
          <h3 style={{marginTop: 16}}>Blocked eligibility detail</h3>
          <div className="tg-table-scroll">
            <table className="tg-table">
              <thead><tr><th>Recorded category</th><th>Events</th></tr></thead>
              <tbody>{ELIGIBILITY.map(([key, label]) => <tr key={key}><td>{label}</td><td>{data.blocked_eligibility[key]}</td></tr>)}</tbody>
            </table>
          </div>
        </>
      )}
      </div>
    </section>
  )
}

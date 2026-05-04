import React, { useEffect, useState } from 'react'
import TGDLQPanel from './TGDLQPanel.jsx'

function fmtTime(iso) {
  if (!iso) return '–'
  try {
    const d = new Date(iso)
    return d.toLocaleString([], {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function fmtMcap(v) {
  if (v == null) return '–'
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`
  return `$${v}`
}

// cashtags + contracts come back as JSON-encoded strings from the parser.
// Empty cases land as '', '[]', or '"[]"' depending on path; defensive parse.
function parseList(raw) {
  if (!raw || raw === '[]' || raw === '""') return []
  try {
    const v = JSON.parse(raw)
    if (Array.isArray(v)) return v
    // Some rows store contracts as JSON of objects with .address
    if (typeof v === 'object' && v !== null) return [v]
    return []
  } catch {
    return []
  }
}

function ResolutionBadge({ resolution }) {
  if (!resolution || !resolution.state) {
    return <span className="tg-badge tg-badge-muted">no signal</span>
  }
  const state = resolution.state
  if (resolution.paper_trade_id) {
    return (
      <span className="tg-badge tg-badge-ok">
        traded #{resolution.paper_trade_id}
      </span>
    )
  }
  if (state === 'RESOLVED') {
    return <span className="tg-badge tg-badge-info">resolved (alert)</span>
  }
  if (state === 'UNRESOLVED_TRANSIENT') {
    return <span className="tg-badge tg-badge-warn">retrying</span>
  }
  if (state === 'UNRESOLVED_TERMINAL') {
    return <span className="tg-badge tg-badge-muted">unresolved</span>
  }
  return <span className="tg-badge tg-badge-muted">{state}</span>
}

export default function TGAlertsTab() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch('/api/tg_social/alerts?limit=80')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setData(json)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      }
    }
    load()
    const t = setInterval(load, 15_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  if (error) {
    return (
      <div className="panel">
        <div className="panel-header">TG Alerts</div>
        <div className="empty-state">Failed to load: {error}</div>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="panel">
        <div className="panel-header">TG Alerts</div>
        <div className="empty-state">Loading…</div>
      </div>
    )
  }

  const stats = data.stats_24h || {}
  const channels = data.channels || []
  const health = data.health || {}
  const alerts = data.alerts || []

  return (
    <div className="tg-alerts">
      <div className="panel">
        <div className="panel-header">TG Alerts — last 24h rollup</div>
        <div className="tg-stat-row">
          <div className="tg-stat">
            <div className="tg-stat-label">Messages</div>
            <div className="tg-stat-value">{stats.messages ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">With CA</div>
            <div className="tg-stat-value">{stats.with_ca ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">With Cashtag</div>
            <div className="tg-stat-value">{stats.with_cashtag ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">Signals Resolved</div>
            <div className="tg-stat-value">{stats.signals_resolved ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">Trades Dispatched</div>
            <div className="tg-stat-value">{stats.trades_dispatched ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">Cashtag Dispatched</div>
            <div className="tg-stat-value">{stats.cashtag_dispatched_24h ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">DLQ</div>
            <div className="tg-stat-value">{stats.dlq ?? 0}</div>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">Channels</div>
        <table className="tg-table">
          <thead>
            <tr>
              <th>Channel</th>
              <th>Trade-eligible</th>
              <th>Safety required</th>
              <th>Cashtag-eligible</th>
              <th>Cashtag today</th>
              <th>Listener</th>
              <th>Last message</th>
            </tr>
          </thead>
          <tbody>
            {channels.filter(c => !c.removed).map(c => {
              const h = health[`channel:${c.channel_handle}`] || {}
              // BL-066': defensive ?? defaults so a stale-cache or
              // pre-extension API response renders dashes, not "undefined".
              const today = c.cashtag_dispatched_today
              const cap = c.cashtag_cap_per_day
              const nearCap = today != null && cap != null && today >= cap
              return (
                <tr key={c.channel_handle}>
                  <td>{c.channel_handle}</td>
                  <td>{c.trade_eligible ? 'yes' : 'no'}</td>
                  <td>{c.safety_required ? 'yes' : 'no'}</td>
                  <td>{c.cashtag_trade_eligible ? 'yes' : 'no'}</td>
                  <td>
                    <span className={
                      nearCap ? 'tg-badge tg-badge-warn' : 'tg-badge tg-badge-muted'
                    }>
                      {today ?? '–'} / {cap ?? '–'}
                    </span>
                  </td>
                  <td>
                    <span className={`tg-badge ${
                      h.state === 'running' ? 'tg-badge-ok' : 'tg-badge-warn'
                    }`}>
                      {h.state || 'unknown'}
                    </span>
                  </td>
                  <td>{fmtTime(h.last_message_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <div className="panel-header">Recent messages ({alerts.length})</div>
        {alerts.length === 0 ? (
          <div className="empty-state">No messages yet</div>
        ) : (
          <table className="tg-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Channel</th>
                <th>Cashtags</th>
                <th>CAs</th>
                <th>Resolution</th>
                <th>Symbol / Mcap</th>
                <th>Text</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map(a => {
                const cashtags = parseList(a.cashtags)
                const contracts = parseList(a.contracts)
                const r = a.resolution
                return (
                  <tr key={a.id}>
                    <td>{fmtTime(a.posted_at)}</td>
                    <td>{a.channel_handle}</td>
                    <td>{cashtags.length ? cashtags.join(', ') : '–'}</td>
                    <td>{contracts.length ? `${contracts.length}` : '–'}</td>
                    <td><ResolutionBadge resolution={r} /></td>
                    <td>
                      {r && r.symbol ? `${r.symbol} (${fmtMcap(r.mcap)})` : '–'}
                    </td>
                    <td className="tg-text-cell">
                      {a.text_preview || '(empty)'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <TGDLQPanel />
    </div>
  )
}

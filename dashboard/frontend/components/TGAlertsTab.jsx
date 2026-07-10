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

function fmtPct(v) {
  if (v == null || !Number.isFinite(v)) return '–'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(1)}%`
}

function fmtUsd(v) {
  if (v == null || !Number.isFinite(v)) return '–'
  const sign = v >= 0 ? '+' : '-'
  return `${sign}$${Math.abs(v).toFixed(2)}`
}

// DASH-06: linked paper-trade outcome for a sent alert. 81% of sent rows
// carry a paper_trade_id; unlinked rows (and the defensive 'missing' case
// for a dangling historical FK) show an explicit 'unlinked' tag, never blank.
function OutcomeCell({ outcome }) {
  if (!outcome || !outcome.linked || outcome.state === 'missing') {
    return <span className="tg-badge tg-badge-muted">unlinked</span>
  }
  if (outcome.state === 'open') {
    const peak = outcome.peak_pct != null ? ` · peak ${fmtPct(outcome.peak_pct)}` : ''
    return <span className="tg-badge tg-badge-info">open{peak}</span>
  }
  // closed — realized PnL present
  const positive = (outcome.pnl_usd ?? 0) >= 0
  return (
    <span className={`tg-badge ${positive ? 'tg-badge-ok' : 'tg-badge-warn'}`}>
      {fmtUsd(outcome.pnl_usd)} / {fmtPct(outcome.pnl_pct)}
      {outcome.exit_reason ? ` · ${outcome.exit_reason}` : ''}
    </span>
  )
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
  const [dispatchData, setDispatchData] = useState(null)
  const [error, setError] = useState(null)
  const [markingId, setMarkingId] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [res, dispatchRes] = await Promise.all([
          fetch('/api/tg_social/alerts?limit=80'),
          fetch('/api/tg_alerts/recent?limit=80'),
        ])
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        if (!dispatchRes.ok) throw new Error(`HTTP ${dispatchRes.status}`)
        const json = await res.json()
        const dispatchJson = await dispatchRes.json()
        if (!cancelled) {
          setData(json)
          setDispatchData(dispatchJson)
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

  async function markDispatch(alertId, action) {
    setMarkingId(alertId)
    try {
      const res = await fetch(`/api/tg_alerts/${alertId}/operator-action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const updated = await res.json()
      setDispatchData(prev => {
        if (!prev) return prev
        return {
          ...prev,
          alerts: (prev.alerts || []).map(a => (
            a.id === alertId
              ? {
                  ...a,
                  operator_action: {
                    action: updated.action,
                    note: updated.note,
                    source: updated.source,
                    marked_at: updated.marked_at,
                    updated_at: updated.updated_at,
                  },
                }
              : a
          )),
        }
      })
    } catch (e) {
      setError(String(e))
    } finally {
      setMarkingId(null)
    }
  }

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
  const dispatchAlerts = (dispatchData && dispatchData.alerts) || []
  const actionLabels = {
    acted: 'Acted',
    useful: 'Useful',
    ignored: 'Ignored',
    false_positive: 'Bad',
  }

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
        <div className="panel-header">Recent Telegram dispatches</div>
        {dispatchAlerts.length === 0 ? (
          <div className="empty-state">No sent dispatches in this view</div>
        ) : (
          <table className="tg-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Signal</th>
                <th>Token</th>
                <th>Paper trade</th>
                <th>Outcome</th>
                <th>Label</th>
                <th>Mark</th>
              </tr>
            </thead>
            <tbody>
              {dispatchAlerts.map(a => {
                const current = a.operator_action && a.operator_action.action
                return (
                  <tr key={a.id}>
                    <td>{fmtTime(a.alerted_at)}</td>
                    <td>{a.signal_type}</td>
                    <td>{a.token_id}</td>
                    <td>{a.paper_trade_id ? `#${a.paper_trade_id}` : '-'}</td>
                    <td><OutcomeCell outcome={a.outcome} /></td>
                    <td>
                      <span className={`tg-badge ${current ? 'tg-badge-info' : 'tg-badge-muted'}`}>
                        {current ? actionLabels[current] : 'unmarked'}
                      </span>
                    </td>
                    <td>
                      <div className="tg-action-buttons">
                        {Object.entries(actionLabels).map(([value, label]) => (
                          <button
                            key={value}
                            type="button"
                            className={`tg-action-btn ${current === value ? 'active' : ''}`}
                            disabled={markingId === a.id}
                            onClick={() => markDispatch(a.id, value)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
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

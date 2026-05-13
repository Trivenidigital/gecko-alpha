import React, { useEffect, useState } from 'react'

function fmtTime(iso) {
  if (!iso) return '-'
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

function fmtConfidence(v) {
  if (v == null) return '-'
  return `${Math.round(Number(v) * 100)}%`
}

function AlertAsset({ alert }) {
  if (alert.extracted_cashtag) {
    return <span className="tg-badge tg-badge-info">{alert.extracted_cashtag}</span>
  }
  if (alert.extracted_ca) {
    return (
      <span className="x-contract" title={alert.extracted_ca}>
        {alert.extracted_ca.slice(0, 8)}...{alert.extracted_ca.slice(-6)}
      </span>
    )
  }
  return <span className="tg-badge tg-badge-muted">none</span>
}

function UrgencyBadge({ value }) {
  const key = (value || 'unknown').toLowerCase()
  const cls = key === 'high' ? 'tg-badge-warn' : key === 'critical' ? 'tg-badge-ok' : 'tg-badge-muted'
  return <span className={`tg-badge ${cls}`}>{value || 'unknown'}</span>
}

export default function XAlertsTab() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch('/api/x_alerts?limit=80')
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
        <div className="panel-header">X Alerts</div>
        <div className="empty-state">Failed to load: {error}</div>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="panel">
        <div className="panel-header">X Alerts</div>
        <div className="empty-state">Loading...</div>
      </div>
    )
  }

  const stats = data.stats_24h || {}
  const alerts = data.alerts || []

  return (
    <div className="x-alerts">
      <div className="panel">
        <div className="panel-header">X Alerts - last 24h rollup</div>
        <div className="tg-stat-row x-stat-row">
          <div className="tg-stat">
            <div className="tg-stat-label">Alerts</div>
            <div className="tg-stat-value">{stats.alerts ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">KOLs</div>
            <div className="tg-stat-value">{stats.unique_authors ?? 0}</div>
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
            <div className="tg-stat-label">Resolved</div>
            <div className="tg-stat-value">{stats.resolved ?? 0}</div>
          </div>
          <div className="tg-stat">
            <div className="tg-stat-label">Avg Confidence</div>
            <div className="tg-stat-value">{fmtConfidence(stats.avg_confidence)}</div>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">Recent X signals ({alerts.length})</div>
        {alerts.length === 0 ? (
          <div className="empty-state">No X alerts yet</div>
        ) : (
          <table className="tg-table x-alert-table">
            <thead>
              <tr>
                <th>Received</th>
                <th>KOL</th>
                <th>Asset</th>
                <th>Chain</th>
                <th>Theme</th>
                <th>Urgency</th>
                <th>Confidence</th>
                <th>Resolved</th>
                <th>Tweet</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map(a => (
                <tr key={a.event_id}>
                  <td>{fmtTime(a.received_at)}</td>
                  <td>
                    {a.tweet_url ? (
                      <a className="x-author-link" href={a.tweet_url} target="_blank" rel="noreferrer">
                        @{a.tweet_author}
                      </a>
                    ) : (
                      `@${a.tweet_author || '-'}`
                    )}
                  </td>
                  <td><AlertAsset alert={a} /></td>
                  <td>{a.extracted_chain || '-'}</td>
                  <td>{a.narrative_theme || '-'}</td>
                  <td><UrgencyBadge value={a.urgency_signal} /></td>
                  <td>{fmtConfidence(a.classifier_confidence)}</td>
                  <td>{a.resolved_coin_id || '-'}</td>
                  <td className="tg-text-cell">{a.text_preview || '(empty)'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

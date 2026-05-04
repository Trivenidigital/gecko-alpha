// BL-066': DLQ inspector panel for tg_social pipeline.
// Renders inside TGAlertsTab. 30s poll cadence (vs 15s for the parent
// stats panel) because DLQ rows change slowly and carry larger payloads.
import React, { useEffect, useState } from 'react'

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

export default function TGDLQPanel() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch('/api/tg_social/dlq?limit=20')
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setRows(json)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      }
    }
    load()
    const t = setInterval(load, 30_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  if (error) {
    return (
      <div className="panel">
        <div className="panel-header">DLQ — recent failures (refreshes every 30s)</div>
        <div className="empty-state">Failed to load: {error}</div>
      </div>
    )
  }
  if (!rows) {
    return (
      <div className="panel">
        <div className="panel-header">DLQ — recent failures (refreshes every 30s)</div>
        <div className="empty-state">Loading…</div>
      </div>
    )
  }
  if (rows.length === 0) {
    return (
      <div className="panel">
        <div className="panel-header">DLQ — recent failures (refreshes every 30s)</div>
        <div className="empty-state">
          No DLQ entries — pipeline healthy
        </div>
      </div>
    )
  }
  return (
    <div className="panel">
      <div className="panel-header">
        DLQ — recent failures ({rows.length}, refreshes every 30s)
      </div>
      <table className="tg-table">
        <thead>
          <tr>
            <th>Failed at</th>
            <th>Channel</th>
            <th>Msg id</th>
            <th>Error class</th>
            <th>Error</th>
            <th>Raw text</th>
            <th>Retried at</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.id}>
              <td>{fmtTime(r.failed_at)}</td>
              <td>{r.channel_handle}</td>
              <td>{r.msg_id}</td>
              <td>
                <span className="tg-badge tg-badge-warn">
                  {r.error_class}
                </span>
              </td>
              <td className="tg-text-cell">{r.error_text}</td>
              <td className="tg-text-cell">
                {r.raw_text_preview || '(empty)'}
              </td>
              <td>{r.retried_at ? fmtTime(r.retried_at) : '–'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

import React, { useState, useEffect, useCallback } from 'react'

function relTime(iso) {
  if (!iso) return 'never'
  try {
    const t = new Date(iso).getTime()
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
    if (s < 60) return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`
    return `${Math.floor(s / 86400)}d ago`
  } catch {
    return iso
  }
}

const TABLE_LABELS = {
  category_snapshots: 'Category Snapshots',
  narrative_signals: 'Narrative Signals',
  predictions: 'Predictions',
  second_wave_candidates: 'Second Wave',
  signal_events: 'Signal Events',
  active_chains: 'Active Chains',
  chain_matches: 'Chain Matches',
  chain_patterns: 'Chain Patterns',
  candidates: 'Candidates',
  alerts: 'Alerts',
  learn_logs: 'Learn Logs',
  agent_strategy: 'Agent Strategy',
}

export default function HealthTab() {
  const [health, setHealth] = useState({})
  const [status, setStatus] = useState(null)
  const [strategy, setStrategy] = useState([])
  const [learnLogs, setLearnLogs] = useState([])
  const [editingKey, setEditingKey] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [expandedLog, setExpandedLog] = useState(null)

  const fetchAll = useCallback(async () => {
    try {
      const [hRes, sRes, stRes, lRes] = await Promise.all([
        fetch('/api/system/health'),
        fetch('/api/status'),
        fetch('/api/narrative/strategy'),
        fetch('/api/narrative/learn-logs?limit=10'),
      ])
      if (hRes.ok) setHealth(await hRes.json())
      if (sRes.ok) setStatus(await sRes.json())
      if (stRes.ok) setStrategy(await stRes.json())
      if (lRes.ok) setLearnLogs(await lRes.json())
    } catch (e) {
      // ignore
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const poll = setInterval(fetchAll, 30000)
    return () => clearInterval(poll)
  }, [fetchAll])

  const saveStrategy = async (key) => {
    try {
      const res = await fetch(`/api/narrative/strategy/${key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: editValue }),
      })
      if (res.ok) {
        setEditingKey(null)
        fetchAll()
      } else {
        const err = await res.json()
        alert(`Update failed: ${err.detail || res.status}`)
      }
    } catch (e) {
      alert(`Update error: ${e.message}`)
    }
  }

  return (
    <div>
      {/* Heartbeat stats */}
      <div className="stat-bar">
        <div className="stat-card">
          <div className="label">Tokens Scanned</div>
          <div className="value">{status ? status.tokens_scanned_session : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Alerts Today</div>
          <div className="value">{status ? status.alerts_today : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">CG Calls / min</div>
          <div className="value">
            {status ? `${status.cg_calls_this_minute}/${status.cg_rate_limit}` : '-'}
          </div>
        </div>
        <div className="stat-card">
          <div className="label">MiroFish Jobs</div>
          <div className="value">
            {status ? `${status.mirofish_jobs_today}/${status.mirofish_cap}` : '-'}
          </div>
        </div>
      </div>

      {/* System health grid */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header">Database Table Health</div>
        <table className="candidates-table">
          <thead>
            <tr>
              <th>Table</th>
              <th>Row Count</th>
              <th>Last Activity</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(health).map(([table, info]) => (
              <tr key={table}>
                <td style={{ fontWeight: 600 }}>{TABLE_LABELS[table] || table}</td>
                <td>{(info && info.count) != null ? info.count.toLocaleString() : '-'}</td>
                <td style={{ color: 'var(--color-text-secondary)' }}>
                  {info && info.latest ? relTime(info.latest) : 'never'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="main-grid">
        {/* Agent Strategy */}
        <div className="panel">
          <div className="panel-header">Agent Strategy</div>
          {strategy.length === 0 ? (
            <div className="empty-state">No strategy keys yet</div>
          ) : (
            <div style={{ maxHeight: 500, overflowY: 'auto' }}>
              <table className="candidates-table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Value</th>
                    <th>Source</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {strategy.map((s) => (
                    <tr key={s.key}>
                      <td style={{ fontWeight: 600, fontFamily: 'monospace', fontSize: 11 }}>
                        {s.key}
                      </td>
                      <td>
                        {editingKey === s.key ? (
                          <input
                            type="text"
                            value={editValue}
                            onChange={(e) => setEditValue(e.target.value)}
                            style={{
                              background: 'var(--color-bar-bg)',
                              border: '1px solid var(--color-border)',
                              color: 'var(--color-text-primary)',
                              padding: '2px 6px',
                              width: 80,
                            }}
                          />
                        ) : (
                          <span>{s.value}{s.locked ? ' 🔒' : ''}</span>
                        )}
                      </td>
                      <td style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                        {s.updated_by || '-'}
                      </td>
                      <td>
                        {editingKey === s.key ? (
                          <>
                            <button
                              className="tab-btn"
                              style={{ padding: '2px 8px', fontSize: 11 }}
                              onClick={() => saveStrategy(s.key)}
                            >
                              Save
                            </button>
                            <button
                              className="tab-btn"
                              style={{ padding: '2px 8px', fontSize: 11 }}
                              onClick={() => setEditingKey(null)}
                            >
                              Cancel
                            </button>
                          </>
                        ) : (
                          <button
                            className="tab-btn"
                            style={{ padding: '2px 8px', fontSize: 11 }}
                            onClick={() => {
                              setEditingKey(s.key)
                              setEditValue(s.value || '')
                            }}
                            disabled={!!s.locked}
                          >
                            Edit
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Learn logs */}
        <div className="panel">
          <div className="panel-header">Recent Learn Logs</div>
          {learnLogs.length === 0 ? (
            <div className="empty-state">No learn logs yet</div>
          ) : (
            <div style={{ maxHeight: 500, overflowY: 'auto', padding: 12 }}>
              {learnLogs.map((log, i) => {
                const key = log.id ?? i
                const expanded = expandedLog === key
                const txt = log.reflection_text || log.message || ''
                const preview = txt.length > 140 && !expanded ? txt.slice(0, 140) + '...' : txt
                return (
                  <div
                    key={key}
                    style={{
                      borderBottom: '1px solid var(--color-border)',
                      padding: '8px 0',
                      fontSize: 12,
                    }}
                  >
                    <div style={{ color: 'var(--color-text-secondary)', marginBottom: 4 }}>
                      Cycle #{log.cycle_number ?? '?'} ({log.cycle_type ?? '-'}) — hit rate{' '}
                      {log.hit_rate_before != null ? `${log.hit_rate_before}% → ${log.hit_rate_after}%` : 'n/a'}
                    </div>
                    <div>{preview}</div>
                    {txt.length > 140 && (
                      <button
                        className="tab-btn"
                        style={{ padding: '2px 4px', fontSize: 11, marginTop: 4 }}
                        onClick={() => setExpandedLog(expanded ? null : key)}
                      >
                        {expanded ? 'Collapse' : 'Expand'}
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'

function relTime(iso) {
  if (!iso) return '-'
  try {
    const t = new Date(iso).getTime()
    const now = Date.now()
    const s = Math.max(0, Math.floor((now - t) / 1000))
    if (s < 60) return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`
    return `${Math.floor(s / 86400)}d ago`
  } catch {
    return iso
  }
}

export default function ChainsTab() {
  const [stats, setStats] = useState(null)
  const [active, setActive] = useState([])
  const [patterns, setPatterns] = useState([])
  const [events, setEvents] = useState([])

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, aRes, pRes, eRes] = await Promise.all([
        fetch('/api/chains/stats'),
        fetch('/api/chains/active'),
        fetch('/api/chains/patterns'),
        fetch('/api/chains/events/recent?limit=30'),
      ])
      if (sRes.ok) setStats(await sRes.json())
      if (aRes.ok) setActive(await aRes.json())
      if (pRes.ok) setPatterns(await pRes.json())
      if (eRes.ok) setEvents(await eRes.json())
    } catch (e) {
      // ignore
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const poll = setInterval(fetchAll, 30000)
    return () => clearInterval(poll)
  }, [fetchAll])

  return (
    <div>
      {/* Stats row */}
      <div className="stat-bar">
        <div className="stat-card">
          <div className="label">Active Chains</div>
          <div className="value">{stats ? stats.active_chains : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Completed Matches</div>
          <div className="value">{stats ? stats.completed_matches : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Events (24h)</div>
          <div className="value">{stats ? stats.events_24h : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Total Events</div>
          <div className="value">{stats ? stats.total_events : '-'}</div>
        </div>
      </div>

      {/* Active Chains */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header">Active Chains</div>
        {active.length === 0 ? (
          <div className="empty-state">No active chains in progress</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Pipeline</th>
                  <th>Pattern</th>
                  <th>Steps</th>
                  <th>Last Step</th>
                  <th>Anchor</th>
                </tr>
              </thead>
              <tbody>
                {active.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <TokenLink
                        tokenId={c.token_id}
                        symbol={c.ticker || c.token_name || undefined}
                        pipeline={c.pipeline}
                        chain={c.chain}
                        type={c.pipeline === 'narrative' ? 'category' : 'auto'}
                      />
                      {c.token_name && <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>{c.token_name}</div>}
                    </td>
                    <td>
                      <span className={`pipeline-badge pipeline-badge-${c.pipeline}`}>
                        {c.pipeline}
                      </span>
                    </td>
                    <td>{c.pattern_name}</td>
                    <td>{Array.isArray(c.steps_matched) ? c.steps_matched.length : '?'}</td>
                    <td>{relTime(c.last_step_time)}</td>
                    <td>{relTime(c.anchor_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="main-grid">
        {/* Pattern Summary */}
        <div className="panel">
          <div className="panel-header">Chain Patterns</div>
          {patterns.length === 0 ? (
            <div className="empty-state">No chain patterns defined</div>
          ) : (
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Pattern</th>
                  <th>Priority</th>
                  <th>Triggers</th>
                  <th>Hit Rate</th>
                  <th>Boost</th>
                </tr>
              </thead>
              <tbody>
                {patterns.map((p) => (
                  <tr key={p.id}>
                    <td style={{ fontWeight: 600 }}>{p.name}</td>
                    <td>
                      <span className={`outcome-badge ${
                        p.alert_priority === 'high' ? 'win' :
                        p.alert_priority === 'medium' ? '' : ''
                      }`}>
                        {p.alert_priority}
                      </span>
                    </td>
                    <td>{p.total_triggers}</td>
                    <td>
                      <span style={{ color: p.hit_rate >= 50 ? 'var(--color-accent-green)' : 'var(--color-text-secondary)' }}>
                        {p.hit_rate}%
                      </span>
                    </td>
                    <td>+{p.conviction_boost}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Recent Events */}
        <div className="panel">
          <div className="panel-header">Recent Signal Events</div>
          {events.length === 0 ? (
            <div className="empty-state">No signal events yet</div>
          ) : (
            <div style={{ maxHeight: 400, overflowY: 'auto' }}>
              <table className="candidates-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Token</th>
                    <th>Pipeline</th>
                    <th>Event</th>
                    <th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((e) => (
                    <tr key={e.id}>
                      <td>{relTime(e.created_at)}</td>
                      <td>
                        <TokenLink
                          tokenId={e.token_id}
                          symbol={e.ticker || e.token_name || undefined}
                          pipeline={e.pipeline}
                          chain={e.chain}
                          type={e.pipeline === 'narrative' ? 'category' : 'auto'}
                          maxLen={10}
                        />
                      </td>
                      <td>
                        <span className={`pipeline-badge pipeline-badge-${e.pipeline}`}>
                          {e.pipeline}
                        </span>
                      </td>
                      <td>{e.event_type}</td>
                      <td style={{ color: 'var(--color-text-secondary)', fontSize: 11 }}>
                        {e.source_module}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

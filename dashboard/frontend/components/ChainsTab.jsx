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

function formatPct(v) {
  if (v == null || v === undefined) return null
  const n = Number(v)
  if (isNaN(n)) return null
  return n.toFixed(1) + '%'
}

function pctColor(v) {
  if (v == null) return 'var(--color-text-secondary)'
  const n = Number(v)
  if (n > 10) return '#4caf50'
  if (n > 0) return '#ffc107'
  if (n < 0) return '#ef5350'
  return 'var(--color-text-secondary)'
}

function rowBg(priceChange24h) {
  if (priceChange24h == null) return undefined
  const n = Number(priceChange24h)
  if (n > 50) return 'rgba(76, 175, 80, 0.18)'
  if (n > 20) return 'rgba(76, 175, 80, 0.08)'
  if (n < -10) return 'rgba(239, 83, 80, 0.08)'
  return undefined
}

function scoreBadgeColor(score) {
  if (score >= 70) return { bg: '#1b5e20', color: '#a5d6a7' }
  if (score >= 40) return { bg: '#4a3800', color: '#ffd54f' }
  if (score > 0) return { bg: '#333', color: '#aaa' }
  return { bg: '#2a2a2a', color: '#666' }
}

function pipelineLabel(pipeline, chain) {
  if (chain === 'coingecko') {
    return { text: 'CoinGecko', cls: 'coingecko' }
  }
  if (pipeline === 'narrative') {
    return { text: 'Narrative', cls: 'narrative' }
  }
  return { text: 'DEX', cls: 'memecoin' }
}

function formatMcap(v) {
  if (!v) return '-'
  const n = Number(v)
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(1) + 'B'
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(0) + 'K'
  return '$' + n.toFixed(0)
}

export default function ChainsTab() {
  const [stats, setStats] = useState(null)
  const [active, setActive] = useState([])
  const [patterns, setPatterns] = useState([])
  const [events, setEvents] = useState([])
  const [topMovers, setTopMovers] = useState([])

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, aRes, pRes, eRes, tmRes] = await Promise.all([
        fetch('/api/chains/stats'),
        fetch('/api/chains/active'),
        fetch('/api/chains/patterns'),
        fetch('/api/chains/events/recent?limit=50'),
        fetch('/api/chains/top-movers?limit=5'),
      ])
      if (sRes.ok) setStats(await sRes.json())
      if (aRes.ok) setActive(await aRes.json())
      if (pRes.ok) setPatterns(await pRes.json())
      if (eRes.ok) setEvents(await eRes.json())
      if (tmRes.ok) setTopMovers(await tmRes.json())
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

      {/* Top Movers */}
      {topMovers.length > 0 && (
        <div className="panel" style={{ marginBottom: 16 }}>
          <div className="panel-header">Top Movers (24h)</div>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', padding: '8px 0' }}>
            {topMovers.map((m) => {
              const pl = pipelineLabel(null, m.chain)
              const pct24 = formatPct(m.price_change_24h)
              const pct1h = formatPct(m.price_change_1h)
              return (
                <div
                  key={m.token_id}
                  style={{
                    background: 'var(--color-surface)',
                    border: '1px solid var(--color-border)',
                    borderRadius: 8,
                    padding: '10px 14px',
                    minWidth: 160,
                    flex: '1 1 160px',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                    <TokenLink
                      tokenId={m.token_id}
                      symbol={m.ticker || m.token_name || undefined}
                      chain={m.chain}
                      type={m.chain === 'coingecko' ? 'coin' : 'auto'}
                    />
                    <span
                      className={`pipeline-badge pipeline-badge-${pl.cls}`}
                      style={{ fontSize: 10, padding: '1px 6px' }}
                    >
                      {pl.text}
                    </span>
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', marginBottom: 4 }}>
                    {m.token_name}
                  </div>
                  <div style={{ display: 'flex', gap: 12, fontSize: 12 }}>
                    {pct24 && (
                      <span style={{ color: pctColor(m.price_change_24h), fontWeight: 700 }}>
                        24h: {m.price_change_24h > 0 ? '+' : ''}{pct24}
                      </span>
                    )}
                    {pct1h && (
                      <span style={{ color: pctColor(m.price_change_1h), fontWeight: 600 }}>
                        1h: {m.price_change_1h > 0 ? '+' : ''}{pct1h}
                      </span>
                    )}
                  </div>
                  <div style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--color-text-secondary)', marginTop: 4 }}>
                    <span>MCap: {formatMcap(m.market_cap_usd)}</span>
                    {m.quant_score > 0 && (
                      <span>Score: <strong style={{ color: m.quant_score >= 60 ? '#4caf50' : '#ffc107' }}>{m.quant_score}</strong></span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Recent Signal Events — most important, shown first */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header">Recent Signals</div>
        {events.length === 0 ? (
          <div className="empty-state">No signal events yet</div>
        ) : (
          <div style={{ maxHeight: 500, overflowY: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Token</th>
                  <th>24h Change</th>
                  <th>Score</th>
                  <th>Event</th>
                  <th>Pipeline</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => {
                  const pct24 = e.ed_price_change_24h
                  const score = e.ed_quant_score || 0
                  const pl = pipelineLabel(e.pipeline, e.chain)
                  const bg = rowBg(pct24)

                  return (
                    <tr key={e.id} style={bg ? { background: bg } : undefined}>
                      <td style={{ whiteSpace: 'nowrap', fontSize: 11 }}>
                        {relTime(e.created_at)}
                      </td>
                      <td>
                        <TokenLink
                          tokenId={e.token_id}
                          symbol={e.ticker || e.token_name || undefined}
                          pipeline={e.pipeline}
                          chain={e.chain}
                          type={e.pipeline === 'narrative' ? 'category' : 'auto'}
                          maxLen={10}
                        />
                        {e.token_name && (
                          <div style={{ fontSize: 10, color: 'var(--color-text-secondary)' }}>
                            {e.token_name}
                          </div>
                        )}
                      </td>
                      <td style={{ fontWeight: 600 }}>
                        {pct24 != null ? (
                          <span style={{ color: pctColor(pct24) }}>
                            {pct24 > 0 ? '+' : ''}{formatPct(pct24)}
                          </span>
                        ) : (
                          <span style={{ color: '#555', fontSize: 11 }}>--</span>
                        )}
                      </td>
                      <td>
                        {score > 0 ? (
                          <span style={{
                            display: 'inline-block',
                            padding: '1px 6px',
                            borderRadius: 4,
                            fontSize: 11,
                            fontWeight: 700,
                            background: scoreBadgeColor(score).bg,
                            color: scoreBadgeColor(score).color,
                          }}>
                            {score}
                          </span>
                        ) : (
                          <span style={{ color: '#666', fontSize: 11 }}>0</span>
                        )}
                      </td>
                      <td>
                        <span style={{
                          display: 'inline-block',
                          padding: '1px 6px',
                          borderRadius: 4,
                          fontSize: 10,
                          background: '#2a2a2a',
                          color: '#aaa',
                        }}>
                          {e.event_type}
                        </span>
                      </td>
                      <td>
                        <span className={`pipeline-badge pipeline-badge-${pl.cls}`}>
                          {pl.text}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
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
                  <th>MCap</th>
                  <th>Score</th>
                  <th>Last Step</th>
                  <th>Anchor</th>
                </tr>
              </thead>
              <tbody>
                {active.map((c) => {
                  const pl = pipelineLabel(c.pipeline, c.chain)
                  return (
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
                        <span className={`pipeline-badge pipeline-badge-${pl.cls}`}>
                          {pl.text}
                        </span>
                      </td>
                      <td>{c.pattern_name}</td>
                      <td>{Array.isArray(c.steps_matched) ? c.steps_matched.length : '?'}</td>
                      <td style={{ fontSize: 11 }}>{formatMcap(c.market_cap_usd)}</td>
                      <td>
                        {c.quant_score > 0 ? (
                          <span style={{
                            display: 'inline-block',
                            padding: '1px 6px',
                            borderRadius: 4,
                            fontSize: 11,
                            fontWeight: 700,
                            background: scoreBadgeColor(c.quant_score).bg,
                            color: scoreBadgeColor(c.quant_score).color,
                          }}>
                            {c.quant_score}
                          </span>
                        ) : (
                          <span style={{ color: '#666', fontSize: 11 }}>-</span>
                        )}
                      </td>
                      <td>{relTime(c.last_step_time)}</td>
                      <td>{relTime(c.anchor_time)}</td>
                    </tr>
                  )
                })}
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

        {/* Events moved above — this slot intentionally left empty */}
      </div>
    </div>
  )
}

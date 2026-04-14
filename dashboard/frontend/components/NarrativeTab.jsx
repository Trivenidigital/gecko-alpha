import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'

function fmtNum(n) {
  if (n == null) return '-'
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B'
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return Number(n).toFixed(1)
}

function fmtPct(n) {
  if (n == null) return '-'
  return Number(n).toFixed(1) + '%'
}

function outcomeClass(outcome) {
  if (!outcome) return ''
  if (outcome === 'HIT') return 'win'
  if (outcome === 'MISS') return 'loss'
  return ''
}

function regimeClass(regime) {
  if (!regime) return ''
  if (regime === 'HEATING') return 'win'
  if (regime === 'COOLING') return 'loss'
  return ''
}

export default function NarrativeTab() {
  const [metrics, setMetrics] = useState(null)
  const [heating, setHeating] = useState([])
  const [predictions, setPredictions] = useState([])
  const [expandedPred, setExpandedPred] = useState(null)

  const fetchData = useCallback(async () => {
    try {
      const [mRes, hRes, pRes] = await Promise.all([
        fetch('/api/narrative/metrics'),
        fetch('/api/narrative/heating'),
        fetch('/api/narrative/predictions?limit=50'),
      ])
      if (mRes.ok) setMetrics(await mRes.json())
      if (hRes.ok) setHeating(await hRes.json())
      if (pRes.ok) setPredictions(await pRes.json())
    } catch (e) {
      // API not available yet
    }
  }, [])

  useEffect(() => {
    fetchData()
    const poll = setInterval(fetchData, 60000)
    return () => clearInterval(poll)
  }, [fetchData])

  return (
    <div>
      {/* Metric cards */}
      <div className="stat-bar">
        <div className="stat-card">
          <div className="label">Hit Rate</div>
          <div className="value">{metrics ? fmtPct(metrics.agent_hit_rate) : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">True Alpha</div>
          <div className={`value ${metrics && metrics.true_alpha > 0 ? '' : 'warning'}`}>
            {metrics ? fmtPct(metrics.true_alpha) : '-'}
          </div>
        </div>
        <div className="stat-card">
          <div className="label">Active Predictions</div>
          <div className="value">{metrics ? metrics.active_predictions : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Total Predictions</div>
          <div className="value">{metrics ? metrics.total_predictions : '-'}</div>
        </div>
      </div>

      <div className="main-grid">
        {/* Heating categories table */}
        <div className="panel">
          <div className="panel-header">Heating Categories</div>
          {heating.length === 0 ? (
            <div className="empty-state">No category data yet</div>
          ) : (
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Category</th>
                  <th>MCap Change 24h</th>
                  <th>Volume 24h</th>
                  <th>Regime</th>
                </tr>
              </thead>
              <tbody>
                {heating.map((c, i) => (
                  <tr key={c.category_id || i}>
                    <td style={{ fontWeight: 600 }}>
                      <TokenLink tokenId={c.category_id} symbol={c.name || c.category_id} type="category" pipeline="narrative" />
                    </td>
                    <td>
                      <span style={{ color: c.market_cap_change_24h > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red)' }}>
                        {fmtPct(c.market_cap_change_24h)}
                      </span>
                    </td>
                    <td>{fmtNum(c.volume_24h)}</td>
                    <td>
                      <span className={`outcome-badge ${regimeClass(c.market_regime)}`}>
                        {c.market_regime || '-'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Predictions table */}
        <div className="panel">
          <div className="panel-header">Predictions</div>
          {predictions.length === 0 ? (
            <div className="empty-state">No predictions yet</div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="candidates-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Category</th>
                    <th>Fit</th>
                    <th>Conf</th>
                    <th>Counter</th>
                    <th>Flags</th>
                    <th>Watch</th>
                    <th>Regime</th>
                    <th>6h</th>
                    <th>24h</th>
                    <th>48h</th>
                    <th>Outcome</th>
                    <th>Peak</th>
                  </tr>
                </thead>
                <tbody>
                  {predictions.map((p, i) => {
                    let flags = []
                    try {
                      if (typeof p.counter_flags === 'string') {
                        flags = JSON.parse(p.counter_flags)
                      } else if (Array.isArray(p.counter_flags)) {
                        flags = p.counter_flags
                      }
                    } catch { /* ignore */ }
                    const counter = p.counter_risk_score
                    const counterColor =
                      counter == null ? 'var(--color-text-secondary)'
                      : counter < 30 ? 'var(--color-accent-green)'
                      : counter <= 60 ? 'var(--color-accent-amber)'
                      : 'var(--color-accent-red)'
                    const expanded = expandedPred === (p.id || i)
                    return (
                      <React.Fragment key={p.id || i}>
                        <tr>
                          <td
                            style={{ cursor: 'pointer' }}
                            onClick={() => setExpandedPred(expanded ? null : (p.id || i))}
                            title="Click to toggle details"
                          >
                            {expanded ? '▼ ' : '▶ '}
                            <TokenLink tokenId={p.coin_id} symbol={p.symbol} pipeline="narrative" />
                          </td>
                          <td>
                            <TokenLink tokenId={p.category_id} symbol={p.category_name || p.category_id} type="category" pipeline="narrative" />
                          </td>
                          <td>{p.narrative_fit_score != null ? Number(p.narrative_fit_score).toFixed(0) : (p.fit_score != null ? Number(p.fit_score).toFixed(0) : '-')}</td>
                          <td>{p.confidence != null ? (typeof p.confidence === 'string' ? p.confidence : Number(p.confidence).toFixed(0)) : '-'}</td>
                          <td>
                            <span style={{ color: counterColor, fontWeight: 600 }}>
                              {counter != null ? `${counter}/100` : '-'}
                            </span>
                          </td>
                          <td>
                            {flags.slice(0, 3).map((f, idx) => (
                              <span key={idx} className="signal-badge fired">{typeof f === 'object' ? f.flag : f}</span>
                            ))}
                            {flags.length > 3 && (
                              <span className="signal-badge">+{flags.length - 3}</span>
                            )}
                          </td>
                          <td>{p.watchlist_users != null ? p.watchlist_users : '-'}</td>
                          <td>
                            <span className={`outcome-badge ${regimeClass(p.market_regime)}`}>
                              {p.market_regime || '-'}
                            </span>
                          </td>
                          <td>{fmtPct(p.outcome_6h_change_pct || p.price_change_6h)}</td>
                          <td>{fmtPct(p.outcome_24h_change_pct || p.price_change_24h)}</td>
                          <td>{fmtPct(p.outcome_48h_change_pct || p.price_change_48h)}</td>
                          <td>
                            <span className={`outcome-badge ${outcomeClass(p.outcome_class)}`}>
                              {p.outcome_class || 'PENDING'}
                            </span>
                          </td>
                          <td>{fmtPct(p.peak_change_pct)}</td>
                        </tr>
                        {expanded && (
                          <tr>
                            <td colSpan={13} style={{ background: 'var(--color-bar-bg)', padding: 12, fontSize: 12 }}>
                              <div style={{ marginBottom: 6 }}>
                                <strong>Reasoning:</strong> {p.reasoning || '-'}
                              </div>
                              {p.counter_argument && (
                                <div style={{ marginBottom: 6 }}>
                                  <strong>Counter:</strong> {p.counter_argument}
                                </div>
                              )}
                              {p.outcome_reason && (
                                <div>
                                  <strong>Outcome reason:</strong> {p.outcome_reason}
                                </div>
                              )}
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

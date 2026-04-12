import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'

function fmtNum(n) {
  if (n == null) return '-'
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B'
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return Number(n).toFixed(2)
}

function fmtPct(n) {
  if (n == null) return '-'
  return Number(n).toFixed(1) + '%'
}

function relTime(iso) {
  if (!iso) return '-'
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

export default function SecondWaveTab() {
  const [stats, setStats] = useState(null)
  const [candidates, setCandidates] = useState([])

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, cRes] = await Promise.all([
        fetch('/api/secondwave/stats'),
        fetch('/api/secondwave/candidates?days=7&limit=50'),
      ])
      if (sRes.ok) setStats(await sRes.json())
      if (cRes.ok) setCandidates(await cRes.json())
    } catch (e) {
      // ignore
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const poll = setInterval(fetchAll, 30000)
    return () => clearInterval(poll)
  }, [fetchAll])

  const alertsSent = candidates.filter((c) => c.alerted_at).length

  return (
    <div>
      <div className="stat-bar">
        <div className="stat-card">
          <div className="label">Detected (7d)</div>
          <div className="value">{stats ? stats.count : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Avg Score</div>
          <div className="value">{stats ? stats.avg_score : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Alerts Sent</div>
          <div className="value">{alertsSent}</div>
        </div>
        <div className="stat-card">
          <div className="label">Window</div>
          <div className="value">{stats ? `${stats.days}d` : '-'}</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">Second-Wave Candidates</div>
        {candidates.length === 0 ? (
          <div className="empty-state">
            No second-wave candidates yet. The detector runs every 30 min.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Chain</th>
                  <th>Days Since Seen</th>
                  <th>Reaccum Score</th>
                  <th>Signals</th>
                  <th>Alert MCap</th>
                  <th>Current MCap</th>
                  <th>vs Alert</th>
                  <th>Detected</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <TokenLink tokenId={c.contract_address} symbol={c.ticker || c.token_name} pipeline="memecoin" chain={c.chain} />
                    </td>
                    <td>
                      <span className={`chain-badge ${c.chain}`}>{c.chain}</span>
                    </td>
                    <td>{c.days_since_first_seen != null ? Number(c.days_since_first_seen).toFixed(1) : '-'}</td>
                    <td>
                      <span className="conviction-badge high">
                        {c.reaccumulation_score}
                      </span>
                    </td>
                    <td>
                      {Array.isArray(c.reaccumulation_signals) && c.reaccumulation_signals.slice(0, 3).map((s, i) => (
                        <span key={i} className="signal-badge fired">{s}</span>
                      ))}
                      {Array.isArray(c.reaccumulation_signals) && c.reaccumulation_signals.length > 3 && (
                        <span className="signal-badge">+{c.reaccumulation_signals.length - 3}</span>
                      )}
                    </td>
                    <td>${fmtNum(c.alert_market_cap)}</td>
                    <td>${fmtNum(c.current_market_cap)}</td>
                    <td>
                      <span style={{
                        color: (c.price_vs_alert_pct || 0) > 0
                          ? 'var(--color-accent-green)'
                          : 'var(--color-accent-red)'
                      }}>
                        {fmtPct(c.price_vs_alert_pct)}
                      </span>
                    </td>
                    <td>{relTime(c.detected_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

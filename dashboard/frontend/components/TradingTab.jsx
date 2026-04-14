import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'

function fmtUsd(n) {
  if (n == null) return '-'
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (abs >= 1e6) return sign + '$' + (abs / 1e6).toFixed(1) + 'M'
  if (abs >= 1e3) return sign + '$' + (abs / 1e3).toFixed(1) + 'K'
  return sign + '$' + abs.toFixed(2)
}

function fmtPct(n) {
  if (n == null) return '-'
  return Number(n).toFixed(2) + '%'
}

function fmtDate(iso) {
  if (!iso) return '-'
  try {
    const d = new Date(iso)
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) +
      ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
  } catch {
    return iso
  }
}

function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return '-'
  try {
    const ms = new Date(endIso) - new Date(startIso)
    const mins = Math.floor(ms / 60000)
    if (mins < 60) return mins + 'm'
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return hrs + 'h ' + (mins % 60) + 'm'
    const days = Math.floor(hrs / 24)
    return days + 'd ' + (hrs % 24) + 'h'
  } catch {
    return '-'
  }
}

function pnlColor(val) {
  if (val == null || val === 0) return 'var(--color-text-primary)'
  return val > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)'
}

function reasonBadge(reason) {
  if (!reason) return <span className="outcome-badge">-</span>
  const r = reason.toUpperCase()
  if (r === 'TP' || r === 'TAKE_PROFIT') return <span className="outcome-badge win">TP</span>
  if (r === 'SL' || r === 'STOP_LOSS') return <span className="outcome-badge loss">SL</span>
  if (r === 'EXPIRED' || r === 'TIMEOUT') return <span className="outcome-badge" style={{ background: 'var(--color-bar-bg)', color: 'var(--color-text-secondary)' }}>Expired</span>
  if (r === 'MANUAL') return <span className="outcome-badge" style={{ background: 'rgba(255, 183, 77, 0.15)', color: 'var(--color-accent-amber)' }}>Manual</span>
  return <span className="outcome-badge">{reason}</span>
}

export default function TradingTab() {
  const [stats, setStats] = useState(null)
  const [bySignal, setBySignal] = useState([])
  const [positions, setPositions] = useState([])
  const [history, setHistory] = useState([])

  const fetchAll = useCallback(async () => {
    try {
      const [statsRes, sigRes, posRes, histRes] = await Promise.all([
        fetch('/api/trading/stats'),
        fetch('/api/trading/stats/by-signal'),
        fetch('/api/trading/positions'),
        fetch('/api/trading/history?limit=20'),
      ])
      if (statsRes.ok) setStats(await statsRes.json())
      if (sigRes.ok) setBySignal(await sigRes.json())
      if (posRes.ok) setPositions(await posRes.json())
      if (histRes.ok) setHistory(await histRes.json())
    } catch {
      // API not available yet
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const poll = setInterval(fetchAll, 30000)
    return () => clearInterval(poll)
  }, [fetchAll])

  const totalPnl = stats?.total_pnl ?? 0
  const winRate = stats?.win_rate_pct ?? 0
  const openCount = positions.length
  const totalExposure = positions.reduce((sum, p) => sum + (p.amount ?? 0) * (p.entry_price ?? 0), 0)
  const totalTrades = stats?.total_trades ?? 0

  return (
    <div>
      {/* Section 1: Stats Cards */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: 12,
        marginBottom: 16,
      }}>
        <div className="panel" style={{ padding: '16px 20px', textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Total PnL</div>
          <div style={{ fontSize: 28, fontWeight: 700, color: pnlColor(totalPnl) }}>
            {fmtUsd(totalPnl)}
          </div>
        </div>
        <div className="panel" style={{ padding: '16px 20px', textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Win Rate</div>
          <div style={{ fontSize: 28, fontWeight: 700, color: winRate >= 50 ? 'var(--color-accent-green)' : 'var(--color-accent-amber)' }}>
            {Number(winRate).toFixed(1)}%
          </div>
        </div>
        <div className="panel" style={{ padding: '16px 20px', textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Open Positions</div>
          <div style={{ fontSize: 28, fontWeight: 700 }}>{openCount}</div>
          <div style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtUsd(totalExposure)} exposure</div>
        </div>
        <div className="panel" style={{ padding: '16px 20px', textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Total Trades</div>
          <div style={{ fontSize: 28, fontWeight: 700 }}>{totalTrades}</div>
        </div>
      </div>

      {/* Section 2: PnL by Signal Type */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            PnL by Signal Type
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Which signals make money?
          </span>
        </div>
        {bySignal.length === 0 ? (
          <div className="empty-state">No signal data yet. Trades will appear after the first paper trade closes.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Signal Type</th>
                  <th>Trades</th>
                  <th>Wins</th>
                  <th>PnL ($)</th>
                  <th>Win Rate</th>
                  <th>Avg PnL %</th>
                </tr>
              </thead>
              <tbody>
                {bySignal.map((s, i) => {
                  const pnl = s.total_pnl ?? s.pnl ?? 0
                  const wr = s.win_rate_pct ?? (s.trades > 0 ? ((s.wins / s.trades) * 100) : 0)
                  const rowBg = pnl > 0
                    ? 'rgba(76, 175, 80, 0.07)'
                    : pnl < 0
                      ? 'rgba(239, 83, 80, 0.07)'
                      : 'transparent'
                  return (
                    <tr key={s.signal_type || i} style={{ background: rowBg }}>
                      <td style={{ fontWeight: 600 }}>{s.signal_type || '-'}</td>
                      <td>{s.trades ?? s.total_trades ?? 0}</td>
                      <td>{s.wins ?? 0}</td>
                      <td style={{ fontWeight: 700, color: pnlColor(pnl) }}>{fmtUsd(pnl)}</td>
                      <td>{Number(wr).toFixed(1)}%</td>
                      <td style={{ color: pnlColor(s.avg_pnl_pct) }}>{fmtPct(s.avg_pnl_pct)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Section 3: Open Positions */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Open Positions
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            {openCount} active paper trade{openCount !== 1 ? 's' : ''}
          </span>
        </div>
        {positions.length === 0 ? (
          <div className="empty-state">No open positions.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Signal</th>
                  <th>Entry Price</th>
                  <th>Amount</th>
                  <th>TP / SL</th>
                  <th>Opened</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={p.id || i}>
                    <td>
                      <TokenLink
                        tokenId={p.coin_id || p.token_id}
                        symbol={p.symbol || p.name}
                        chain="coingecko"
                      />
                    </td>
                    <td style={{ fontSize: 12 }}>{p.signal_type || '-'}</td>
                    <td>{p.entry_price != null ? '$' + Number(p.entry_price).toFixed(4) : '-'}</td>
                    <td>{p.amount != null ? Number(p.amount).toFixed(2) : '-'}</td>
                    <td style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                      <div>TP: {p.take_profit != null ? '$' + Number(p.take_profit).toFixed(4) : '-'}</div>
                      <div>SL: {p.stop_loss != null ? '$' + Number(p.stop_loss).toFixed(4) : '-'}</div>
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtDate(p.opened_at || p.created_at)}</td>
                    <td>
                      <span className="outcome-badge" style={{ background: 'rgba(33, 150, 243, 0.15)', color: '#42a5f5' }}>
                        {p.status || 'OPEN'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Section 4: Recent Closed Trades */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Recent Closed Trades
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Last 20 completed trades
          </span>
        </div>
        {history.length === 0 ? (
          <div className="empty-state">No closed trades yet.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Signal</th>
                  <th>Entry / Exit</th>
                  <th>PnL ($)</th>
                  <th>PnL %</th>
                  <th>Reason</th>
                  <th>Duration</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h, i) => {
                  const pnl = h.pnl ?? h.realized_pnl ?? 0
                  const pnlPct = h.pnl_pct ?? h.realized_pnl_pct ?? null
                  return (
                    <tr key={h.id || i}>
                      <td>
                        <TokenLink
                          tokenId={h.coin_id || h.token_id}
                          symbol={h.symbol || h.name}
                          chain="coingecko"
                        />
                      </td>
                      <td style={{ fontSize: 12 }}>{h.signal_type || '-'}</td>
                      <td style={{ fontSize: 12 }}>
                        ${Number(h.entry_price || 0).toFixed(4)} {'  '}
                        <span style={{ color: 'var(--color-text-secondary)' }}>{' -> '}</span>
                        {' '}${Number(h.exit_price || 0).toFixed(4)}
                      </td>
                      <td style={{ fontWeight: 700, color: pnlColor(pnl) }}>{fmtUsd(pnl)}</td>
                      <td style={{ color: pnlColor(pnlPct) }}>{fmtPct(pnlPct)}</td>
                      <td>{reasonBadge(h.close_reason || h.reason)}</td>
                      <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                        {fmtDuration(h.opened_at || h.created_at, h.closed_at)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

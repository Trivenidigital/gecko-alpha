import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import TokenLink from './TokenLink'
import { useSort, SortHeader as SharedSortHeader } from './useSort.jsx'

const CLOSED_PER_PAGE = 20  // closed-trades pagination size

function _readStoredPage() {
  try {
    const v = sessionStorage.getItem('gecko.closedPage')
    const n = v == null ? 0 : parseInt(v, 10)
    return Number.isFinite(n) && n >= 0 ? n : 0
  } catch { return 0 }
}

function fmtUsd(n) {
  if (n == null) return '-'
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (abs >= 1e6) return sign + '$' + (abs / 1e6).toFixed(1) + 'M'
  if (abs >= 1e3) return sign + '$' + (abs / 1e3).toFixed(1) + 'K'
  return sign + '$' + abs.toFixed(2)
}

function fmtPrice(v) {
  if (v == null) return '-'
  const n = Number(v)
  if (isNaN(n)) return '-'
  if (n === 0) return '$0'
  if (n >= 1) return '$' + n.toFixed(2)
  if (n >= 0.01) return '$' + n.toFixed(4)
  if (n >= 0.0001) return '$' + n.toFixed(6)
  return '$' + n.toFixed(8)
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

function fmtRelative(iso) {
  if (!iso) return '-'
  try {
    const ms = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(ms / 60000)
    if (mins < 1) return 'just now'
    if (mins < 60) return mins + 'm ago'
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return hrs + 'h ago'
    const days = Math.floor(hrs / 24)
    return days + 'd ago'
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

function getCategory(p) {
  try {
    const sd = typeof p.signal_data === 'string' ? JSON.parse(p.signal_data) : p.signal_data
    return sd?.category || p.signal_type || '-'
  } catch {
    return p.signal_type || '-'
  }
}

function getTokenLabel(p) {
  if (p.symbol && p.symbol.trim()) return p.symbol.toUpperCase()
  if (p.name && p.name.trim()) return p.name
  return p.token_id || '-'
}

function reasonBadge(reason) {
  if (!reason) return <span className="outcome-badge">-</span>
  const r = reason.toUpperCase()
  if (r === 'TP' || r === 'TAKE_PROFIT') return <span className="outcome-badge win">TP</span>
  if (r === 'SL' || r === 'STOP_LOSS') return <span className="outcome-badge loss">SL</span>
  if (r === 'EXPIRED' || r === 'TIMEOUT') return <span className="outcome-badge" style={{ background: 'var(--color-bar-bg)', color: 'var(--color-text-secondary)' }}>Expired</span>
  if (r === 'PEAK_FADE') return <span className="outcome-badge" style={{ background: 'rgba(255, 183, 77, 0.15)', color: 'var(--color-accent-amber)' }}>Peak Fade</span>
  if (r === 'MANUAL') return <span className="outcome-badge" style={{ background: 'rgba(255, 183, 77, 0.15)', color: 'var(--color-accent-amber)' }}>Manual</span>
  return <span className="outcome-badge">{reason}</span>
}

function checkpointBadges(p) {
  const checks = [
    { label: '1h', val: p.checkpoint_1h_pct },
    { label: '6h', val: p.checkpoint_6h_pct },
    { label: '24h', val: p.checkpoint_24h_pct },
    { label: '48h', val: p.checkpoint_48h_pct },
  ].filter(c => c.val != null)
  if (checks.length === 0) return <span style={{ color: 'var(--color-text-secondary)', fontSize: 11 }}>-</span>
  return (
    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
      {checks.map(c => (
        <span key={c.label} style={{
          fontSize: 10,
          padding: '1px 5px',
          borderRadius: 3,
          background: c.val > 0 ? 'rgba(76, 175, 80, 0.12)' : 'rgba(239, 83, 80, 0.12)',
          color: c.val > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)',
          fontWeight: 600,
          whiteSpace: 'nowrap',
        }}>
          {c.label}: {c.val > 0 ? '+' : ''}{Number(c.val).toFixed(1)}%
        </span>
      ))}
    </div>
  )
}

export default function TradingTab() {
  const [stats, setStats] = useState(null)
  const [bySignal, setBySignal] = useState([])
  const [positions, setPositions] = useState([])
  const [history, setHistory] = useState([])
  const [closedPage, setClosedPageState] = useState(_readStoredPage)
  const [closedTotal, setClosedTotal] = useState(0)
  const [sortCol, setSortCol] = useState('pnl_pct')
  const [sortDir, setSortDir] = useState('desc')
  const [closingId, setClosingId] = useState(null)

  // R2-I1 fold: persist page to sessionStorage so tab-switch unmount
  // (App.jsx conditional render) doesn't reset operator's position.
  const setClosedPage = useCallback((v) => {
    setClosedPageState(prev => {
      const next = typeof v === 'function' ? v(prev) : v
      try { sessionStorage.setItem('gecko.closedPage', String(next)) } catch {}
      return next
    })
  }, [])

  // R1-I1 fold: AbortController guard against stale-page fetches
  // overwriting current-page response when page-change races polling.
  const abortRef = useRef(null)

  const fetchAll = useCallback(async () => {
    if (abortRef.current) abortRef.current.abort()
    const ac = new AbortController()
    abortRef.current = ac
    const signal = ac.signal
    try {
      const offset = closedPage * CLOSED_PER_PAGE
      const [statsRes, sigRes, posRes, histRes, countRes] = await Promise.all([
        fetch('/api/trading/stats', { signal }),
        fetch('/api/trading/stats/by-signal', { signal }),
        fetch('/api/trading/positions', { signal }),
        fetch(`/api/trading/history?limit=${CLOSED_PER_PAGE}&offset=${offset}`, { signal }),
        fetch('/api/trading/history/count', { signal }),
      ])
      // R1-I1 timeline-race guard: catches "5 fetches resolved cleanly +
      // a subsequent fetchAll already called ac.abort() before we wrote
      // state". The Promise.all itself doesn't reject in this case.
      if (signal.aborted) return
      if (statsRes.ok) setStats(await statsRes.json())
      if (sigRes.ok) {
        const sig = await sigRes.json()
        setBySignal(Array.isArray(sig) ? sig : Object.entries(sig).map(([k, v]) => ({ signal_type: k, ...v })))
      }
      if (posRes.ok) setPositions(await posRes.json())
      if (histRes.ok) setHistory(await histRes.json())
      if (countRes.ok) {
        const { total } = await countRes.json()
        setClosedTotal(total ?? 0)
      }
    } catch (e) {
      if (e?.name === 'AbortError') return  // expected on page-change race
      // API not available yet
    }
  }, [closedPage])

  // R2-I2 fold: decouple polling timer from page change so rapid
  // pagination doesn't starve the 30s polling refresh of stats /
  // positions / by-signal.
  const fetchAllRef = useRef(fetchAll)
  useEffect(() => { fetchAllRef.current = fetchAll }, [fetchAll])

  // Effect 1: immediate refetch when closedPage changes.
  useEffect(() => { fetchAll() }, [fetchAll])

  // Effect 2: 30s polling — runs once at mount, never resets.
  useEffect(() => {
    const poll = setInterval(() => fetchAllRef.current(), 30000)
    return () => clearInterval(poll)
  }, [])

  // R1-I2 + R2-I2 fold: auto-clamp on closedTotal decrease.
  useEffect(() => {
    if (closedTotal > 0 && closedPage * CLOSED_PER_PAGE >= closedTotal) {
      const lastPage = Math.max(0, Math.ceil(closedTotal / CLOSED_PER_PAGE) - 1)
      setClosedPage(lastPage)
    }
  }, [closedTotal, closedPage, setClosedPage])

  function handleSort(col) {
    if (sortCol === col) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortCol(col)
      setSortDir('desc')
    }
  }

  const sortedPositions = [...positions].sort((a, b) => {
    let va, vb
    switch (sortCol) {
      case 'token': va = (a.symbol || a.token_id || '').toLowerCase(); vb = (b.symbol || b.token_id || '').toLowerCase(); break
      case 'category': va = getCategory(a).toLowerCase(); vb = getCategory(b).toLowerCase(); break
      case 'entry': va = a.entry_price || 0; vb = b.entry_price || 0; break
      case 'amount': va = a.amount_usd || 0; vb = b.amount_usd || 0; break
      case 'current': va = a.current_price || 0; vb = b.current_price || 0; break
      case 'pnl_usd': va = a.total_pnl_usd ?? 0; vb = b.total_pnl_usd ?? 0; break
      case 'pnl_pct': va = a.total_pnl_pct ?? 0; vb = b.total_pnl_pct ?? 0; break
      case 'opened': va = a.opened_at || ''; vb = b.opened_at || ''; break
      default: va = 0; vb = 0
    }
    if (typeof va === 'string') {
      return sortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va)
    }
    return sortDir === 'asc' ? va - vb : vb - va
  })

  function SortHeader({ col, label }) {
    const active = sortCol === col
    return (
      <th
        style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' }}
        onClick={() => handleSort(col)}
      >
        {label} {active ? (sortDir === 'asc' ? '▲' : '▼') : ''}
      </th>
    )
  }

  // Rank map: persistent P&L rank regardless of current sort order. Uses
  // total_pnl_pct (realized + unrealized vs original capital) so the
  // leaderboard reflects actual trader return, not raw price move on a
  // partially-filled ladder trade.
  const pnlRankMap = useMemo(() => {
    const byPnl = [...positions].sort(
      (a, b) => (b.total_pnl_pct ?? -Infinity) - (a.total_pnl_pct ?? -Infinity)
    )
    const m = new Map()
    byPnl.forEach((p, idx) => m.set(p.id, idx + 1))
    return m
  }, [positions])

  // Enrich closed trades with computed sort keys
  const enrichedHistory = React.useMemo(() => history.map(h => ({
    ...h,
    _pnl: h.pnl_usd ?? h.pnl ?? h.realized_pnl ?? 0,
    _pnl_pct: h.pnl_pct ?? h.realized_pnl_pct ?? null,
    _category: getCategory(h),
    _token: (h.symbol || h.name || h.token_id || '').toLowerCase(),
  })), [history])

  const closedSort = useSort(enrichedHistory, 'closed_at', 'desc')

  const totalPnl = stats?.total_pnl_usd ?? stats?.total_pnl ?? 0
  const winRate = stats?.win_rate_pct ?? 0
  const openCount = positions.length
  const totalExposure = positions.reduce((sum, p) => sum + (p.amount_usd ?? 0), 0)
  // Sum of total_pnl_usd across open trades (realized-on-closed-legs +
  // unrealized-on-remainder). Reconciles with the per-row PnL$ column so
  // numbers across the page tell the same story.
  const totalOpenPnl = positions.reduce((sum, p) => sum + (p.total_pnl_usd ?? 0), 0)
  const totalTrades = stats?.total_trades ?? 0

  return (
    <div>
      {/* Section 1: Stats Cards */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
        gap: 12,
        marginBottom: 16,
      }}>
        <div className="panel" style={{ padding: '16px 20px', textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Realized PnL</div>
          <div style={{ fontSize: 28, fontWeight: 700, color: pnlColor(totalPnl) }}>
            {fmtUsd(totalPnl)}
          </div>
        </div>
        <div className="panel" style={{ padding: '16px 20px', textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Open PnL</div>
          <div style={{ fontSize: 28, fontWeight: 700, color: pnlColor(totalOpenPnl) }}>
            {fmtUsd(totalOpenPnl)}
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
                  const wr = s.win_rate_pct ?? s.win_rate ?? (s.trades > 0 ? ((s.wins / s.trades) * 100) : 0)
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
          {positions.length > 0 && (
            <div className="summary-line" style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
              {positions.length} active
            </div>
          )}
        </div>
        {positions.length === 0 ? (
          <div className="empty-state">No open positions.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <SortHeader col="pnl_pct" label="Rank" />
                  <SortHeader col="token" label="Token" />
                  <SortHeader col="category" label="Category" />
                  <SortHeader col="entry" label="Entry" />
                  <SortHeader col="amount" label="Amount" />
                  <SortHeader col="current" label="Current" />
                  <SortHeader col="pnl_usd" label="PnL $" />
                  <SortHeader col="pnl_pct" label="PnL %" />
                  <th>TP / SL</th>
                  <th>Legs</th>
                  <th>Checkpoints</th>
                  <SortHeader col="opened" label="Opened" />
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {sortedPositions.map((p, i) => {
                  // Total PnL = realized (closed ladder legs) + unrealized
                  // (remainder at current price), reconciled against the
                  // original amount_usd so the $ and % columns tell one
                  // coherent story even after partial fills.
                  const pnlUsd = p.total_pnl_usd
                  const pnlPct = p.total_pnl_pct
                  return (
                    <tr key={p.id || i}>
                      <td className="rank-cell" style={{ whiteSpace: 'nowrap', textAlign: 'center', fontWeight: 600, fontSize: 13 }}>
                        {p.total_pnl_pct == null ? '—' : (pnlRankMap.get(p.id) ?? '—')}
                      </td>
                      <td>
                        <TokenLink
                          tokenId={p.coin_id || p.token_id}
                          symbol={getTokenLabel(p)}
                          chain="coingecko"
                        />
                      </td>
                      <td style={{ fontSize: 11, maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {getCategory(p)}
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>{fmtPrice(p.entry_price)}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>{fmtUsd(p.amount_usd)}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        {p.current_price != null
                          ? fmtPrice(p.current_price)
                          : <span style={{ color: 'var(--color-text-secondary)' }}>-</span>}
                      </td>
                      <td style={{ fontWeight: 700, color: pnlColor(pnlUsd), whiteSpace: 'nowrap' }}>
                        {pnlUsd != null ? fmtUsd(pnlUsd) : '-'}
                      </td>
                      <td style={{ fontWeight: 600, color: pnlColor(pnlPct), whiteSpace: 'nowrap' }}>
                        {pnlPct != null ? (pnlPct > 0 ? '+' : '') + Number(pnlPct).toFixed(2) + '%' : '-'}
                      </td>
                      <td style={{ fontSize: 11, color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>
                        <div>
                          <span style={{ color: 'var(--color-accent-green)' }}>
                            +{p.tp_pct != null ? Number(p.tp_pct).toFixed(0) : '?'}%
                          </span>
                          {' / '}
                          <span style={{ color: 'var(--color-accent-red, #ef5350)' }}>
                            -{p.sl_pct != null ? Number(p.sl_pct).toFixed(0) : '?'}%
                          </span>
                        </div>
                        <div style={{ fontSize: 10, color: 'var(--color-text-secondary)', opacity: 0.7 }}>
                          {fmtPrice(p.tp_price)} / {fmtPrice(p.sl_price)}
                        </div>
                      </td>
                      <td style={{ fontSize: 12, textAlign: 'center', whiteSpace: 'nowrap' }}>
                        <span title={p.leg_1_filled_at ? `leg 1 filled ${p.leg_1_filled_at}` : 'leg 1 pending (+25%)'}>
                          {p.leg_1_filled_at ? '▣' : '○'}
                        </span>
                        {' '}
                        <span title={p.leg_2_filled_at ? `leg 2 filled ${p.leg_2_filled_at}` : 'leg 2 pending (+50%)'}>
                          {p.leg_2_filled_at ? '▣' : '○'}
                        </span>
                        {p.floor_armed === 1 && (
                          <span title="floor armed" style={{ marginLeft: 4, color: 'var(--color-text-secondary)' }}>🛡</span>
                        )}
                      </td>
                      <td>{checkpointBadges(p)}</td>
                      <td style={{ fontSize: 12, color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>
                        {fmtRelative(p.opened_at)}
                      </td>
                      <td>
                        <button
                          disabled={closingId === p.id}
                          onClick={async () => {
                            if (!confirm('Close this position?')) return
                            setClosingId(p.id)
                            try {
                              const res = await fetch(`/api/trading/close/${p.id}`, { method: 'POST' })
                              if (res.ok) {
                                fetchAll()
                              } else {
                                const err = await res.json()
                                alert(err.error || 'Failed to close')
                              }
                            } catch {
                              alert('Network error')
                            } finally {
                              setClosingId(null)
                            }
                          }}
                          style={{
                            padding: '4px 10px',
                            fontSize: 11,
                            background: closingId === p.id ? 'rgba(150, 150, 150, 0.15)' : 'rgba(239, 83, 80, 0.15)',
                            color: closingId === p.id ? '#999' : '#ef5350',
                            border: '1px solid rgba(239, 83, 80, 0.3)',
                            borderRadius: 4,
                            cursor: closingId === p.id ? 'not-allowed' : 'pointer',
                          }}
                        >
                          {closingId === p.id ? 'Closing...' : 'Close'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Section 4: Closed Trades (paginated) */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Closed Trades
          </span>
          <span
            style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}
            aria-live="polite"
          >
            {closedTotal === 0
              ? 'No closed trades yet'
              : `Showing ${closedPage * CLOSED_PER_PAGE + 1}–${Math.min((closedPage + 1) * CLOSED_PER_PAGE, closedTotal)} of ${closedTotal}${closedTotal > CLOSED_PER_PAGE ? ' (sort applies to current page only)' : ''}`}
          </span>
        </div>
        {history.length === 0 ? (
          <div className="empty-state">No closed trades yet.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <SharedSortHeader col="_token" label="Token" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <SharedSortHeader col="_category" label="Category" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <SharedSortHeader col="entry_price" label="Entry / Exit" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <SharedSortHeader col="amount_usd" label="Amount" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <SharedSortHeader col="_pnl" label="PnL $" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <SharedSortHeader col="_pnl_pct" label="PnL %" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <SharedSortHeader col="exit_reason" label="Reason" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <SharedSortHeader col="closed_at" label="Duration" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                </tr>
              </thead>
              <tbody>
                {closedSort.sorted.map((h, i) => {
                  const pnl = h._pnl
                  const pnlPct = h._pnl_pct
                  const rowBg = pnl > 0
                    ? 'rgba(76, 175, 80, 0.05)'
                    : pnl < 0
                      ? 'rgba(239, 83, 80, 0.05)'
                      : 'transparent'
                  return (
                    <tr key={h.id || i} style={{ background: rowBg }}>
                      <td>
                        <TokenLink
                          tokenId={h.coin_id || h.token_id}
                          symbol={getTokenLabel(h)}
                          chain="coingecko"
                        />
                      </td>
                      <td style={{ fontSize: 11, maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {getCategory(h)}
                      </td>
                      <td style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                        {fmtPrice(h.entry_price)}
                        <span style={{ color: 'var(--color-text-secondary)', margin: '0 3px' }}>&rarr;</span>
                        {fmtPrice(h.exit_price)}
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>{fmtUsd(h.amount_usd)}</td>
                      <td style={{ fontWeight: 700, color: pnlColor(pnl), whiteSpace: 'nowrap' }}>{fmtUsd(pnl)}</td>
                      <td style={{ fontWeight: 600, color: pnlColor(pnlPct), whiteSpace: 'nowrap' }}>
                        {pnlPct != null ? (pnlPct > 0 ? '+' : '') + Number(pnlPct).toFixed(2) + '%' : '-'}
                      </td>
                      <td>{reasonBadge(h.exit_reason || h.close_reason || h.reason)}</td>
                      <td style={{ fontSize: 12, color: 'var(--color-text-secondary)', whiteSpace: 'nowrap' }}>
                        {fmtDuration(h.opened_at || h.created_at, h.closed_at)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {/* Pagination controls — R2-C1 fold: inline disabled style
                because plain .btn class has no :disabled rule. */}
            <div style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '12px 8px',
              borderTop: '1px solid var(--color-border)',
            }}>
              <button
                className="btn"
                disabled={closedPage === 0}
                onClick={() => setClosedPage(p => Math.max(0, p - 1))}
                aria-label="Previous page"
                style={{
                  opacity: closedPage === 0 ? 0.4 : 1,
                  cursor: closedPage === 0 ? 'not-allowed' : 'pointer',
                }}
              >
                ← Prev
              </button>
              <span
                style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}
                aria-live="polite"
              >
                Page {closedPage + 1} of {Math.max(1, Math.ceil(closedTotal / CLOSED_PER_PAGE))}
              </span>
              <button
                className="btn"
                disabled={(closedPage + 1) * CLOSED_PER_PAGE >= closedTotal}
                onClick={() => setClosedPage(p => p + 1)}
                aria-label="Next page"
                style={{
                  opacity: (closedPage + 1) * CLOSED_PER_PAGE >= closedTotal ? 0.4 : 1,
                  cursor: (closedPage + 1) * CLOSED_PER_PAGE >= closedTotal ? 'not-allowed' : 'pointer',
                }}
              >
                Next →
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import TokenLink from './TokenLink'

const GROUPS = [
  ['act_now', 'Review Now'],
  ['watch', 'Watch'],
  ['already_ran', 'Moved Already'],
  ['blocked', 'Blocked'],
]

function fmtUsd(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (!Number.isFinite(v)) return '-'
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e9) return sign + '$' + (abs / 1e9).toFixed(1) + 'B'
  if (abs >= 1e6) return sign + '$' + (abs / 1e6).toFixed(1) + 'M'
  if (abs >= 1e3) return sign + '$' + (abs / 1e3).toFixed(1) + 'K'
  return sign + '$' + abs.toFixed(2)
}

function fmtPct(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (!Number.isFinite(v)) return '-'
  return v.toFixed(2) + '%'
}

function rowKey(row) {
  return `${row.group}:${row.token_id}`
}

function loadSeen() {
  try {
    return JSON.parse(sessionStorage.getItem('tradeInboxSeen') || '{}')
  } catch {
    return {}
  }
}

function saveSeen(value) {
  try {
    sessionStorage.setItem('tradeInboxSeen', JSON.stringify(value))
  } catch {}
}

export default function TradeInboxTab() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [paused, setPaused] = useState(false)
  const [limit, setLimit] = useState(10)
  const [seen, setSeen] = useState(loadSeen)
  const [dismissed, setDismissed] = useState({})

  const fetchInbox = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/trade_inbox?limit_per_group=${limit}&window_hours=36`)
      const data = await res.json()
      if (!res.ok) throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`)
      setPayload(data)
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
    } finally {
      setLoading(false)
    }
  }, [limit])

  useEffect(() => {
    fetchInbox()
  }, [fetchInbox])

  useEffect(() => {
    if (paused) return undefined
    const t = setInterval(fetchInbox, 30000)
    return () => clearInterval(t)
  }, [fetchInbox, paused])

  useEffect(() => {
    if (!payload) return
    const next = { ...seen }
    for (const [group] of GROUPS) {
      for (const row of payload.groups?.[group] || []) {
        const key = rowKey(row)
        if (!next[key]) {
          next[key] = {
            first_seen_at: new Date().toISOString(),
            last_seen_group: row.group,
            last_seen_score: row.trade_score,
          }
        }
      }
    }
    setSeen(next)
    saveSeen(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload])

  const meta = payload?.meta || {}
  const dismissedCount = Object.keys(dismissed).length
  const visibleGroups = useMemo(() => {
    const out = {}
    for (const [group] of GROUPS) {
      out[group] = (payload?.groups?.[group] || []).filter(row => !dismissed[rowKey(row)])
    }
    return out
  }, [payload, dismissed])

  return (
    <div>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>Trade Inbox</span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Read-only review queue over open paper trades. Not execution advice.
          </span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="tab-btn" onClick={fetchInbox} disabled={loading} style={{ padding: '2px 8px', fontSize: 12 }}>
              {loading ? 'Refreshing...' : 'Refresh'}
            </button>
            <button className="tab-btn" onClick={() => setPaused(v => !v)} style={{ padding: '2px 8px', fontSize: 12 }}>
              {paused ? 'Resume' : 'Pause'}
            </button>
            {dismissedCount ? (
              <button className="tab-btn" onClick={() => setDismissed({})} style={{ padding: '2px 8px', fontSize: 12 }}>
                Restore {dismissedCount}
              </button>
            ) : null}
          </span>
        </div>
        <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)', fontSize: 12 }}>
          read_only={String(meta.read_only ?? '?')} not_trade_advice={String(meta.not_trade_advice ?? '?')} generated_at={meta.generated_at || '?'} source_rows={meta.source_rows_considered ?? '?'} scanned={meta.open_trades_scanned ?? '?'}
          {meta.source_truncated ? (
            <div style={{ marginTop: 8, color: 'var(--color-accent-amber)' }}>
              Source truncated at {meta.source_limit}; older open trades may not be represented.
            </div>
          ) : null}
          {error ? <div style={{ marginTop: 8, color: 'var(--color-accent-red)' }}>Error: {error}</div> : null}
        </div>
      </div>

      {GROUPS.map(([group, title]) => {
        const rows = visibleGroups[group] || []
        const hidden = meta.group_hidden_counts?.[group] || 0
        const total = meta.group_counts?.[group] || rows.length
        return (
          <div className="panel" key={group} style={{ marginBottom: 16 }}>
            <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span>{title}</span>
              <span style={{ color: 'var(--color-text-secondary)', fontSize: 12 }}>{total} total</span>
              {hidden ? <span style={{ color: 'var(--color-accent-amber)', fontSize: 12 }}>{hidden} hidden by limit</span> : null}
              {hidden ? (
                <button className="tab-btn" onClick={() => setLimit(v => Math.min(100, v + 10))} style={{ marginLeft: 'auto', padding: '2px 8px', fontSize: 12 }}>
                  Show more
                </button>
              ) : null}
            </div>
            {rows.length === 0 ? (
              <div className="empty-state" style={{ padding: 14 }}>
                {group === 'act_now' ? 'No review-now rows. Check Watch/Blocked diagnostics before assuming the desk is quiet.' : 'No rows in this group.'}
              </div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table className="candidates-table">
                  <thead>
                    <tr>
                      <th>Token</th>
                      <th>Action</th>
                      <th>Window</th>
                      <th>Score</th>
                      <th>From Entry</th>
                      <th>24h</th>
                      <th>MCap</th>
                      <th>Why / Risk</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(row => {
                      const key = rowKey(row)
                      const wasSeen = seen[key]
                      const changed = wasSeen && wasSeen.last_seen_group && wasSeen.last_seen_group !== row.group
                      return (
                        <tr key={key}>
                          <td>
                            <TokenLink tokenId={row.token_id} symbol={row.symbol || row.name} chain={row.chain} />
                            <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>{changed ? 'changed_group' : wasSeen ? 'seen_this_session' : 'new'}</div>
                          </td>
                          <td style={{ fontWeight: 700 }}>{row.action_label}</td>
                          <td>{row.window_state}</td>
                          <td>{row.trade_score}</td>
                          <td style={{ color: row.pct_from_entry != null && row.pct_from_entry >= 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red)' }}>{fmtPct(row.pct_from_entry)}</td>
                          <td>{fmtPct(row.price_change_24h)}</td>
                          <td>{fmtUsd(row.market_cap)}</td>
                          <td style={{ color: 'var(--color-text-secondary)', fontSize: 12 }}>
                            {(row.why_now || []).slice(0, 4).join(' | ') || row.block_reason_primary || '-'}
                          </td>
                          <td>
                            <button className="tab-btn" onClick={() => setDismissed(d => ({ ...d, [key]: true }))} style={{ padding: '2px 8px', fontSize: 12 }}>
                              Dismiss
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
        )
      })}
    </div>
  )
}

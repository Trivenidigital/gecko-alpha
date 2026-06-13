import React, { useCallback, useEffect, useMemo, useState } from 'react'
import TokenLink from './TokenLink'

// BL-NEW-CONVICTION-DASHBOARD-PANEL: read-only UI over /api/conviction/shortlist.
// RETROSPECTIVE — rows are coins that ALREADY appeared on the +20% gainers
// tracker, ranked by how many independent detectors confirmed them >=24h early.
// Not a pre-pump buy list. Observe-first.

const TIER_COLORS = { high: 'var(--color-accent-green)', watch: 'var(--color-accent-amber)', low: 'var(--color-text-secondary)' }

function fmtPct(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (!Number.isFinite(v)) return '-'
  return v.toFixed(0) + '%'
}

function fmtIso(iso) {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('en-US', { hour12: false })
  } catch {
    return iso
  }
}

function loadSeen() {
  try {
    return JSON.parse(localStorage.getItem('convictionSeen') || '{}')
  } catch {
    return {}
  }
}

function saveSeen(value) {
  try {
    localStorage.setItem('convictionSeen', JSON.stringify(value))
  } catch {}
}

export default function ConvictionTab() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [minTier, setMinTier] = useState('high')
  const [sort, setSort] = useState('score')
  const [seen, setSeen] = useState(loadSeen)

  const fetchShortlist = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(
        `/api/conviction/shortlist?min_tier=${minTier}&sort=${sort}&limit=50`
      )
      const data = await res.json()
      if (!res.ok) throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`)
      setPayload(data)
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
      setPayload(null)
    } finally {
      setLoading(false)
    }
  }, [minTier, sort])

  useEffect(() => {
    fetchShortlist()
    const t = setInterval(fetchShortlist, 30000)
    return () => clearInterval(t)
  }, [fetchShortlist])

  const rows = payload?.rows || []
  const meta = payload?.meta || {}

  // Mark coins not seen before this device as NEW, then remember them.
  const newFlags = useMemo(() => {
    const flags = {}
    for (const r of rows) flags[r.coin_id] = !seen[r.coin_id]
    return flags
  }, [rows, seen])

  useEffect(() => {
    if (rows.length === 0) return
    const next = { ...seen }
    let changed = false
    for (const r of rows) {
      if (!next[r.coin_id]) {
        next[r.coin_id] = new Date().toISOString()
        changed = true
      }
    }
    if (changed) {
      setSeen(next)
      saveSeen(next)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload])

  const newCount = rows.filter((r) => newFlags[r.coin_id]).length

  return (
    <div>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>Conviction Shortlist</span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Tracked gainers ranked by EARLY cross-surface confirmation. Read-only — not trade advice.
          </span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="tab-btn" onClick={() => setMinTier('high')} style={{ padding: '2px 8px', fontSize: 12, opacity: minTier === 'high' ? 1 : 0.6 }}>High</button>
            <button className="tab-btn" onClick={() => setMinTier('watch')} style={{ padding: '2px 8px', fontSize: 12, opacity: minTier === 'watch' ? 1 : 0.6 }}>Watch+</button>
            <button className="tab-btn" onClick={() => setSort('score')} style={{ padding: '2px 8px', fontSize: 12, opacity: sort === 'score' ? 1 : 0.6 }}>Top conviction</button>
            <button className="tab-btn" onClick={() => setSort('recency')} style={{ padding: '2px 8px', fontSize: 12, opacity: sort === 'recency' ? 1 : 0.6 }}>Newest</button>
            <button className="tab-btn" onClick={fetchShortlist} disabled={loading} style={{ padding: '2px 8px', fontSize: 12 }}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </span>
        </div>
        <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)', fontSize: 12 }}>
          <div style={{ marginBottom: 6, color: 'var(--color-accent-amber)' }}>
            RETROSPECTIVE: these coins already appeared on the gainers tracker — a conviction ranking, NOT a pre-pump buy list.
          </div>
          <div>
            tier≥{minTier} · sort={sort} · returned={meta.returned ?? '?'} of total_tracked={meta.total_tracked ?? '?'} · high-gate=≥{meta.high_tier_min_surfaces ?? '?'} early surfaces (≥{((meta.early_lead_minutes ?? 1440) / 60).toFixed(0)}h before +20%) · new-since-last-visit={newCount}
          </div>
          {meta.truncated ? (
            <div style={{ marginTop: 6, color: 'var(--color-accent-amber)' }}>
              Pool truncated at {meta.pool_cap}; oldest tracked gainers not ranked.
            </div>
          ) : null}
          {error ? <div style={{ marginTop: 8, color: 'var(--color-accent-red)' }}>Error: {error}</div> : null}
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">Ranked plays</div>
        {rows.length === 0 ? (
          <div className="empty-state" style={{ padding: 16 }}>
            No rows at this tier (does not imply none exist — try Watch+).
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Tier</th>
                  <th>Early surfaces</th>
                  <th>Peak gain</th>
                  <th>Confirming surfaces</th>
                  <th>Appeared on gainers</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.coin_id}>
                    <td>
                      <TokenLink tokenId={r.coin_id} symbol={r.symbol || r.name} chain="coingecko" />
                      {newFlags[r.coin_id] ? (
                        <span style={{ marginLeft: 6, fontSize: 10, fontWeight: 700, color: 'var(--color-accent-green)' }}>NEW</span>
                      ) : null}
                    </td>
                    <td style={{ fontWeight: 700, color: TIER_COLORS[r.tier] || 'var(--color-text-secondary)' }}>{r.tier}</td>
                    <td style={{ fontSize: 12 }}>{r.early_count}</td>
                    <td style={{ fontSize: 12, color: (r.peak_gain_pct || 0) >= 200 ? 'var(--color-accent-green)' : 'var(--color-text-primary)' }}>
                      {fmtPct(r.peak_gain_pct)}
                    </td>
                    <td style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                      {(r.contributing_surfaces || []).join(' · ')}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtIso(r.appeared_on_gainers_at)}</td>
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

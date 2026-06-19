import React, { useCallback, useEffect, useMemo, useState } from 'react'
import TokenLink from './TokenLink'
import { useSort, SortHeader } from './useSort.jsx'

// BL-NEW-CONVICTION-PROSPECTIVE-SCORE (V1, observe-only): read-only UI over
// /api/conviction/prospective.
//
// PROSPECTIVE — the mirror image of the retrospective Conviction tab. Rows are
// coins that have NOT yet appeared on the +20% gainers tracker, sub-$30M market
// cap, with sustained (>=24h-old) cross-surface early confirmation. Prospective
// precision is UNVALIDATED — this is an observation surface to measure forward
// hit-rate. It is NOT a recommendation and NOT trade advice. No alerts, no trades.

const TIER_COLORS = {
  high: 'var(--color-accent-green)',
  watch: 'var(--color-accent-amber)',
  low: 'var(--color-text-secondary)',
}
const TIER_RANK = { high: 3, watch: 2, low: 1 }
const SURFACES = ['chains', 'pipeline', 'narrative', 'spikes', 'momentum', 'slow_burn', 'acceleration', 'velocity']

function fmtMcap(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (!Number.isFinite(v)) return '-'
  if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B'
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M'
  if (v >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K'
  return '$' + v.toFixed(0)
}

function fmtAge(minutes) {
  if (minutes == null) return '-'
  const m = Number(minutes)
  if (!Number.isFinite(m)) return '-'
  if (m < 60) return Math.round(m) + 'm ago'
  if (m < 1440) return (m / 60).toFixed(1) + 'h ago'
  return (m / 1440).toFixed(1) + 'd ago'
}

function fmtIso(iso) {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('en-US', { hour12: false })
  } catch {
    return iso
  }
}

// Oldest contributing surface age (minutes) — the sustained-confirmation depth.
// first_detection_ages is a {surface: age_minutes} map; the MAX is how long the
// earliest detector has been confirming this coin.
function oldestAge(row) {
  const ages = row && row.first_detection_ages
  if (!ages || typeof ages !== 'object') return null
  const vals = Object.values(ages).map(Number).filter(Number.isFinite)
  return vals.length ? Math.max(...vals) : null
}

function loadSeen() {
  try {
    const v = JSON.parse(localStorage.getItem('prospectiveWatchlistSeen') || '{}')
    // Guard against a stored literal `null`/array/scalar (JSON.parse("null")
    // returns null without throwing; null[coin_id] would crash the render).
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {}
  } catch {
    return {}
  }
}

function saveSeen(value) {
  try {
    localStorage.setItem('prospectiveWatchlistSeen', JSON.stringify(value))
  } catch {}
}

export default function ProspectiveWatchlistTab() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [minTier, setMinTier] = useState('high')
  const [symbolQuery, setSymbolQuery] = useState('')
  const [minSurfaces, setMinSurfaces] = useState('')
  const [surfaceFilter, setSurfaceFilter] = useState('any')
  // "Seen before this visit" — frozen at mount so NEW badges stay stable across
  // refreshes for the whole visit. loadSeen() is type-guarded.
  const [visitSnapshot] = useState(loadSeen)

  const fetchWatchlist = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(
        `/api/conviction/prospective?min_tier=${minTier}&limit=50`
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
  }, [minTier])

  useEffect(() => {
    fetchWatchlist()
    const t = setInterval(fetchWatchlist, 30000)
    return () => clearInterval(t)
  }, [fetchWatchlist])

  const rows = payload?.rows || []
  const unknown = payload?.mcap_unknown || []
  const meta = payload?.meta || {}

  // NEW = appeared since the last visit (relative to the frozen snapshot).
  const newFlags = useMemo(() => {
    const flags = {}
    for (const r of rows) flags[r.coin_id] = !visitSnapshot[r.coin_id]
    return flags
  }, [rows, visitSnapshot])

  // Persist current coins so the NEXT visit's snapshot includes them. Re-read +
  // merge (no React state) keeps THIS visit's badges stable.
  useEffect(() => {
    if (rows.length === 0) return
    const stored = loadSeen()
    let changed = false
    for (const r of rows) {
      if (!stored[r.coin_id]) {
        stored[r.coin_id] = new Date().toISOString()
        changed = true
      }
    }
    if (changed) saveSeen(stored)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload])

  // Per-column filters (client-side over fetched rows). Blank/NaN => no constraint.
  const filtered = useMemo(() => {
    const q = symbolQuery.trim().toLowerCase()
    const minS = Number.parseFloat(minSurfaces)
    return rows.filter((r) => {
      if (q) {
        const hay = `${r.symbol || ''} ${r.name || ''} ${r.coin_id || ''}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      if (Number.isFinite(minS) && (r.early_count || 0) < minS) return false
      if (surfaceFilter !== 'any' && !(r.contributing_surfaces || []).includes(surfaceFilter)) return false
      return true
    })
  }, [rows, symbolQuery, minSurfaces, surfaceFilter])

  // Numeric sort proxies: tier rank (high>watch>low, not alphabetical), oldest
  // surface age (sustained depth), and mcap (small-first is the whole point).
  const enriched = useMemo(
    () =>
      filtered.map((r) => ({
        ...r,
        _tier_rank: TIER_RANK[r.tier] || 0,
        _oldest_age: oldestAge(r) ?? 0,
        _mcap: r.market_cap == null ? Infinity : Number(r.market_cap),
      })),
    [filtered]
  )
  const { sorted, sortCol, sortDir, handleSort } = useSort(enriched, '_oldest_age', 'desc')
  const visibleNewCount = sorted.filter((r) => newFlags[r.coin_id]).length

  const filtersActive = symbolQuery || minSurfaces || surfaceFilter !== 'any'
  const clearFilters = () => {
    setSymbolQuery('')
    setMinSurfaces('')
    setSurfaceFilter('any')
  }

  return (
    <div>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>Prospective Watchlist</span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Not-yet-pumped sub-$30M coins with sustained cross-surface early confirmation. Read-only — not trade advice.
          </span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="tab-btn" aria-pressed={minTier === 'high'} onClick={() => setMinTier('high')} style={{ padding: '2px 8px', fontSize: 12, opacity: minTier === 'high' ? 1 : 0.6 }}>High</button>
            <button className="tab-btn" aria-pressed={minTier === 'watch'} onClick={() => setMinTier('watch')} style={{ padding: '2px 8px', fontSize: 12, opacity: minTier === 'watch' ? 1 : 0.6 }}>Watch+</button>
            <button className="tab-btn" onClick={fetchWatchlist} disabled={loading} style={{ padding: '2px 8px', fontSize: 12 }}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </span>
        </div>
        <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)', fontSize: 12 }}>
          <div style={{ marginBottom: 6, color: 'var(--color-accent-amber)' }}>
            PROSPECTIVE · observe-only · prospective precision UNVALIDATED · not trade advice. Forward hit-rate is INSUFFICIENT_DATA until enough snapshots mature — no rate is shown.
          </div>
          <div>
            tier≥{minTier} · sub-$30M (fresh mcap) · high-gate=≥{meta.high_tier_min_surfaces ?? 4} sustained surfaces (first detection ≥{((meta.early_lead_minutes ?? 1440) / 60).toFixed(0)}h ago) · snapshot={fmtIso(meta.snapshot_at)} ({meta.snapshot_age_minutes == null ? '?' : meta.snapshot_age_minutes + 'm'} old) · returned={meta.returned ?? '?'} of batch={meta.total_in_batch ?? '?'} · new-since-last-visit={visibleNewCount}
          </div>
          {meta.enabled === false ? (
            <div style={{ marginTop: 6, color: 'var(--color-accent-amber)' }}>
              Prospective watchlist is disabled (CONVICTION_PROSPECTIVE_ENABLED=False).
            </div>
          ) : null}
          {meta.run_status && meta.run_status !== 'ok' ? (
            <div style={{ marginTop: 6, color: 'var(--color-accent-red)' }}>
              Last build status: {meta.run_status} — this batch may be incomplete (operator has been alerted). Rows shown are still valid.
            </div>
          ) : null}
          {error ? <div style={{ marginTop: 8, color: 'var(--color-accent-red)' }}>Error: {error}</div> : null}
        </div>
      </div>

      <div className="panel">
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span>Watchlist</span>
          <input
            type="text"
            placeholder="Filter symbol…"
            aria-label="Filter by symbol"
            value={symbolQuery}
            onChange={(e) => setSymbolQuery(e.target.value)}
            style={{ fontSize: 12, padding: '2px 6px', width: 130 }}
          />
          <label style={{ fontSize: 12 }}>
            min surfaces{' '}
            <input type="number" min="0" max="8" aria-label="Minimum sustained surfaces" value={minSurfaces} onChange={(e) => setMinSurfaces(e.target.value)} style={{ width: 48, fontSize: 12 }} />
          </label>
          <select aria-label="Filter by confirming surface" value={surfaceFilter} onChange={(e) => setSurfaceFilter(e.target.value)} style={{ fontSize: 12 }}>
            <option value="any">any surface</option>
            {SURFACES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          {filtersActive ? (
            <button className="tab-btn" onClick={clearFilters} style={{ padding: '2px 8px', fontSize: 12 }}>Clear</button>
          ) : null}
          <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--color-text-secondary)' }}>
            showing {sorted.length} of {rows.length}
          </span>
        </div>
        {rows.length === 0 ? (
          <div className="empty-state" style={{ padding: 16 }}>
            {meta.enabled === false
              ? 'Prospective watchlist is disabled (CONVICTION_PROSPECTIVE_ENABLED=False).'
              : meta.snapshot_at == null
              ? 'No snapshot yet (builder has not run, or last run wrote 0 rows).'
              : 'No sub-$30M rows at this tier (does not imply none exist — try Watch+).'}
          </div>
        ) : sorted.length === 0 ? (
          <div className="empty-state" style={{ padding: 16 }}>
            No rows match the active filters. <button className="tab-btn" onClick={clearFilters} style={{ padding: '2px 8px', fontSize: 12 }}>Clear filters</button>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <SortHeader col="symbol" label="Token" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
                  <SortHeader col="_tier_rank" label="Tier" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
                  <SortHeader col="_mcap" label="Market cap" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
                  <SortHeader col="early_count" label="Sustained surfaces" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
                  <SortHeader col="fresh_count" label="Emerging (<24h)" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
                  <SortHeader col="_oldest_age" label="Oldest signal" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
                  <th>Confirming surfaces</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((r) => (
                  <tr key={r.coin_id}>
                    <td>
                      <TokenLink tokenId={r.coin_id} symbol={r.symbol || r.name} chain="coingecko" />
                      {newFlags[r.coin_id] ? (
                        <span style={{ marginLeft: 6, fontSize: 10, fontWeight: 700, color: 'var(--color-accent-green)' }}>NEW</span>
                      ) : null}
                    </td>
                    <td style={{ fontWeight: 700, color: TIER_COLORS[r.tier] || 'var(--color-text-secondary)' }}>{r.tier}</td>
                    <td style={{ fontSize: 12 }}>{fmtMcap(r.market_cap)}</td>
                    <td style={{ fontSize: 12 }}>{r.early_count}</td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }} title="Fresh (<24h) surfaces — emerging context only, NOT counted toward high conviction">
                      {r.fresh_count ? '+' + r.fresh_count : '0'}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtAge(oldestAge(r))}</td>
                    <td style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                      {(r.contributing_surfaces || []).join(' · ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* mcap_unknown: fresh-mcap missing/stale rows. Surfaced so they are never
          silently dropped, but explicitly NOT counted as sub-$30M hits. */}
      {unknown.length > 0 ? (
        <div className="panel" style={{ marginTop: 16, opacity: 0.85 }}>
          <div className="panel-header">
            <span>Market cap unknown / stale ({unknown.length})</span>
            <span style={{ marginLeft: 12, fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
              Confirmed sustained early surfaces but mcap is missing or older than {meta.mcap_max_age_minutes ?? 1440}m — NOT classified as sub-$30M.
            </span>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Tier</th>
                  <th>Sustained surfaces</th>
                  <th>Confirming surfaces</th>
                </tr>
              </thead>
              <tbody>
                {unknown.map((r) => (
                  <tr key={r.coin_id}>
                    <td><TokenLink tokenId={r.coin_id} symbol={r.symbol || r.name} chain="coingecko" /></td>
                    <td style={{ fontWeight: 700, color: TIER_COLORS[r.tier] || 'var(--color-text-secondary)' }}>{r.tier}</td>
                    <td style={{ fontSize: 12 }}>{r.early_count}</td>
                    <td style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>{(r.contributing_surfaces || []).join(' · ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  )
}

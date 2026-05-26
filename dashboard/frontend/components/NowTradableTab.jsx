import React, { useEffect, useMemo, useState, useCallback } from 'react'
import TokenLink from './TokenLink'

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

function fmtIso(iso) {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('en-US', { hour12: false })
  } catch {
    return iso
  }
}

function riskBucket(score) {
  const n = Number(score)
  if (!Number.isFinite(n)) return null
  if (n < 30) return 'low'
  if (n <= 60) return 'mid'
  return 'high'
}

function normalizeCounterFlags(v) {
  if (!v || !Array.isArray(v)) return []
  const out = []
  for (const item of v) {
    if (typeof item === 'string') {
      const label = item.trim()
      if (label) out.push({ label, severity: null, detail: null })
      continue
    }
    if (item && typeof item === 'object') {
      const label = typeof item.flag === 'string' ? item.flag.trim() : ''
      if (!label) continue
      const severity =
        item.severity === 'high' || item.severity === 'medium' || item.severity === 'low'
          ? item.severity
          : null
      const detail = typeof item.detail === 'string' ? item.detail : null
      out.push({ label, severity, detail })
    }
  }
  const sevRank = { high: 0, medium: 1, low: 2, null: 3 }
  out.sort((a, b) => {
    const ar = sevRank[a.severity ?? 'null'] ?? 3
    const br = sevRank[b.severity ?? 'null'] ?? 3
    if (ar !== br) return ar - br
    return String(a.label).localeCompare(String(b.label))
  })
  return out
}

function renderCounterRiskCell(row) {
  const score = row?.counter_risk_score
  const bucket = riskBucket(score)
  const flags = normalizeCounterFlags(row?.counter_flags)
  const scoreOk = Number.isFinite(Number(score))
  const showScore = scoreOk && bucket
  const showFlags = flags.length > 0
  if (!showScore && !showFlags) return <span style={{ color: 'var(--color-text-secondary)' }}>-</span>

  const tooltip = [
    showScore ? `Counter-risk (enrichment-only; does not change verdict): ${Number(score).toFixed(0)}` : null,
    flags.length
      ? `Flags: ${flags.map(f => (f.detail ? `${f.label} — ${f.detail}` : f.label)).slice(0, 20).join(' | ')}`
      : null,
  ].filter(Boolean).join('\n')

  return (
    <span className="cr-cell" title={tooltip}>
      {showScore ? (
        <span className={`risk-badge ${bucket}`}>CR {Number(score).toFixed(0)}</span>
      ) : null}
      {showFlags ? (
        <span className="flag-badge neutral" style={{ marginLeft: showScore ? 6 : 0 }}>
          +{flags.length}
        </span>
      ) : null}
    </span>
  )
}

function summarizePriceFreshness(rows) {
  const times = rows.map(r => r?.price_updated_at).filter(Boolean).map(t => new Date(t).getTime()).filter(Number.isFinite)
  if (times.length === 0) return 'price_updated_at: n/a'
  const min = new Date(Math.min(...times)).toISOString()
  const max = new Date(Math.max(...times)).toISOString()
  return `price_updated_at: ${min} .. ${max} (UTC)`
}

export default function NowTradableTab() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  const fetchNow = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/live_candidates?limit=30&window_hours=36')
      const data = await res.json()
      if (!res.ok) throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`)
      setPayload(data)
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
      setPayload(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchNow()
    const t = setInterval(fetchNow, 30000)
    return () => clearInterval(t)
  }, [fetchNow])

  const rows = payload?.rows || []
  const meta = payload?.meta || {}

  const counts = useMemo(() => {
    const c = { candidate_review: 0, watch: 0, blocked: 0, data_insufficient: 0 }
    for (const r of rows) {
      const k = r?.verdict
      if (k && Object.prototype.hasOwnProperty.call(c, k)) c[k] += 1
    }
    return c
  }, [rows])

  const disclaimer = rows.find(r => r?.disclaimer)?.disclaimer
  const banner = [
    'EXPERIMENTAL — visibility-only.',
    meta.read_only === true ? 'read_only=true' : 'read_only=?',
    meta.not_trade_advice === true ? 'not_trade_advice=true' : 'not_trade_advice=?',
    meta.experimental === true ? 'experimental=true' : 'experimental=?',
    meta.generated_at ? `generated_at=${meta.generated_at}` : null,
  ].filter(Boolean).join(' ')

  return (
    <div>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>Now Tradable (V1)</span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Read-only labels over open paper trades. Not for execution or pruning.
          </span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <button className="tab-btn" onClick={fetchNow} disabled={loading} style={{ padding: '2px 8px', fontSize: 12 }}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </span>
        </div>
        <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)', fontSize: 12 }}>
          <div style={{ marginBottom: 6 }}>{banner}</div>
          <div style={{ color: 'var(--color-text-secondary)' }}>
            {payload ? summarizePriceFreshness(rows) : 'price_updated_at: n/a'} | limit={meta.limit ?? '?'} window_hours={meta.window_hours ?? '?'}
          </div>
          {disclaimer ? (
            <div style={{ marginTop: 6, whiteSpace: 'pre-wrap', color: 'var(--color-text-secondary)' }}>
              {disclaimer}
            </div>
          ) : null}
          {error ? (
            <div style={{ marginTop: 8, color: 'var(--color-accent-amber)' }}>
              Error: {error}
            </div>
          ) : null}
        </div>
        <div style={{ display: 'flex', gap: 10, padding: '10px 16px', flexWrap: 'wrap' }}>
          {['candidate_review', 'watch', 'blocked', 'data_insufficient'].map(k => (
            <div key={k} style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
              <span style={{ fontWeight: 700, color: 'var(--color-text-primary)' }}>{k}</span>: {counts[k]}
            </div>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">Candidates</div>
        {rows.length === 0 ? (
          <div className="empty-state" style={{ padding: 16 }}>
            No rows returned (this does not imply “safe”; it may mean empty cohort or missing data).
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Chain</th>
                  <th>MCap</th>
                  <th>From Entry</th>
                  <th>Entry</th>
                  <th>Verdict</th>
                  <th title="Counter-risk (enrichment-only; does not change verdict).">CR</th>
                  <th>Price Updated</th>
                  <th>Reasons</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.token_id}>
                    <td>
                      <TokenLink tokenId={r.token_id} symbol={r.symbol || r.name} chain={r.chain} />
                      {r.name ? <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>{r.name}</div> : null}
                    </td>
                    <td>{r.chain ? <span className={`chain-badge ${r.chain}`}>{r.chain}</span> : '-'}</td>
                    <td style={{ fontSize: 12 }}>{fmtUsd(r.market_cap)}</td>
                    <td style={{ fontSize: 12, color: r.pct_from_entry != null && r.pct_from_entry >= 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red)' }}>
                      {fmtPct(r.pct_from_entry)}
                    </td>
                    <td style={{ fontSize: 12 }}>{r.entry_quality || '-'}</td>
                    <td style={{ fontSize: 12, fontWeight: 700 }}>{r.verdict || '-'}</td>
                    <td style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                      {renderCounterRiskCell(r)}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {fmtIso(r.price_updated_at)} {r.price_is_stale ? <span style={{ color: 'var(--color-accent-amber)' }}>(stale)</span> : null}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                      {[...(r.inclusion_reasons || []), ...((r.risk_reasons || []).filter(x => x !== 'counter_risk_present_display_only_v1'))].slice(0, 6).join(' · ') || '-'}
                    </td>
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

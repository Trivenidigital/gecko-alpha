import React, { useState, useEffect, useCallback } from 'react'

/**
 * Visibility-only panel for the source_calls ledger.
 *
 * Per operator gate: shows health and rankability blockers — NEVER
 * per-source rankings. Backed by GET /api/source_calls/health which
 * returns rollup counts, never source identifiers.
 *
 * Forbidden by design (the API doesn't expose these either):
 *   - KOL rank / "best TG channel" / "best X handle"
 *   - per-source PnL or hit-rate comparison
 *   - source pruning recommendations
 *
 * Allowed surfaces (each rendered with explicit "not rankable yet"
 * framing when applicable):
 *   - writer freshness (minutes since last observed)
 *   - row count by source_type (tg vs x)
 *   - unresolvable rate + duplicate rate as percentages
 *   - outcome status distribution
 *   - price coverage by horizon (30m / 1h / 6h / 24h)
 *   - rankability rollup with not_rankable_label
 */

function fmtPct(v) {
  if (v == null) return '—'
  return (v * 100).toFixed(1) + '%'
}

function fmtInt(n) {
  if (n == null) return '—'
  return Number(n).toLocaleString()
}

function fmtAge(minutes) {
  if (minutes == null) return '—'
  if (minutes < 60) return `${minutes.toFixed(1)}m`
  if (minutes < 1440) return `${(minutes / 60).toFixed(1)}h`
  return `${(minutes / 1440).toFixed(1)}d`
}

function writerFreshnessStatus(wf) {
  if (!wf || wf.minutes_since_last_observed == null) {
    return { label: 'unknown', cls: 'badge-muted' }
  }
  const age = wf.minutes_since_last_observed
  const threshold = wf.lag_threshold_minutes ?? 30
  if (age <= threshold) {
    return { label: `fresh (${fmtAge(age)})`, cls: 'badge-ok' }
  }
  if (age <= threshold * 2) {
    return { label: `lagging (${fmtAge(age)})`, cls: 'badge-warn' }
  }
  return { label: `STALE (${fmtAge(age)})`, cls: 'badge-err' }
}

function CoverageBar({ label, count, total }) {
  const pct = total > 0 ? (count / total) * 100 : 0
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
      <span style={{ width: 80 }}>{label}</span>
      <div style={{
        flex: 1,
        background: '#333',
        borderRadius: 3,
        height: 14,
        overflow: 'hidden',
        position: 'relative',
      }}>
        <div style={{
          width: `${pct}%`,
          background: pct > 50 ? '#1b5e20' : pct > 10 ? '#4a3800' : '#5a1b1b',
          height: '100%',
        }} />
      </div>
      <span style={{ width: 100, textAlign: 'right', color: '#aaa' }}>
        {fmtInt(count)} / {fmtInt(total)} ({pct.toFixed(1)}%)
      </span>
    </div>
  )
}

export default function SourceCallsHealthPanel() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)
  const [lastFetched, setLastFetched] = useState(null)

  const fetchHealth = useCallback(async () => {
    try {
      const r = await fetch('/api/source_calls/health')
      if (!r.ok) {
        setErr(`HTTP ${r.status}`)
        setData(null)
      } else {
        setData(await r.json())
        setErr(null)
      }
    } catch (e) {
      setErr(String(e))
      setData(null)
    } finally {
      setLoading(false)
      setLastFetched(new Date())
    }
  }, [])

  useEffect(() => {
    fetchHealth()
    const t = setInterval(fetchHealth, 30_000)
    return () => clearInterval(t)
  }, [fetchHealth])

  if (loading && data == null) {
    return (
      <div className="panel">
        <h3>Source Calls — Ledger Health</h3>
        <div style={{ color: '#888' }}>loading…</div>
      </div>
    )
  }

  if (err) {
    return (
      <div className="panel">
        <h3>Source Calls — Ledger Health</h3>
        <div style={{ color: '#f88' }}>error: {err}</div>
      </div>
    )
  }

  if (data?.schema_missing) {
    return (
      <div className="panel">
        <h3>Source Calls — Ledger Health</h3>
        <div style={{ color: '#f88' }}>
          source_calls table missing — fresh install or pre-PR-#206 rollback
        </div>
      </div>
    )
  }

  if (!data || data.row_count === 0) {
    return (
      <div className="panel">
        <h3>Source Calls — Ledger Health</h3>
        <div style={{ color: '#888' }}>
          {data?.rankability?.not_rankable_label ?? 'no source_calls rows yet'}
        </div>
      </div>
    )
  }

  const wf = writerFreshnessStatus(data.writer_freshness)
  const total = data.row_count
  const rk = data.rankability ?? {}
  const cov = data.price_coverage ?? {}
  const outcomes = data.outcome_status_counts ?? {}

  return (
    <div className="panel">
      <h3 style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        Source Calls — Ledger Health
        <span style={{
          fontSize: 11,
          padding: '2px 8px',
          borderRadius: 3,
          background: wf.cls === 'badge-ok' ? '#1b5e20'
            : wf.cls === 'badge-warn' ? '#4a3800'
            : wf.cls === 'badge-err' ? '#5a1b1b'
            : '#333',
          color: wf.cls === 'badge-ok' ? '#a5d6a7'
            : wf.cls === 'badge-warn' ? '#ffd54f'
            : wf.cls === 'badge-err' ? '#ef9a9a'
            : '#aaa',
        }}>
          writer: {wf.label}
        </span>
        {lastFetched && (
          <span style={{ fontSize: 10, color: '#666', marginLeft: 'auto' }}>
            refreshed {lastFetched.toLocaleTimeString()}
          </span>
        )}
      </h3>

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
        gap: 24,
        marginTop: 12,
      }}>
        {/* LEFT: rates + counts */}
        <div>
          <div style={{ marginBottom: 8 }}>
            <span style={{ color: '#888', fontSize: 12 }}>total rows</span>{' '}
            <span style={{ fontWeight: 600, fontSize: 14 }}>{fmtInt(total)}</span>
            <span style={{ color: '#888', fontSize: 12, marginLeft: 12 }}>
              tg {fmtInt(data.row_count_by_source_type?.tg)} ·{' '}
              x {fmtInt(data.row_count_by_source_type?.x)}
            </span>
          </div>

          <div style={{ marginBottom: 6 }}>
            <span style={{ color: '#888', fontSize: 12 }}>unresolvable rate</span>{' '}
            <span style={{
              color: data.unresolvable_rate > 0.9 ? '#ef9a9a'
                : data.unresolvable_rate > 0.5 ? '#ffd54f' : '#a5d6a7',
              fontWeight: 600,
            }}>
              {fmtPct(data.unresolvable_rate)}
            </span>
          </div>

          <div style={{ marginBottom: 12 }}>
            <span style={{ color: '#888', fontSize: 12 }}>duplicate rate</span>{' '}
            <span style={{
              color: data.duplicate_rate > 0.5 ? '#ef9a9a'
                : data.duplicate_rate > 0.2 ? '#ffd54f' : '#a5d6a7',
              fontWeight: 600,
            }}>
              {fmtPct(data.duplicate_rate)}
            </span>
          </div>

          <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>
            outcome status
          </div>
          <div style={{ fontSize: 13 }}>
            {Object.entries(outcomes).sort((a, b) => b[1] - a[1]).map(([s, n]) => (
              <div key={s} style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span>{s}</span>
                <span style={{ color: '#aaa' }}>{fmtInt(n)}</span>
              </div>
            ))}
          </div>
        </div>

        {/* RIGHT: price coverage by horizon */}
        <div>
          <div style={{ fontSize: 12, color: '#888', marginBottom: 6 }}>
            price coverage by horizon
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            <CoverageBar label="at call" count={cov.with_price_at_call ?? 0} total={total} />
            <CoverageBar label="+30m" count={cov.with_forward_30m_pct ?? 0} total={total} />
            <CoverageBar label="+1h" count={cov.with_forward_1h_pct ?? 0} total={total} />
            <CoverageBar label="+6h" count={cov.with_forward_6h_pct ?? 0} total={total} />
            <CoverageBar label="+24h" count={cov.with_forward_24h_pct ?? 0} total={total} />
          </div>
        </div>
      </div>

      {/* RANKABILITY BLOCKER BANNER — NOT a rank surface */}
      <div style={{
        marginTop: 16,
        padding: '10px 14px',
        background: rk.rankable > 0 ? '#2a3a2a' : '#3a2a2a',
        border: '1px solid ' + (rk.rankable > 0 ? '#4caf50' : '#d32f2f'),
        borderRadius: 4,
        fontSize: 13,
      }}>
        <div style={{
          fontWeight: 600,
          marginBottom: 4,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'baseline',
          gap: 6,
        }}>
          <span>Why ranking is blocked</span>
          <span style={{ fontSize: 11, color: '#aaa' }}>
            ({rk.rankable ?? 0} / {rk.source_count ?? 0} sources meet gate)
          </span>
        </div>
        <div style={{ color: '#ddd', lineHeight: 1.4 }}>
          {rk.not_rankable_label ?? '—'}
        </div>
        <div style={{ fontSize: 11, color: '#888', marginTop: 8, lineHeight: 1.5 }}>
          <div>
            <strong style={{ color: '#bbb' }}>Gate:</strong>{' '}
            each source needs ≥10 distinct cluster events AND ≥50% forward-window coverage to be ranked.
          </div>
          <div style={{ marginTop: 4 }}>
            <strong style={{ color: '#bbb' }}>Why not show partial rankings:</strong>{' '}
            sub-gate sources have too few samples or biased coverage; ranking them
            below the gate would surface noise as signal.
          </div>
          {(rk.insufficient_sample > 0 || rk.biased_low_coverage > 0) && (
            <div style={{ marginTop: 4 }}>
              <strong style={{ color: '#bbb' }}>Current breakdown:</strong>{' '}
              {rk.insufficient_sample > 0 && (
                <span>{rk.insufficient_sample} source{rk.insufficient_sample !== 1 ? 's' : ''} below sample floor</span>
              )}
              {rk.insufficient_sample > 0 && rk.biased_low_coverage > 0 && ' · '}
              {rk.biased_low_coverage > 0 && (
                <span>{rk.biased_low_coverage} source{rk.biased_low_coverage !== 1 ? 's' : ''} below coverage floor</span>
              )}
              .
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

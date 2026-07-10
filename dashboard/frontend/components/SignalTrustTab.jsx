import React, { useCallback, useEffect, useMemo, useState } from 'react'
import ProvenanceExpander from './ProvenanceExpander'

// Live signal_params status badge (cockpit slice 1, fable-review Phase 2
// findings 2-3 / GA-35, GA-36). The static registry contradicted live state
// (chain_completed rendered trusted_experimental while auto-suspended since
// 2026-06-06).
//
// OPERATOR INVARIANT: trust surfaces read from the live store the engine
// writes (signal_params, joined server-side into each row's `live` field) —
// never static snapshots. A suspended signal shows SUSPENDED regardless of
// registry maturity.
function LiveStatusBadge({ live }) {
  if (!live) {
    return (
      <span
        title="No live signal_params row for this signal (or live join unavailable)."
        style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}
      >
        no live row
      </span>
    )
  }
  const suspended = live.suspended_at != null || live.enabled === 0
  if (suspended) {
    const tooltip = [
      live.suspended_reason ? `reason: ${live.suspended_reason}` : null,
      live.suspended_at ? `suspended_at: ${live.suspended_at}` : null,
      live.enabled === 0 && live.suspended_at == null ? 'enabled=0 in signal_params' : null,
      live.last_calibration_at ? `last_calibration_at: ${live.last_calibration_at}` : null,
    ].filter(Boolean).join('\n')
    return (
      <span
        title={tooltip || 'suspended in live signal_params'}
        data-testid="live-status-suspended"
        style={{
          display: 'inline-block',
          padding: '2px 7px',
          borderRadius: 4,
          fontSize: 11,
          fontWeight: 700,
          background: 'rgba(239, 83, 80, 0.15)',
          color: 'var(--color-accent-red, #ef5350)',
          whiteSpace: 'nowrap',
        }}
      >
        SUSPENDED
      </span>
    )
  }
  return (
    <span
      title={live.last_calibration_at ? `last_calibration_at: ${live.last_calibration_at}` : 'enabled in live signal_params'}
      style={{
        display: 'inline-block',
        padding: '2px 7px',
        borderRadius: 4,
        fontSize: 11,
        fontWeight: 700,
        background: 'rgba(76, 175, 80, 0.12)',
        color: 'var(--color-accent-green)',
        whiteSpace: 'nowrap',
      }}
    >
      enabled
    </span>
  )
}

function gateBadge(text) {
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '2px 7px',
        borderRadius: 999,
        fontSize: 11,
        fontWeight: 700,
        background: 'rgba(88, 166, 255, 0.12)',
        color: 'var(--color-accent-blue)',
        marginRight: 6,
      }}
    >
      {text}
    </span>
  )
}

export default function SignalTrustTab() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [scorecards, setScorecards] = useState(null)
  const [scorecardsError, setScorecardsError] = useState(null)

  const currencyFmt0 = useMemo(
    () => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }),
    []
  )
  const currencyFmt2 = useMemo(
    () => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }),
    []
  )

  const fetchNow = useCallback(async () => {
    setLoading(true)
    setError(null)
    setScorecardsError(null)
    setScorecards(null)
    try {
      const res = await fetch('/api/signal_trust_registry')
      const data = await res.json()
      if (!res.ok) {
        const msg = data?.error?.message || data?.detail || `HTTP ${res.status}`
        setPayload(data)
        throw new Error(msg)
      }
      setPayload(data)

      // Scorecards are best-effort: registry may exist even when DB tables are missing.
      try {
        const sc = await fetch('/api/signal_trust/scorecards')
        const scData = await sc.json()
        if (!sc.ok) {
          const msg2 = scData?.error?.message || scData?.detail || `HTTP ${sc.status}`
          setScorecards(null)
          setScorecardsError(msg2)
        } else {
          setScorecards(scData)
        }
      } catch (e2) {
        setScorecards(null)
        setScorecardsError(String(e2 && e2.message ? e2.message : e2))
      }
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchNow()
  }, [fetchNow])

  const meta = payload?.meta || {}
  const registry = payload?.registry || null
  const entries = Array.isArray(registry?.entries) ? registry.entries : []
  const scRows = Array.isArray(scorecards?.rows) ? scorecards.rows : []
  const scMeta = scorecards?.meta || {}

  const bannerLines = useMemo(() => [
    meta.generated_at ? `generated_at=${meta.generated_at}` : null,
    meta.registry_mtime ? `registry_mtime=${meta.registry_mtime}` : null,
    `signal_params_joined=${String(meta.signal_params_joined ?? '?')}`,
  ], [meta.generated_at, meta.registry_mtime, meta.signal_params_joined])

  // Finding 3: warn when the static registry file is stale. Maturity labels
  // come from the file; suspension state comes from the live join and is
  // always current regardless of this warning.
  const staleWarning = meta.registry_stale
    ? (meta.registry_stale_warning || 'registry stale — maturity labels may not reflect current state')
    : null

  return (
    <div>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>Signal Trust (V1)</span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Read-only registry. Not for pruning, suppression, or auto-disable.
          </span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <button className="tab-btn" onClick={fetchNow} disabled={loading} style={{ padding: '2px 8px', fontSize: 12 }}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </span>
        </div>
        <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)', fontSize: 12 }}>
          <div style={{ marginBottom: 8 }}>
            {gateBadge('visibility_only')}
            {gateBadge('not_for_pruning')}
            {gateBadge('not_for_alerting')}
            {gateBadge('not_for_auto_disable')}
          </div>
          <div>V1 trust registry — visibility-only. Live status column reads signal_params (the store the engine writes), not this file.</div>
          {staleWarning ? (
            <div
              data-testid="registry-stale-warning"
              style={{
                marginTop: 8,
                padding: '6px 10px',
                background: 'rgba(255, 183, 77, 0.10)',
                borderLeft: '2px solid var(--color-accent-amber)',
                borderRadius: 2,
                color: 'var(--color-accent-amber)',
                fontWeight: 600,
              }}
            >
              {staleWarning}
            </div>
          ) : null}
          <ProvenanceExpander lines={bannerLines} />
          {error ? (
            <div style={{ marginTop: 8, color: 'var(--color-accent-amber)' }}>
              Error: {error}
            </div>
          ) : null}
          {payload?.error?.errors && Array.isArray(payload.error.errors) ? (
            <div style={{ marginTop: 8, whiteSpace: 'pre-wrap', color: 'var(--color-accent-amber)' }}>
              {payload.error.errors.slice(0, 10).map((e, i) => (
                <div key={i}>- {e}</div>
              ))}
            </div>
          ) : null}
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">Registry entries</div>
        {entries.length === 0 ? (
          <div className="empty-state" style={{ padding: 16 }}>
            No entries (or registry unavailable). This surface is visibility-only.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Signal</th>
                  <th>Maturity (registry)</th>
                  <th title="Live signal_params state — the store the engine writes. Authoritative over registry maturity.">Live</th>
                  <th>Warning</th>
                  <th>Next gate</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, idx) => (
                  <tr key={`${e.signal_type || 'unknown'}:${idx}`}>
                    <td style={{ fontWeight: 700 }}>{e.signal_type || '-'}</td>
                    <td style={{ fontSize: 12 }}>{e.maturity_state || '-'}</td>
                    <td><LiveStatusBadge live={e.live} /></td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)', maxWidth: 520 }}>
                      {e.data_quality && typeof e.data_quality.warning === 'string' ? e.data_quality.warning : '-'}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                      {e.next_gate ? `${e.next_gate.type || '-'}: ${e.next_gate.threshold || '-'}` : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-header">Closed paper-trade evidence (read-only)</div>
        <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)', fontSize: 12 }}>
          <div>
            Fabricated closes (force-closed-unpriced) are excluded from n / win-rate below — same predicate as auto-suspend rolling stats.
          </div>
          <ProvenanceExpander
            lines={[
              `read_only=${String(scMeta.read_only ?? '?')} not_for_pruning=${String(scMeta.not_for_pruning ?? '?')} not_for_auto_disable=${String(scMeta.not_for_auto_disable ?? '?')} not_live_eligibility_verdict=${String(scMeta.not_live_eligibility_verdict ?? '?')}`,
              `cohort=${scMeta.cohort_policy || 'closed_paper_trades_excl_fabricated'} sort=${scMeta.sort_policy || 'signal_type_asc_not_ranked'} windows_days=${Array.isArray(scMeta.windows_days) ? scMeta.windows_days.join(',') : '?'}${scMeta.generated_at ? ` generated_at=${scMeta.generated_at}` : ''}`,
              `signal_params_joined=${String(scMeta.signal_params_joined ?? '?')}`,
            ]}
          />
          {scorecardsError ? (
            <div style={{ marginTop: 8, color: 'var(--color-accent-amber)' }}>
              Scorecards error: {scorecardsError}
            </div>
          ) : null}
        </div>
        {scorecardsError ? (
          <div className="empty-state" style={{ padding: 16 }}>
            Scorecards unavailable (see error above). Visibility-only.
          </div>
        ) : scRows.length === 0 ? (
          <div className="empty-state" style={{ padding: 16 }}>
            No scorecards rows (DB may be missing or empty). Visibility-only.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Signal</th>
                  <th>Maturity (registry)</th>
                  <th title="Live signal_params state — the store the engine writes. Authoritative over registry maturity.">Live</th>
                  <th>Open</th>
                  <th>7d closed paper</th>
                  <th>14d closed paper</th>
                  <th>30d closed paper</th>
                </tr>
              </thead>
              <tbody>
                {scRows.map((r) => {
                  const win = (days) => (Array.isArray(r.windows) ? r.windows.find(w => w.days === days) : null)
                  const w7 = win(7)
                  const w14 = win(14)
                  const w30 = win(30)
                  const fmt = (w) => {
                    if (!w || !w.closed) return '—'
                    const n = w.closed.closed_n ?? 0
                    if (n === 0) return '—'
                    const wr = w.closed.win_rate_pct ?? 0
                    const pnl = w.closed.total_pnl_usd ?? 0
                    const warns = Array.isArray(w.warnings) && w.warnings.length ? ` (${w.warnings.join(',')})` : ''
                    const wrTxt = Number.isFinite(wr) ? `${Math.round(wr)}%` : '—'
                    const pnlTxt = Number.isFinite(pnl) ? currencyFmt2.format(pnl) : '—'
                    return `n=${n} win=${wrTxt} pnl=${pnlTxt}${warns}`
                  }
                  const maturity = r.registry?.maturity_state || '—'
                  const openCount = r.open?.open_count ?? 0
                  const openExp = r.open?.open_exposure_usd ?? 0
                  const openExpTxt = Number.isFinite(openExp) ? currencyFmt0.format(openExp) : '—'
                  return (
                    <tr key={r.signal_type}>
                      <td style={{ fontWeight: 700 }}>{r.signal_type}</td>
                      <td style={{ fontSize: 12 }}>{maturity}</td>
                      <td><LiveStatusBadge live={r.live} /></td>
                      <td style={{ fontSize: 12 }}>{`n=${openCount} ${openExpTxt}`}</td>
                      <td style={{ fontSize: 12 }}>{fmt(w7)}</td>
                      <td style={{ fontSize: 12 }}>{fmt(w14)}</td>
                      <td style={{ fontSize: 12 }}>{fmt(w30)}</td>
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

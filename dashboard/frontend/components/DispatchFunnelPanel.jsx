import React, { useEffect, useState } from 'react'

// DASH-01 "Why nothing fired" — reason-count breakdown of dispatch decisions
// from /api/dispatch_funnel (over trade_decision_events). Visibility-only:
// it reports the block-reason split the dispatcher already recorded, it does
// not classify, rank, alert, or dispatch anything.

const WINDOWS = [1, 7, 30]

// Plain-words map for the raw reason keys the engine writes. Unknown reasons
// fall through to the raw key so a new reason is never hidden.
const REASON_WORDS = {
  suppressed: 'Suppressed (combo underperforming)',
  signal_disabled: 'Signal disabled',
  below_min_market_cap: 'Below min market cap',
  above_max_market_cap: 'Above max market cap',
  late_pump: 'Late pump (already ran)',
  missing_24h_change: 'Missing 24h change',
  junk_candidate: 'Junk candidate',
  missing_market_cap: 'Missing market cap',
  missing_entry_price: 'Missing entry price',
  warmup: 'Engine warmup',
  unknown_signal_type: 'Unknown signal type',
  stale_price: 'Stale price',
  open_position: 'Already holding position',
  max_open_trades: 'Max open trades',
  max_exposure: 'Max exposure reached',
  cooldown: 'Cooldown',
  unpriceable_token_id: 'Unpriceable token',
  no_price: 'No price',
  trade_mode_not_supported: 'Trade mode not supported',
  quarantined: 'Quarantined lane',
  universe_filter: 'Universe filter',
  paper_trade_opened: 'Opened',
}

function reasonLabel(reason) {
  return REASON_WORDS[reason] || reason
}

export default function DispatchFunnelPanel() {
  const [days, setDays] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch(`/api/dispatch_funnel?days=${days}`)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setData(json)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      }
    }
    load()
    const t = setInterval(load, 15_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [days])

  const blockedReasons = data
    ? data.reasons.filter((r) => r.decision === 'blocked')
    : []

  return (
    <div className="panel">
      <div className="panel-header">
        Why nothing fired
        <span className="funnel-window-picker">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              className={`funnel-window-btn ${days === w ? 'active' : ''}`}
              onClick={() => setDays(w)}
            >
              {w}d
            </button>
          ))}
        </span>
      </div>

      {error && <div className="empty-state">Failed to load: {error}</div>}
      {!error && !data && <div className="empty-state">Loading…</div>}

      {!error && data && (
        <>
          <div className="funnel-summary">
            <span className="funnel-summary-item">
              <span className="funnel-summary-value opened">{data.opened}</span>
              <span className="funnel-summary-label">opened</span>
            </span>
            <span className="funnel-summary-item">
              <span className="funnel-summary-value blocked">{data.blocked}</span>
              <span className="funnel-summary-label">blocked</span>
            </span>
            <span className="funnel-summary-item">
              <span className="funnel-summary-value">{data.total_events}</span>
              <span className="funnel-summary-label">decisions ({data.window_days}d)</span>
            </span>
          </div>

          {data.total_events === 0 ? (
            <div className="empty-state">No dispatch decisions in this window</div>
          ) : blockedReasons.length === 0 ? (
            <div className="empty-state">No blocked decisions in this window</div>
          ) : (
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Block reason</th>
                  <th>Count</th>
                  <th>Share of blocked</th>
                </tr>
              </thead>
              <tbody>
                {blockedReasons.map((r) => {
                  const share = data.blocked > 0
                    ? `${Math.round((r.count / data.blocked) * 100)}%`
                    : '—'
                  return (
                    <tr key={r.reason}>
                      <td>{reasonLabel(r.reason)}</td>
                      <td style={{ fontVariantNumeric: 'tabular-nums' }}>{r.count}</td>
                      <td style={{ fontVariantNumeric: 'tabular-nums' }}>{share}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  )
}

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

// Live-eligibility indicator for per-trade rows.
// Source: paper_trades.would_be_live (0 / 1 / NULL). NULL = pre-writer-deploy
// trade (writer shipped 2026-05-11); these are permanently un-classifiable.
// Explicit hover text for accessibility / screen-readers.
function EligibilityIcon({ value }) {
  if (value === 1) {
    return (
      <span
        title="Live-eligible: yes"
        style={{ color: 'var(--color-accent-green)', fontWeight: 700 }}
      >
        ✓
      </span>
    )
  }
  if (value === 0) {
    return (
      <span
        title="Live-eligible: no"
        style={{ color: 'var(--color-text-secondary)' }}
      >
        ✗
      </span>
    )
  }
  return (
    <span
      title="Pre-writer trade — not classifiable (opened before 2026-05-11)"
      style={{ color: 'var(--color-text-secondary)' }}
    >
      —
    </span>
  )
}

// BL-NEW-LIVE-ELIGIBLE follow-up: cohort-comparison panel for PnL by signal type.
// Default tab is 'full' so a casual glance at the dashboard doesn't anchor on
// the smaller-n live-eligible cohort. See tasks/plan_dashboard_live_eligible_view.md.
//
// Verdict thresholds (Vector B/C review folds, 2026-05-12):
// - MIN_ELIGIBLE_N_FOR_VERDICT: per-signal-type verdict requires eligible n >= 10
//   (below: INSUFFICIENT_DATA, not Tracking). Server can override via payload.
// - STRONG_PATTERN_WR_GAP_PP: 15pp threshold matches plan doc. Strict > per docstring.
// - STRONG_PATTERN_PNL_FLOOR: $200 magnitude required in BOTH cohorts before sign-flip
//   counts. Without floor, near-zero PnL trends produce sign-flips from single outlier trades.
// - NEAR_IDENTICAL_COHORTS: chain_completed's Tier 1a entry makes full ≈ eligible by
//   construction; divergence verdicts are not informative. UI annotates the row.
const MIN_ELIGIBLE_N_FOR_VERDICT_DEFAULT = 10
const STRONG_PATTERN_WR_GAP_PP = 15
const STRONG_PATTERN_PNL_FLOOR = 200

// Cohort-warming hint window. The writer for `would_be_live` deployed
// 2026-05-11T13:22Z; for ~14d after, an "0 of N (0.0% live-eligible)" reading
// is most likely cohort-not-warmed rather than eligibility-logic-broken.
// After WARMING_WINDOW_DAYS, the hint suppresses — a sustained 0% rate at
// day 14+ should remain visible as a finding to investigate, not get
// permanently softened by the hint. Same shape as the witness-vs-dispatch
// finding: the surface displays accurate data, the natural inference is wrong.
const WRITER_DEPLOY_ISO = '2026-05-11T13:22:00Z'
const WARMING_WINDOW_DAYS = 14

function PnlBySignalPanel({ bySignal, cohort, cohortView, setCohortView }) {
  // Prefer the cohort endpoint's full_cohort (carries win_rate_pct/avg_pnl_pct
  // uniformly); fall back to legacy /by-signal payload if the cohort endpoint
  // isn't deployed yet (older backend).
  const full = (cohort && cohort.full_cohort) || bySignal || []
  const eligible = (cohort && cohort.eligible_cohort) || []
  const excluded = (cohort && cohort.excluded_signal_types) || []
  const nearIdenticalCohorts = (cohort && cohort.near_identical_cohorts) || []
  const minN = (cohort && cohort.min_eligible_n_for_verdict) || MIN_ELIGIBLE_N_FOR_VERDICT_DEFAULT
  const caveat = cohort && cohort.small_n_caveat
  const verdictWindow = cohort && cohort.verdict_window_anchor
  const isEmpty = full.length === 0 && eligible.length === 0

  // Eligibility-rate counter: when toggle is on, show "Showing N of M (X%)"
  // so the missing trades are explicit rather than confusing. Empirically the
  // eligible cohort is ~5% of paper volume — without an explicit count, an
  // operator toggling on sees the table collapse and reads it as "view broke"
  // rather than "filter applied." Same anchoring concern as the small-n caveat.
  const fullN = full.reduce((s, r) => s + (r.trades ?? r.total_trades ?? 0), 0)
  const eligibleN = eligible.reduce((s, r) => s + (r.trades ?? r.total_trades ?? 0), 0)
  const eligiblePct = fullN > 0 ? (eligibleN / fullN) * 100 : 0
  const daysSinceWriterDeploy =
    (Date.now() - new Date(WRITER_DEPLOY_ISO).getTime()) / (1000 * 60 * 60 * 24)
  const showWarmingHint =
    fullN > 0 && eligibleN === 0 && daysSinceWriterDeploy < WARMING_WINDOW_DAYS

  const TabBtn = ({ id, label }) => (
    <button
      onClick={() => setCohortView(id)}
      style={{
        padding: '4px 10px',
        fontSize: 12,
        fontWeight: 600,
        border: '1px solid var(--color-border)',
        background: cohortView === id ? 'var(--color-accent-blue, #4a90e2)' : 'transparent',
        color: cohortView === id ? '#fff' : 'var(--color-text-secondary)',
        borderRadius: 4,
        cursor: 'pointer',
      }}
    >
      {label}
    </button>
  )

  const renderRow = (s, i, opts = {}) => {
    const pnl = s.total_pnl_usd ?? s.total_pnl ?? s.pnl ?? 0
    const wr = s.win_rate_pct ?? s.win_rate ?? (s.trades > 0 ? ((s.wins / s.trades) * 100) : 0)
    const rowBg = pnl > 0
      ? 'rgba(76, 175, 80, 0.07)'
      : pnl < 0
        ? 'rgba(239, 83, 80, 0.07)'
        : 'transparent'
    // Ticker inline display: when count ≤ 5, show all comma-separated;
    // otherwise show first 5 + " +N more" with title-attribute for the rest.
    // Subdued styling so the aggregate metrics remain the visual anchor.
    const symbols = Array.isArray(s.symbols) ? s.symbols : []
    const visibleSymbols = symbols.slice(0, 5)
    const overflowCount = Math.max(0, symbols.length - visibleSymbols.length)
    return (
      <tr key={(opts.keyPrefix || '') + (s.signal_type || i)} style={{ background: rowBg }}>
        <td style={{ fontWeight: 600 }}>
          <div>{s.signal_type || '-'}</div>
          {symbols.length > 0 && (
            <div
              style={{
                fontSize: 11,
                fontWeight: 400,
                color: 'var(--color-text-secondary)',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                maxWidth: 280,
              }}
              title={symbols.join(', ')}
            >
              {visibleSymbols.join(', ')}
              {overflowCount > 0 ? ` +${overflowCount} more` : ''}
            </div>
          )}
        </td>
        <td>{s.trades ?? s.total_trades ?? 0}</td>
        <td>{s.wins ?? 0}</td>
        <td style={{ fontWeight: 700, color: pnlColor(pnl) }}>{fmtUsd(pnl)}</td>
        <td>{Number(wr).toFixed(1)}%</td>
        <td style={{ color: pnlColor(s.avg_pnl_pct) }}>{fmtPct(s.avg_pnl_pct)}</td>
      </tr>
    )
  }

  // Side-by-side view: merge by signal_type, compute deltas.
  const sideBySide = (() => {
    const byType = new Map()
    full.forEach(s => byType.set(s.signal_type, { full: s, eligible: null }))
    eligible.forEach(s => {
      const e = byType.get(s.signal_type) || { full: null, eligible: null }
      e.eligible = s
      byType.set(s.signal_type, e)
    })
    return Array.from(byType.entries()).map(([signal_type, pair]) => ({
      signal_type,
      full: pair.full,
      eligible: pair.eligible,
    }))
  })()

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
          PnL by Signal Type
        </span>
        <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
          Which signals make money?
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <TabBtn id="full" label="All trades" />
          <TabBtn id="eligible" label="Live-eligible only" />
          <TabBtn id="side-by-side" label="Side-by-side" />
        </div>
      </div>

      {/* Caveat hoisted above the table (Vector C F-N1 fold): the calibration
          anchor must be visually peer to the ⚠ glyph, not subordinate. The plan
          doc's anchoring discipline lives or dies on whether the operator reads
          this before reading the verdicts. */}
      {(caveat || verdictWindow) && cohortView !== 'full' && (
        <div
          style={{
            padding: '8px 12px',
            margin: '0 0 8px 0',
            fontSize: 11,
            color: 'var(--color-text-secondary)',
            background: 'rgba(255, 183, 77, 0.06)',
            borderLeft: '2px solid var(--color-accent-amber)',
            borderRadius: 2,
          }}
        >
          {caveat && <div>{caveat}</div>}
          {verdictWindow && (
            <div style={{ marginTop: 4 }}>
              <strong style={{ color: 'var(--color-text-primary)' }}>Decision-locked at:</strong>{' '}
              {verdictWindow}.
            </div>
          )}
        </div>
      )}

      {cohortView !== 'full' && fullN > 0 && (
        <div
          style={{
            padding: '6px 12px',
            fontSize: 12,
            color: 'var(--color-text-secondary)',
            background: 'var(--color-bar-bg, #1a1a1a)',
            borderRadius: 4,
            marginBottom: 8,
          }}
        >
          Showing <strong style={{ color: 'var(--color-text-primary)' }}>{eligibleN}</strong>
          {' of '}
          <strong style={{ color: 'var(--color-text-primary)' }}>{fullN}</strong>
          {' trades ('}
          <strong style={{ color: 'var(--color-text-primary)' }}>{eligiblePct.toFixed(1)}%</strong>
          {' live-eligible) — toggle '}
          <button
            onClick={() => setCohortView('full')}
            style={{
              border: 'none',
              background: 'transparent',
              color: 'var(--color-accent-blue, #4a90e2)',
              padding: 0,
              cursor: 'pointer',
              fontSize: 12,
              textDecoration: 'underline',
            }}
          >
            All trades
          </button>
          {' to see full cohort'}
          {showWarmingHint && (
            <div style={{ marginTop: 4, opacity: 0.85 }}>
              Cohort warming — writer shipped {WRITER_DEPLOY_ISO.slice(0, 10)}
              {' ('}{Math.floor(daysSinceWriterDeploy)}d ago).
              Eligible closes accumulate as Tier 1a/1b/2a/2b trades close;
              ~5% of paper volume at steady state.
            </div>
          )}
        </div>
      )}

      {isEmpty ? (
        <div className="empty-state">No signal data yet. Trades will appear after the first paper trade closes.</div>
      ) : cohortView === 'side-by-side' ? (
        <div style={{ overflowX: 'auto' }}>
          <table className="candidates-table">
            <thead>
              <tr>
                <th rowSpan={2}>Signal Type</th>
                <th colSpan={3} style={{ textAlign: 'center', borderBottom: '1px solid var(--color-border)' }}>Full cohort</th>
                <th colSpan={3} style={{ textAlign: 'center', borderBottom: '1px solid var(--color-border)' }}>Live-eligible only</th>
                <th colSpan={2} style={{ textAlign: 'center', borderBottom: '1px solid var(--color-border)' }}>Δ (eligible − full)</th>
              </tr>
              <tr>
                <th>n</th><th>PnL</th><th>Win %</th>
                <th>n</th><th>PnL</th><th>Win %</th>
                <th>Win-rate Δ</th><th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {sideBySide.map(({ signal_type, full: f, eligible: e }) => {
                const fPnl = f?.total_pnl_usd ?? 0
                const ePnl = e?.total_pnl_usd ?? 0
                const fWr = f?.win_rate_pct ?? 0
                const eWr = e?.win_rate_pct ?? 0
                const eN = e?.trades ?? 0
                const wrDelta = e ? (eWr - fWr) : null
                // Vector B/C folds: gate verdict on eligible n; require magnitude
                // floor on BOTH cohorts before sign-flip counts; near-identical
                // (chain_completed) is annotated, not verdicted.
                const isNearIdentical = nearIdenticalCohorts.includes(signal_type)
                const hasEnoughN = eN >= minN
                const signFlipRaw = e && f && ((fPnl > 0 && ePnl < 0) || (fPnl < 0 && ePnl > 0))
                const signFlipPasses = signFlipRaw
                  && Math.abs(fPnl) >= STRONG_PATTERN_PNL_FLOOR
                  && Math.abs(ePnl) >= STRONG_PATTERN_PNL_FLOOR
                const strongPattern = (
                  hasEnoughN
                  && !isNearIdentical
                  && signFlipPasses
                  && wrDelta != null
                  && Math.abs(wrDelta) > STRONG_PATTERN_WR_GAP_PP
                )
                // Verdict label is the human-visible classification per pre-registration.
                let verdict, verdictColor
                if (isNearIdentical) {
                  verdict = 'near-identical'
                  verdictColor = 'var(--color-text-secondary)'
                } else if (!e || eN === 0) {
                  verdict = `INSUFFICIENT_DATA (n=0)`
                  verdictColor = 'var(--color-text-secondary)'
                } else if (!hasEnoughN) {
                  verdict = `INSUFFICIENT_DATA (n=${eN}, need >=${minN})`
                  verdictColor = 'var(--color-text-secondary)'
                } else if (strongPattern) {
                  verdict = 'strong-pattern (exploratory)'
                  verdictColor = 'var(--color-accent-amber)'
                } else if (signFlipRaw || (wrDelta != null && Math.abs(wrDelta) > 5)) {
                  verdict = 'moderate'
                  verdictColor = 'var(--color-text-primary)'
                } else {
                  verdict = 'tracking'
                  verdictColor = 'var(--color-text-secondary)'
                }
                const rowBg = strongPattern ? 'rgba(255, 183, 77, 0.10)' : 'transparent'
                return (
                  <tr key={signal_type} style={{ background: rowBg }}>
                    <td style={{ fontWeight: 600 }}>
                      {signal_type}
                      {strongPattern && (
                        <span
                          title="Strong-pattern (exploratory, NOT a verdict to act on): PnL sign flip + win-rate gap > 15pp + |PnL| floor met in both cohorts. Per pre-registration, action requires confirmation evaluation at 4-week mark."
                          style={{ marginLeft: 6, color: 'var(--color-accent-amber)' }}
                        >⚠</span>
                      )}
                      {isNearIdentical && (
                        <span
                          title="Near-identical cohorts: Tier 1a entry forces full ≈ eligible by construction. Divergence verdicts are not informative for this signal_type."
                          style={{ marginLeft: 6, color: 'var(--color-text-secondary)', fontWeight: 400, fontSize: 11 }}
                        >(near-identical)</span>
                      )}
                    </td>
                    <td>{f?.trades ?? 0}</td>
                    <td style={{ color: pnlColor(fPnl), fontWeight: 600 }}>{fmtUsd(fPnl)}</td>
                    <td>{fWr.toFixed(1)}%</td>
                    <td>{eN}</td>
                    <td style={{ color: pnlColor(ePnl), fontWeight: 600 }}>{e ? fmtUsd(ePnl) : '-'}</td>
                    <td>{e ? eWr.toFixed(1) + '%' : '-'}</td>
                    <td style={{ color: wrDelta == null ? 'var(--color-text-secondary)' : (wrDelta > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)') }}>
                      {wrDelta == null ? '-' : (wrDelta > 0 ? '+' : '') + wrDelta.toFixed(1) + 'pp'}
                    </td>
                    <td style={{ color: verdictColor, fontWeight: strongPattern ? 700 : 400, fontSize: 11 }}>
                      {verdict}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
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
              {(cohortView === 'eligible' ? eligible : full).map((s, i) => renderRow(s, i, { keyPrefix: cohortView + '-' }))}
              {cohortView === 'eligible' && eligible.length === 0 && (
                <tr><td colSpan={6} style={{ color: 'var(--color-text-secondary)', fontStyle: 'italic', padding: '12px 8px' }}>
                  No live-eligible closes in this window. would_be_live writer shipped 2026-05-11; eligibility rate is typically 5-10% of paper volume.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Outside-framework signal_types — visibility-not-hiding per §2.11.
          Header renamed (Vector C F-I2 fold): "Excluded" alone reads as "killed
          from paper trading"; this is about evaluation framework, not kill status. */}
      {excluded.length > 0 && (
        <details style={{ marginTop: 12, padding: '8px 12px', background: 'var(--color-bar-bg, #1a1a1a)', borderRadius: 4 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)' }}>
            Signals outside live-eligibility framework — still paper-trading ({excluded.length})
          </summary>
          <div style={{ marginTop: 8, fontSize: 11, color: 'var(--color-text-secondary)' }}>
            {excluded.map(e => (
              <div key={e.signal_type} style={{ padding: '4px 0', borderBottom: '1px dashed var(--color-border)' }}>
                <span style={{ fontWeight: 600, color: 'var(--color-text-primary)' }}>{e.signal_type}</span>
                {' — '}{e.reason}
                {' '}<span style={{ opacity: 0.7 }}>(lifetime n={e.lifetime_trades})</span>
              </div>
            ))}
          </div>
        </details>
      )}

      {/* Caveat hoisted above table; trailing render removed to avoid dup. */}
    </div>
  )
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
  // Cohort-toggle view (BL-NEW-LIVE-ELIGIBLE follow-up). Default 'full' —
  // smaller-n eligible view is opt-in to avoid anchoring on wide CIs.
  const [bySignalCohort, setBySignalCohort] = useState(null)
  const [cohortView, setCohortView] = useState('full') // 'full' | 'eligible' | 'side-by-side'
  const [positions, setPositions] = useState([])
  const [history, setHistory] = useState([])
  const [closedPage, setClosedPageState] = useState(_readStoredPage)
  const [closedTotal, setClosedTotal] = useState(0)
  const [sortCol, setSortCol] = useState('pnl_pct')
  const [sortDir, setSortDir] = useState('desc')
  const [closingId, setClosingId] = useState(null)
  // Per-table "show only live-eligible" filters. Independent per panel so
  // operator can filter positions without forcing the same on history.
  const [showOnlyEligibleOpen, setShowOnlyEligibleOpen] = useState(false)
  const [showOnlyEligibleClosed, setShowOnlyEligibleClosed] = useState(false)

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
      const [statsRes, sigRes, cohortRes, posRes, histRes, countRes] = await Promise.all([
        fetch('/api/trading/stats', { signal }),
        fetch('/api/trading/stats/by-signal', { signal }),
        fetch('/api/trading/stats/by-signal-cohort', { signal }),
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
      if (cohortRes.ok) setBySignalCohort(await cohortRes.json())
      if (posRes.ok) setPositions(await posRes.json())
      let histRows = null
      if (histRes.ok) {
        histRows = await histRes.json()
        setHistory(histRows)
      }
      if (countRes.ok) {
        const { total } = await countRes.json()
        setClosedTotal(total ?? 0)
      } else if (histRows && histRows.length > 0) {
        // V3-C1 PR-stage fix: count endpoint failed but history loaded
        // → fall back to history.length so the header doesn't show
        // "No closed trades yet" while rows are clearly visible.
        setClosedTotal(prev => (prev > 0 ? prev : histRows.length))
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
      // V3-I2 PR-stage fix: log auto-clamp for observability — silent
      // page rewrites are otherwise invisible if they fire unexpectedly.
      console.warn(
        `[closed-trades] auto-clamp page=${closedPage} → ${lastPage} (total=${closedTotal})`
      )
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

  const filteredPositions = showOnlyEligibleOpen
    ? positions.filter((p) => p.would_be_live === 1)
    : positions
  const sortedPositions = [...filteredPositions].sort((a, b) => {
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

  const filteredHistory = React.useMemo(
    () =>
      showOnlyEligibleClosed
        ? enrichedHistory.filter((h) => h.would_be_live === 1)
        : enrichedHistory,
    [enrichedHistory, showOnlyEligibleClosed],
  )

  const closedSort = useSort(filteredHistory, 'closed_at', 'desc')

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

      {/* Section 2: PnL by Signal Type — cohort-toggle view (BL-NEW-LIVE-ELIGIBLE follow-up) */}
      <PnlBySignalPanel
        bySignal={bySignal}
        cohort={bySignalCohort}
        cohortView={cohortView}
        setCohortView={setCohortView}
      />


      {/* Section 3: Open Positions */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Open Positions
          </span>
          {positions.length > 0 && (
            <div className="summary-line" style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
              {showOnlyEligibleOpen
                ? `${filteredPositions.length} of ${positions.length} active (live-eligible)`
                : `${positions.length} active`}
            </div>
          )}
          <label style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={showOnlyEligibleOpen}
              onChange={(e) => setShowOnlyEligibleOpen(e.target.checked)}
            />
            Show only live-eligible
          </label>
        </div>
        {positions.length === 0 ? (
          <div className="empty-state">No open positions.</div>
        ) : filteredPositions.length === 0 ? (
          <div className="empty-state">
            No live-eligible open positions.{' '}
            <button
              type="button"
              onClick={() => setShowOnlyEligibleOpen(false)}
              style={{ border: 'none', background: 'transparent', color: 'var(--color-accent-blue, #4a90e2)', cursor: 'pointer', fontSize: 12, textDecoration: 'underline', padding: 0 }}
            >
              Show all positions
            </button>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <SortHeader col="pnl_pct" label="Rank" />
                  <SortHeader col="token" label="Token" />
                  <th title="Live-eligible: would this trade have been opened under live FCFS-20-slots capital constraints? See tasks/findings_open_position_price_freshness_2026_05_12.md.">Eligible</th>
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
                      <td style={{ textAlign: 'center' }}>
                        <EligibilityIcon value={p.would_be_live} />
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
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Closed Trades
          </span>
          <span
            style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}
            aria-live="polite"
          >
            {closedTotal === 0
              ? 'No closed trades yet'
              // V3-I1 PR-stage fix: clamp lower bound so a stale
              // sessionStorage page (e.g., page=99999 from a prior session
              // when N=10) doesn't render nonsensical "1999981–10".
              : `Showing ${Math.min(closedPage * CLOSED_PER_PAGE + 1, closedTotal)}–${Math.min((closedPage + 1) * CLOSED_PER_PAGE, closedTotal)} of ${closedTotal}${closedTotal > CLOSED_PER_PAGE ? ' (sort applies to current page only)' : ''}`}
          </span>
          <label style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--color-text-secondary)', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={showOnlyEligibleClosed}
              onChange={(e) => setShowOnlyEligibleClosed(e.target.checked)}
            />
            Show only live-eligible
          </label>
        </div>
        {history.length === 0 ? (
          <div className="empty-state">No closed trades yet.</div>
        ) : filteredHistory.length === 0 ? (
          <div className="empty-state">
            No live-eligible closed trades in this page.{' '}
            <button
              type="button"
              onClick={() => setShowOnlyEligibleClosed(false)}
              style={{ border: 'none', background: 'transparent', color: 'var(--color-accent-blue, #4a90e2)', cursor: 'pointer', fontSize: 12, textDecoration: 'underline', padding: 0 }}
            >
              Show all closed trades
            </button>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <SharedSortHeader col="_token" label="Token" sortCol={closedSort.sortCol} sortDir={closedSort.sortDir} onSort={closedSort.handleSort} />
                  <th title="Live-eligible: would this trade have been opened under live FCFS-20-slots capital constraints? See tasks/findings_open_position_price_freshness_2026_05_12.md.">Eligible</th>
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
                      <td style={{ textAlign: 'center' }}>
                        <EligibilityIcon value={h.would_be_live} />
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

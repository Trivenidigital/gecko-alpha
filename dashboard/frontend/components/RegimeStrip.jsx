import React from 'react'

// Read-only regime + earliness strip, rendered adjacent to the BTC/SOL
// benchmark chips on the Today's Focus header.
//
// DASH-07 / SIG-09 (display phase): trailing-7d per-trade paper PnL with a
// hostile (red) cue when the figure is below the server-side display
// threshold. This lives in its OWN component — NOT BtcSolBenchmarkStrip —
// precisely because that strip is pinned to a single uniform color with a
// static-scan anti-scope test; a value-conditioned tint would violate it.
// SIG-08 (surface phase): the detection-earliness truth (median lead-time vs
// CG trending + the "no reference" share), shown honestly even when brutal.
//
// Numeric-only, factual copy; no advice / ranking / urgency vocabulary. Both
// inputs are independently optional and omitted server-side when their data
// is absent (presence-iff-data), so an empty dict renders nothing. DISPLAY
// ONLY — this surface gates no behavior (the SIG-09 throttle is out of scope).

const EM_DASH = '—'

function fmtSignedUsd(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return null
  const sign = n < 0 ? '-' : '+'
  return sign + '$' + Math.abs(n).toFixed(2)
}

function fmtHours(minutes) {
  const n = Number(minutes)
  if (!Number.isFinite(n)) return null
  return (Math.abs(n) / 60).toFixed(1) + 'h'
}

function trailingChip(trailingPnl) {
  if (!trailingPnl || typeof trailingPnl !== 'object') return null
  const n = Number(trailingPnl.closed_trades)
  const gate = Number(trailingPnl.n_gate)
  if (!Number.isFinite(n)) return null
  // n-gated: below the gate the per-trade figure is not trustworthy, so we
  // render the em dash rather than a number.
  if (!Number.isFinite(gate) || n < gate) {
    return (
      <span key="pnl" className="todays-focus-regime">
        {`7d/trade: ${EM_DASH} (n=${n})`}
      </span>
    )
  }
  const perTrade = fmtSignedUsd(trailingPnl.per_trade_usd)
  if (perTrade == null) return null
  const hostile = trailingPnl.hostile === true
  const className = hostile
    ? 'todays-focus-regime todays-focus-regime-hostile'
    : 'todays-focus-regime'
  return (
    <span key="pnl" className={className}>
      {`7d/trade: ${perTrade} (n=${n})`}
    </span>
  )
}

function earlinessChip(earliness) {
  if (!earliness || typeof earliness !== 'object') return null
  const parts = []
  const median = earliness.median_lead_time_min
  const m = Number(median)
  if (median != null && Number.isFinite(m)) {
    const direction = m >= 0 ? 'late' : 'early'
    parts.push(`median ${fmtHours(m)} ${direction} vs CG trending`)
  } else {
    parts.push(`median ${EM_DASH} vs CG trending`)
  }
  const pct = Number(earliness.no_reference_pct)
  if (Number.isFinite(pct)) parts.push(`${pct.toFixed(0)}% no reference`)
  return (
    <span key="early" className="todays-focus-regime">
      {parts.join(' | ')}
    </span>
  )
}

export default function RegimeStrip({ trailingPnl, earliness }) {
  const chips = []
  const pnl = trailingChip(trailingPnl)
  if (pnl) chips.push(pnl)
  const early = earlinessChip(earliness)
  if (early) chips.push(early)
  if (chips.length === 0) return null
  return (
    <span
      className="todays-focus-regime-group"
      aria-label="Trailing 7-day per-trade paper PnL and detection earliness vs CG trending"
    >
      {chips}
    </span>
  )
}

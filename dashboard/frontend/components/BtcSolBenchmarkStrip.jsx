import React from 'react'

// Pure functional renderer for the BTC + SOL 4h benchmark chips.
//
// PR-D anti-scope (plan-doc + reviewer folds, structurally enforced):
// - Strict-pinned aria-label per layout test.
// - No banner wrapper; renders inline spans alongside existing meta chips.
// - Single uniform color regardless of sign.
// - No regime, advice, or sentiment vocabulary. Static-scan in
//   test_todays_focus_benchmark_strip_has_no_regime_or_advice_vocabulary
//   enforces absence of the banned vocabulary list.
// - Returns null when benchmarks is null/undefined OR no allowed key
//   is present. Empty dict therefore renders nothing.
// - Sign is explicit per fmtSignedPct. Sign character itself does NOT
//   carry styling.

const BENCHMARK_ORDER = ['btc_4h_pct', 'sol_4h_pct']
const BENCHMARK_LABEL = { btc_4h_pct: 'BTC 4h', sol_4h_pct: 'SOL 4h' }

function fmtSignedPct(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return null
  const formatted = n.toFixed(2)
  return n >= 0 ? '+' + formatted + '%' : formatted + '%'
}

export default function BtcSolBenchmarkStrip({ benchmarks }) {
  if (!benchmarks || typeof benchmarks !== 'object') return null
  const chips = []
  for (const key of BENCHMARK_ORDER) {
    if (!(key in benchmarks)) continue
    const formatted = fmtSignedPct(benchmarks[key])
    if (formatted == null) continue
    chips.push(
      <span
        key={key}
        className="todays-focus-benchmark"
      >{`${BENCHMARK_LABEL[key]}: ${formatted}`}</span>
    )
  }
  if (chips.length === 0) return null
  return (
    <React.Fragment>
      <span
        className="todays-focus-benchmark-group"
        aria-label="BTC and SOL 4-hour deltas"
      >{chips}</span>
    </React.Fragment>
  )
}

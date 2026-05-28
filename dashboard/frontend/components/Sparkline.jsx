import React from 'react'

// Pure functional 24h price-path sparkline renderer.
//
// PR-C anti-scope (plan-doc + reviewer folds, structurally enforced):
// - SVG geometry strictly limited to polyline. Source is statically
//   scanned (see tests/test_dashboard_frontend_layout.py) to assert the
//   banned-tag substrings are absent.
// - aria-label is strict-pinned to the literal string "Sparkline" via
//   layout test; no other extension permitted.
// - Single uniform stroke color (no green-for-up / red-for-down).
// - No fill, gradient, animation, tooltip, or interactive handler.
// - Returns null on missing/empty/single-point input (parent renders
//   the factual "Sparkline unavailable" fallback string).
// - All-same-price degenerate y-range renders a horizontal line at
//   y = height/2.

const DEFAULT_WIDTH = 80
const DEFAULT_HEIGHT = 24

export default function Sparkline({ points, width = DEFAULT_WIDTH, height = DEFAULT_HEIGHT }) {
  if (!Array.isArray(points) || points.length < 2) {
    return null
  }

  const xs = points.map(p => Number(p[0]))
  const ys = points.map(p => Number(p[1]))
  if (xs.some(x => !Number.isFinite(x)) || ys.some(y => !Number.isFinite(y))) {
    return null
  }

  const xMin = Math.min(...xs)
  const xMax = Math.max(...xs)
  const yMin = Math.min(...ys)
  const yMax = Math.max(...ys)

  const xRange = xMax - xMin
  const yRange = yMax - yMin

  const coords = points.map((p, i) => {
    const t = Number(p[0])
    const v = Number(p[1])
    const x = xRange > 0 ? ((t - xMin) / xRange) * width : (i / Math.max(1, points.length - 1)) * width
    // Degenerate y-range (all-same-price): draw horizontal at midpoint.
    const y = yRange > 0
      ? height - ((v - yMin) / yRange) * height
      : height / 2
    return `${x.toFixed(2)},${y.toFixed(2)}`
  })

  return (
    <svg
      className="todays-focus-sparkline"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-label="Sparkline"
      role="img"
    >
      <polyline points={coords.join(' ')} fill="none" />
    </svg>
  )
}

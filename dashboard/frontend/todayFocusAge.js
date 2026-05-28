// Today's Focus factual relative-age formatter.
//
// formatDetectionAge(hours) returns a compact factual relative-age string
// derived from the row's server-computed opened_age_hours. The server
// guarantees the value is rounded to 2 decimals, so no FP-edge handling
// is required at the helper level.
//
// Output is factual only: never interpretive, never directional. The
// pinned format spec lives in
// tasks/plan_todays_focus_detection_age_and_new_since_2026_05_28.md.

const HOURS_PER_DAY = 24
const DAYS_CAP = 7

export function formatDetectionAge(hours) {
  if (hours == null) return '-'
  const v = Number(hours)
  if (!Number.isFinite(v)) return '-'
  if (v < 0) return '-'

  if (v < 1) {
    const minutes = Math.round(v * 60)
    if (minutes <= 0) return '< 1m ago'
    return minutes + 'm ago'
  }

  if (v < HOURS_PER_DAY) {
    return v.toFixed(1) + 'h ago'
  }

  const days = v / HOURS_PER_DAY
  if (days >= DAYS_CAP) return '7d+ ago'
  return days.toFixed(1) + 'd ago'
}

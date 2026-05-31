// What Changed factual copy chokepoint.
//
// Every rendered copy/template string for the What Changed panel originates
// here so the factual-copy firewall has ONE file to scan. Copy is strictly
// observational: integer counts, signed USD, relative age, plain neutral
// nouns. No advisory phrasing, no time-pressure framing, no ranking-as-advice.
//
// Re-imports the shared BANNED_PATTERNS from todayFocusFacts.js (single
// source of truth) so a self-check can run against the same canonical list
// the Python contract scanner mirrors. We do NOT redeclare a subset here.

import { BANNED_PATTERNS } from './todayFocusFacts.js'

export { BANNED_PATTERNS }

// Signed USD: +$1.2K / -$317.00 (mirror TodayFocusPanel fmtUsd).
export function fmtUsd(value) {
  if (value == null) return '-'
  const v = Number(value)
  if (!Number.isFinite(v)) return '-'
  const sign = v >= 0 ? '+' : '-'
  const abs = Math.abs(v)
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}K`
  return `${sign}$${abs.toFixed(2)}`
}

export const PANEL_TITLE = 'What Changed since last visit'

export const EMPTY_LABEL = 'No changes since last visit.'

export const UNAVAILABLE_LABEL = 'unavailable'

// ---- Category 1: newly-closed trades ----

export function closedHeadline(count, netRealizedSince, netUnavailableCount = 0) {
  const base = `Newly closed since last visit: ${count} (net realized ${fmtUsd(netRealizedSince)}`
  if (netUnavailableCount > 0) {
    return `${base}, excludes ${netUnavailableCount} unavailable)`
  }
  return `${base})`
}

export function closedRow(symbol, realizedPnl, closedAgeLabel) {
  const pnl = realizedPnl == null ? UNAVAILABLE_LABEL : fmtUsd(realizedPnl)
  const age = closedAgeLabel == null ? UNAVAILABLE_LABEL : `closed ${closedAgeLabel}`
  return `${symbol}  ${pnl}  ${age}`
}

// ---- Category 2: open-position unrealized-PnL changes ----

export const SWINGS_HEADLINE =
  'Open-position unrealized-PnL changes since last visit — largest absolute first'

export function swingRow(symbol, current, prev, delta) {
  return `${symbol}  ${fmtUsd(current)} (was ${fmtUsd(prev)}, change ${fmtUsd(delta)})`
}

export function newlyOpenedRow(symbol, current) {
  return `${symbol}  ${fmtUsd(current)} (newly opened since last visit)`
}

export function swingsTruncationFootnote(shown, total) {
  return `Showing ${shown} of ${total}, sorted by absolute change`
}

// ---- Category 3: system-health status changes ----

export const HEALTH_HEADLINE = 'Health status changes since last visit'

export function healthRow(subsystem, previousStatus, currentStatus) {
  return `${subsystem}  ${previousStatus} -> ${currentStatus}`
}

// ---- Shared / cross-category ----

export function historyTruncationFootnote(shown, total) {
  return `Showing ${shown} of ${total}, most recent 50`
}

export function unavailableRowsFootnote(count) {
  return `${count} rows unavailable (missing id)`
}

export function categoryUnavailable(reason) {
  if (!reason) return `Data ${UNAVAILABLE_LABEL}`
  return `Data ${UNAVAILABLE_LABEL}: ${reason}`
}

export function firstVisitClosedLabel(knownCount) {
  return `First visit — baseline recorded (${knownCount} closed trades known).`
}

export function firstVisitOpenLabel(openCount) {
  return `First visit — baseline recorded (${openCount} open positions).`
}

export function firstVisitHealthLabel(subsystemCount) {
  return `First visit — baseline recorded (${subsystemCount} health subsystems).`
}

export const FIRST_VISIT_LABEL = 'First visit — baseline recorded.'

export function baselineAgeLabel(ageLabel) {
  if (!ageLabel) return 'Baseline: unknown'
  return `Baseline: ${ageLabel}`
}

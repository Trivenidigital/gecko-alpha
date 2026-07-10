const MAX_WATCH_ROWS = 3
const MAX_LATE_ROWS = 2

function asRows(payload, group) {
  const rows = payload?.groups?.[group]
  return Array.isArray(rows) ? rows : []
}

function numeric(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function flagSeverity(flag) {
  if (!flag || typeof flag !== 'object') return ''
  return String(flag.severity || flag.level || '').toLowerCase()
}

function flagName(flag) {
  if (typeof flag === 'string') return flag.toLowerCase()
  if (!flag || typeof flag !== 'object') return ''
  return String(flag.flag || flag.type || flag.name || flag.reason || '').toLowerCase()
}

export function decisionRiskTier(row) {
  const score = numeric(row?.counter_risk_score)
  const flags = Array.isArray(row?.counter_flags) ? row.counter_flags : []
  const hasHighFlag = flags.some(flag => flagSeverity(flag) === 'high')
  const hasPeakFlag = flags.some(flag => flagName(flag).includes('already_peaked'))
  if (score != null && score >= 70) return 'high'
  if (hasHighFlag || hasPeakFlag) return 'high'
  if (score != null && score >= 40) return 'medium'
  return 'low'
}

function isHardBlocked(row) {
  if (!row) return true
  if (row.group === 'blocked') return true
  if (row.block_reason_primary) return true
  const risks = new Set(Array.isArray(row.risk_reasons) ? row.risk_reasons : [])
  return risks.has('no_price_snapshot_for_token_id') ||
    risks.has('entry_price_missing_or_invalid') ||
    risks.has('price_timestamp_unparseable')
}

function isFreshEnough(row) {
  const stale = numeric(row?.price_staleness_minutes)
  return stale == null || stale < 60
}

function isOpenWindow(row) {
  return row?.window_state === 'open'
}

function isPrimaryEligible(row) {
  return row?.group === 'act_now' &&
    isOpenWindow(row) &&
    isFreshEnough(row) &&
    !isHardBlocked(row) &&
    decisionRiskTier(row) !== 'high'
}

function adjustedScore(row) {
  let score = numeric(row?.trade_score) ?? 0
  const risk = decisionRiskTier(row)
  if (risk === 'high') score -= 55
  else if (risk === 'medium') score -= 20

  if (row?.entry_quality === 'fresh_entry') score += 8
  else if (row?.entry_quality === 'acceptable_pullback') score += 5
  else if (row?.entry_quality === 'already_ran') score -= 45
  else if (row?.entry_quality === 'already_faded') score -= 20

  const momentum = numeric(row?.price_change_24h)
  if (momentum != null && momentum > 0) score += Math.min(8, momentum / 4)
  if (!isFreshEnough(row)) score -= 25
  if (row?.window_state === 'late') score -= 60
  if (row?.window_state === 'closed') score -= 80
  if (isHardBlocked(row)) score -= 80
  return Math.round(Math.max(0, Math.min(100, score)) * 10) / 10
}

function reasonList(row, riskTier) {
  const reasons = []
  // Stable reason CODES (not display copy) — actionability.js
  // formatDecisionReason() maps these to plain words at render time; the raw
  // codes stay available in each row's ProvenanceExpander (DASH-03).
  if (row?.window_state) reasons.push(`window_${row.window_state}`)
  if (row?.entry_quality) reasons.push(row.entry_quality)
  if (isFreshEnough(row)) reasons.push('price_fresh')
  if (numeric(row?.price_change_24h) != null && numeric(row.price_change_24h) > 0) {
    reasons.push('momentum_24h_positive')
  }
  if (riskTier === 'high') reasons.push('risk_demoted')
  if (row?.source_corpus === 'tracker') reasons.push('tracker_only')
  return reasons.slice(0, 6)
}

// DASH-02: the single canonical score surfaced per row. Returns the
// risk/entry/window-adjusted score, or null when the row has no scoring basis
// (no raw trade_score, or a raw score of 0). Mirrors the fmt() n-gate pattern
// (SignalTrustTab: n===0 renders '—', not a misleading worst-rank 0) so tracker
// rows with no score read as '—' instead of dead-last. A row that WAS scored
// but penalized to 0 keeps a real 0 (raw > 0 → number).
export function canonicalScore(row) {
  const raw = numeric(row?.trade_score)
  if (raw == null || raw === 0) return null
  return adjustedScore(row)
}

function decorate(row, decisionLabel) {
  const riskTier = decisionRiskTier(row)
  const adjusted = adjustedScore(row)
  return {
    ...row,
    decision_label: decisionLabel,
    risk_tier: riskTier,
    adjusted_score: adjusted,
    decision_reasons: reasonList(row, riskTier),
  }
}

function sortDecisionRows(a, b) {
  if (b.adjusted_score !== a.adjusted_score) {
    return b.adjusted_score - a.adjusted_score
  }
  return String(a.token_id || '').localeCompare(String(b.token_id || ''))
}

function blockedSummary(payload) {
  const visible = asRows(payload, 'blocked').length
  const total = payload?.meta?.group_counts?.blocked ?? visible
  const hidden = payload?.meta?.group_hidden_counts?.blocked ?? Math.max(0, total - visible)
  return { visible, total, hidden }
}

export function buildTradeDecisionBoard(payload) {
  if (!payload || typeof payload !== 'object') {
    return {
      headline: {
        status: 'empty',
        label: 'No rows loaded',
        detail: 'Refresh the Trade Inbox to load the decision board.',
      },
      primary: null,
      watchlist: [],
      late: [],
      blocked_summary: { visible: 0, total: 0, hidden: 0 },
      meta: { read_only: true, not_trade_advice: true },
    }
  }

  const actRows = asRows(payload, 'act_now')
  const watchRows = asRows(payload, 'watch')
  const lateRows = asRows(payload, 'already_ran')

  const eligiblePrimary = actRows
    .filter(isPrimaryEligible)
    .map(row => decorate(row, 'Review first'))
    .sort(sortDecisionRows)

  const primary = eligiblePrimary[0] || null
  const primaryKey = primary ? `${primary.source_corpus || 'paper'}:${primary.token_id}` : null
  const watchCandidates = [...actRows, ...watchRows]
    .filter(row => !isHardBlocked(row))
    .filter(row => isOpenWindow(row))
    .filter(row => `${row.source_corpus || 'paper'}:${row.token_id}` !== primaryKey)
    .map(row => decorate(row, 'Watch'))
    .sort(sortDecisionRows)
    .slice(0, MAX_WATCH_ROWS)
    .map((row, idx) => ({ ...row, decision_label: idx === 0 ? 'Best watch' : row.decision_label }))

  const late = lateRows
    .slice(0, MAX_LATE_ROWS)
    .map(row => decorate(row, 'Too late'))

  const headline = primary
    ? {
        status: 'review_available',
        label: 'Review row available',
        detail: 'One clean review row is above watch-only rows.',
      }
    : {
        status: 'no_clean_review',
        label: 'No clean review-now rows',
        detail: watchCandidates.length
          ? 'Best available rows are watch-only.'
          : 'No open-window watch rows are available.',
      }

  return {
    headline,
    primary,
    watchlist: watchCandidates,
    late,
    blocked_summary: blockedSummary(payload),
    meta: {
      read_only: payload.meta?.read_only === true,
      not_trade_advice: payload.meta?.not_trade_advice === true,
      generated_at: payload.meta?.generated_at || null,
      source_rows_considered: payload.meta?.source_rows_considered ?? null,
    },
  }
}

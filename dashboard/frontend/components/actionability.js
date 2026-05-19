// Shared actionability presentation helpers used by TradingTab + SignalsTab.
//
// Source of truth for the v1 reason strings: scout/trading/actionability.py.
// The classifier itself is not affected by anything here — these are
// presentation-only mappings for the dashboard.

// Map each v1 reason code to:
//   - label: short human-readable label
//   - why: hover/long-form explanation
// Reasons not in the map fall through to a default formatter.
export const REASON_INFO = {
  v1_pass_chain_completed_mcap_unknown_exception: {
    label: 'Chain-completed (mcap unknown, allowed)',
    why: 'chain_completed signal has no mcap available; v1 still treats it as actionable since the chain itself is the signal.',
  },
  v1_block_missing_mcap: {
    label: 'Missing mcap',
    why: 'No market-cap data was attached at trade-open; v1 cannot evaluate the mcap-band rule.',
  },
  v1_pass_core_signal_mcap_10_50m: {
    label: 'Core signal · $10M–$50M',
    why: 'narrative_prediction, chain_completed, or volume_spike with mcap in the $10M–$50M sweet spot.',
  },
  v1_pass_core_signal_mcap_50m_plus: {
    label: 'Core signal · ≥$50M',
    why: 'narrative_prediction, chain_completed, or volume_spike with mcap at or above $50M.',
  },
  v1_block_core_signal_mcap_below_10m: {
    label: 'Core signal · <$10M (junk band)',
    why: 'Core signal but mcap below $10M — historically dominated by junk; routed to exploratory.',
  },
  v1_block_gainers_early_mcap_5_10m: {
    label: 'gainers_early · $5M–$10M (low confidence)',
    why: 'gainers_early signals between $5M–$10M are exploratory by v1 design.',
  },
  v1_block_gainers_early_confluence_3: {
    label: 'gainers_early · 3+ sources (likely already pumped)',
    why: 'When confluence is 3 or more, the move is usually already broad-market; v1 routes to exploratory.',
  },
  v1_pass_gainers_early_mcap_50m_plus: {
    label: 'gainers_early · ≥$50M',
    why: 'gainers_early at ≥$50M with confluence < 3 — actionable per v1.',
  },
  v1_block_gainers_early_mcap_10_50m_observe: {
    label: 'gainers_early · $10M–$50M (observe only)',
    why: 'gainers_early in the $10M–$50M band is exploratory pending more evidence.',
  },
  v1_block_gainers_early_not_50m_plus: {
    label: 'gainers_early · not ≥$50M',
    why: 'gainers_early below $50M is exploratory by v1 design.',
  },
  v1_block_losers_contrarian_exploratory: {
    label: 'losers_contrarian (exploratory by design)',
    why: 'losers_contrarian is intentionally low-confidence and always classified exploratory in v1.',
  },
  v1_block_trending_catch_low_n: {
    label: 'trending_catch (low-n, exploratory by design)',
    why: 'trending_catch has too few historical events to rank; always exploratory in v1.',
  },
  v1_block_tg_social_low_n: {
    label: 'tg_social (low-n, exploratory by design)',
    why: 'tg_social channels are still under evaluation; always exploratory in v1.',
  },
  v1_block_unknown_signal_type: {
    label: 'Unknown signal type',
    why: 'Signal type not recognized by v1 classifier; routed to exploratory.',
  },
}

// Three-state classification used everywhere a cohort badge is rendered.
// 1 → actionable (green), 0 → exploratory (amber), NULL → unknown (gray).
export function actionabilityState(value) {
  if (value === 1) return 'actionable'
  if (value === 0) return 'exploratory'
  return 'unknown'
}

// Short cohort label for badges + summary cards.
export function cohortLabel(state) {
  if (state === 'actionable') return 'Actionable'
  if (state === 'exploratory') return 'Exploratory'
  return 'Unknown'
}

// Long-form distinguishing copy for the three cohorts. Used in subtitle
// text + hover tooltips so 'exploratory' reads as a deliberate cohort, not
// a failure, and 'unknown' reads as not-rankable-yet, not neutral.
export function cohortSubtitle(state) {
  if (state === 'actionable') return 'high-confidence v1 cohort'
  if (state === 'exploratory') return 'intentional low-confidence (not bad)'
  return 'not rankable yet (no actionability stamp)'
}

export function cohortColor(state) {
  if (state === 'actionable') return 'var(--color-accent-green)'
  if (state === 'exploratory') return 'var(--color-accent-amber)'
  return 'var(--color-text-secondary)'
}

export function cohortBg(state) {
  if (state === 'actionable') return 'rgba(76, 175, 80, 0.12)'
  if (state === 'exploratory') return 'rgba(255, 183, 77, 0.12)'
  return 'var(--color-bar-bg, #1a1a1a)'
}

// Human-readable label for a v1 reason. Falls back to a sanitized form
// of the raw code so future reason additions don't crash the UI.
export function formatActionabilityReason(reason) {
  if (!reason) return 'unstamped'
  const info = REASON_INFO[reason]
  if (info) return info.label
  return String(reason).replace(/^v1_/, '').replaceAll('_', ' ')
}

// Long-form 'why' explanation for hover tooltips. Empty string falls
// through gracefully when the reason isn't in REASON_INFO.
export function reasonWhy(reason) {
  if (!reason) return ''
  const info = REASON_INFO[reason]
  return info ? info.why : ''
}

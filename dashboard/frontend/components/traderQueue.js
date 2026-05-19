// Trader Action Queue — bucket definitions.
//
// BL-NEW-DASHBOARD-TRADER-ACTION-QUEUE. Read-only client-side
// partitioning of the Open Positions list into actionable + risk-side
// + missing-data buckets. The trader uses this panel to decide where to
// look first; clicking a bucket card filters the Open Positions table
// to that bucket's predicate.
//
// Each bucket has:
//   - label:       short text shown on the card
//   - sublabel:    one-line explanation under the count
//   - tone:        'risk' | 'opportunity' | 'neutral' — drives card color
//   - predicate:   row → bool; rows for which this bucket applies
//   - sort:        optional comparator for the preview list
//   - topN:        preview size (default 3)
//
// All thresholds are presentation-only defaults. They do NOT change
// trade behavior, exits, scoring, or actionability classification.
// They exist solely so the trader can scan the buckets.

const NEAR_STOP_MARGIN_PP = 2   // within Npp of triggering the stop
const WINNER_FORMING_MIN_PP = 10 // at least Npp up but not yet at TP
const OLD_POSITION_DAYS = 14    // "stale and still open" floor

function pnlPct(p) {
  if (p.total_pnl_pct != null) return Number(p.total_pnl_pct)
  return null
}
function slPct(p) {
  if (p.sl_pct != null) return Number(p.sl_pct)
  return null
}
function tpPct(p) {
  if (p.tp_pct != null) return Number(p.tp_pct)
  return null
}

export const TRADER_BUCKETS = {
  actionable_losers_near_stop: {
    label: 'Actionable losers near stop',
    sublabel: `≤ ${NEAR_STOP_MARGIN_PP}pp from SL trigger`,
    tone: 'risk',
    predicate: (p) => {
      if (p.actionable !== 1) return false
      const pnl = pnlPct(p)
      const sl = slPct(p)
      if (pnl == null || sl == null) return false
      // sl_pct is the magnitude of the stop. PnL crosses the stop when
      // pnl_pct <= -sl_pct. "Near stop" = within MARGIN of that level.
      return pnl <= -(sl - NEAR_STOP_MARGIN_PP)
    },
    sort: (a, b) => (pnlPct(a) ?? 0) - (pnlPct(b) ?? 0),
  },
  actionable_winners_forming: {
    label: 'Actionable winners forming',
    sublabel: `≥ ${WINNER_FORMING_MIN_PP}pp up, not yet at TP`,
    tone: 'opportunity',
    predicate: (p) => {
      if (p.actionable !== 1) return false
      const pnl = pnlPct(p)
      const tp = tpPct(p)
      if (pnl == null) return false
      if (pnl < WINNER_FORMING_MIN_PP) return false
      if (tp != null && pnl >= tp) return false
      return true
    },
    sort: (a, b) => (pnlPct(b) ?? 0) - (pnlPct(a) ?? 0),
  },
  exploratory_winners_possible_fn: {
    label: 'Exploratory winners (possible FN)',
    sublabel: 'classifier called low-confidence, but up',
    tone: 'opportunity',
    predicate: (p) => {
      if (p.actionable !== 0) return false
      const pnl = pnlPct(p)
      if (pnl == null) return false
      return pnl >= WINNER_FORMING_MIN_PP
    },
    sort: (a, b) => (pnlPct(b) ?? 0) - (pnlPct(a) ?? 0),
  },
  largest_open_losses: {
    label: 'Largest open losses',
    sublabel: 'sorted by realized + unrealized PnL',
    tone: 'risk',
    predicate: (p) =>
      p.total_pnl_usd != null && Number(p.total_pnl_usd) < 0,
    sort: (a, b) => Number(a.total_pnl_usd ?? 0) - Number(b.total_pnl_usd ?? 0),
    topN: 5,
  },
  no_current_price: {
    label: 'No current price',
    sublabel: 'price_cache miss — stop/TP cannot trigger',
    tone: 'risk',
    predicate: (p) => p.current_price == null,
    sort: (a, b) => (a.opened_at || '').localeCompare(b.opened_at || ''),
  },
  oldest_open: {
    label: 'Oldest open positions',
    sublabel: `older than ${OLD_POSITION_DAYS}d still open`,
    tone: 'neutral',
    predicate: (p) => {
      if (!p.opened_at) return false
      const opened = Date.parse(p.opened_at)
      if (Number.isNaN(opened)) return false
      const ageMs = Date.now() - opened
      return ageMs >= OLD_POSITION_DAYS * 24 * 60 * 60 * 1000
    },
    sort: (a, b) => (a.opened_at || '').localeCompare(b.opened_at || ''),
  },
  unknown_unstamped: {
    label: 'Unknown / unstamped',
    sublabel: 'not rankable yet (legacy or pre-cutover)',
    tone: 'neutral',
    predicate: (p) => p.actionable == null,
    sort: (a, b) => (a.opened_at || '').localeCompare(b.opened_at || ''),
  },
}

// Stable iteration order — traders read top to bottom in priority order.
export const TRADER_BUCKET_ORDER = [
  'actionable_losers_near_stop',
  'actionable_winners_forming',
  'exploratory_winners_possible_fn',
  'largest_open_losses',
  'no_current_price',
  'oldest_open',
  'unknown_unstamped',
]

export function computeTraderBuckets(positions) {
  return TRADER_BUCKET_ORDER.map((key) => {
    const def = TRADER_BUCKETS[key]
    const matched = positions.filter(def.predicate)
    const sorted = def.sort ? matched.slice().sort(def.sort) : matched
    const top = sorted.slice(0, def.topN ?? 3)
    return { key, def, count: matched.length, top }
  })
}

export function bucketToneColor(tone) {
  if (tone === 'risk') return 'var(--color-accent-red, #ef5350)'
  if (tone === 'opportunity') return 'var(--color-accent-green)'
  return 'var(--color-text-secondary)'
}

export function bucketToneBg(tone, isActive) {
  if (!isActive) return 'var(--color-bar-bg, #1a1a1a)'
  if (tone === 'risk') return 'rgba(239, 83, 80, 0.10)'
  if (tone === 'opportunity') return 'rgba(76, 175, 80, 0.10)'
  return 'rgba(74, 144, 226, 0.08)'
}
